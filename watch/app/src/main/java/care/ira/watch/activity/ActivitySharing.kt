package care.ira.watch.activity

import care.ira.watch.api.ActivityAck
import care.ira.watch.api.ApiOutcome
import care.ira.watch.storage.AtomicFiles
import care.ira.watch.storage.LineCodec
import care.ira.watch.storage.Sealer
import care.ira.watch.storage.quarantine
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Closed hours waiting to be sent, on disk as one sealed JSON line each, in the current wearer's
 * own file (WearerFiles). Bounded to [maxEntries] hours (two weeks by default): beyond that the
 * oldest go. A line that cannot be sealed is held in memory only (activity is health-adjacent and
 * never written in the clear); a line that cannot be read is moved aside. Pure Kotlin.
 */
class ActivityQueue(private val fileFor: () -> File, private val maxEntries: Int = 24 * 14, private val sealer: Sealer? = null) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val unsealed = ArrayDeque<ActivityPeriod>()
    var dropped: Long = 0
        private set

    @Synchronized
    fun append(p: ActivityPeriod) {
        val file = fileFor()
        val line = LineCodec.sealOrNull(sealer, json.encodeToString(p))
        if (line != null) AtomicFiles.appendLine(file, line) else unsealed.addLast(p)
        while (unsealed.size > maxEntries) { unsealed.removeFirst(); dropped++ }
        val lines = lines(file)
        if (lines.size > maxEntries) { dropped += lines.size - maxEntries; rewrite(file, lines.drop(lines.size - maxEntries)) }
    }

    @Synchronized
    fun size(): Int = lines(fileFor()).size + unsealed.size

    @Synchronized
    fun peek(limit: Int): List<ActivityPeriod> = (readable(fileFor()) + unsealed).take(limit)

    @Synchronized
    fun drop(count: Int) {
        if (count <= 0) return
        val file = fileFor()
        val onDisk = lines(file)
        if (onDisk.isNotEmpty()) rewrite(file, onDisk.drop(count))
        repeat((count - onDisk.size).coerceIn(0, unsealed.size)) { unsealed.removeFirst() }
    }

    /** Consent withdrawn: nothing waiting may be sent, so nothing waiting is kept. */
    @Synchronized
    fun clear() {
        val file = fileFor()
        if (file.exists()) rewrite(file, emptyList())
        unsealed.clear()
    }

    private fun lines(file: File): List<String> = if (file.exists()) file.readLines().filter { it.isNotBlank() } else emptyList()

    private fun readable(file: File): List<ActivityPeriod> {
        val bad = mutableListOf<String>(); val good = mutableListOf<String>()
        val out = lines(file).mapNotNull { line ->
            val p = LineCodec.decode(sealer, line)?.let { runCatching { json.decodeFromString<ActivityPeriod>(it) }.getOrNull() }
            if (p == null) bad += line else good += line
            p
        }
        if (bad.isNotEmpty()) { quarantine(file, bad); rewrite(file, good) }
        return out
    }

    private fun rewrite(file: File, lines: List<String>) =
        AtomicFiles.write(file, if (lines.isEmpty()) "" else lines.joinToString("\n") + "\n")
}

/**
 * The one way an activity estimate leaves the watch, and the consent gate in front of it.
 *
 *  - Windows go to the summariser and to the wearer's own "today" line. A closed hour is queued
 *    only while [sharing] says the person's `activity_sharing` consent is granted (the server
 *    tells the watch in `GET /devices/me`; unknown counts as no).
 *  - [sync] sends queued hours in batches, after the outbox and the readings. With sharing off it
 *    clears the queue instead of sending; when the server answers that consent is not granted
 *    (it checks again), the queue is cleared and nothing more is queued until it is.
 *  - A definite refusal (4xx) drops that batch rather than blocking every later hour; the server
 *    stores nothing it refuses. "Not now" (offline, 5xx) keeps everything for the next sync.
 *
 * Nothing here can raise an alert: there is no alert path on the watch for it, and the server
 * stores the hours apart from the readings its safety rules read. Pure Kotlin.
 */
class ActivitySharing(
    private val summariser: ActivitySummariser,
    private val queue: ActivityQueue,
    private val send: (List<ActivityPeriod>) -> ApiOutcome<ActivityAck>,
    val today: ActivityToday = ActivityToday(),
) {
    /** The server's latest word on the person's consent. False until it has said otherwise. */
    @Volatile var sharing: Boolean = false
        private set

    /** Hours closed while sharing was off, discarded at once (counted, never kept). */
    var discarded: Int = 0
        private set

    fun setSharing(granted: Boolean) {
        sharing = granted
        if (!granted) queue.clear()
    }

    fun onWindow(w: ActivityWindow) {
        today.add(w)
        summariser.add(w).forEach(::closed)
    }

    /** Close the open hour if it is over (called on every sync, so an hour off the wrist still closes). */
    fun tick(nowMillis: Long) { summariser.flushIfDue(nowMillis)?.let(::closed) }

    private fun closed(p: ActivityPeriod) { if (sharing) queue.append(p) else discarded++ }

    enum class Result { Sent, NothingToSend, NotShared, Offline, Unauthorised }

    fun sync(nowMillis: Long, batch: Int = 48): Result {
        tick(nowMillis)
        if (!sharing) { queue.clear(); return Result.NotShared }
        var sent = false
        while (true) {
            val periods = queue.peek(batch)
            if (periods.isEmpty()) return if (sent) Result.Sent else Result.NothingToSend
            when (val out = send(periods)) {
                is ApiOutcome.Ok -> {
                    if (!out.value.sharing) { setSharing(false); return Result.NotShared }
                    queue.drop(periods.size); sent = true
                }
                is ApiOutcome.Failed -> queue.drop(periods.size)
                is ApiOutcome.Offline -> return Result.Offline
                is ApiOutcome.Unauthorised -> return Result.Unauthorised
            }
        }
    }
}
