package care.ira.watch.outbox

import care.ira.watch.core.Clock
import care.ira.watch.storage.AtomicFiles
import care.ira.watch.storage.LineCodec
import care.ira.watch.storage.Sealer
import care.ira.watch.storage.quarantine
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.util.UUID

/**
 * Urgent things the watch has to tell the server: Help, a fall, "I'm OK", an
 * answer to a question. Each is written to disk BEFORE anything is sent, with
 * its own idempotency key, so it survives the service being killed, the app
 * restarting and the watch rebooting; it is removed only when the server has
 * answered. A retry re-sends the same key, and the server hands back the same
 * answer, so a flaky link never opens two alerts.
 *
 * Retries back off (5 s, 10 s, 20 s ... up to [maxBackoffMs]) and the outbox
 * is always drained before ordinary readings go up (see [Syncer]).
 *
 * One event per line, appended and synced to disk; a rewrite replaces the whole file at once
 * (care/ira/watch/storage: written to a temporary file, synced, renamed), so a crash in the
 * middle of a rewrite can never lose the events already waiting. Each line is sealed with the
 * watch's Keystore key when one is given. A line that cannot be read is moved aside, never
 * dropped. `fileFor` gives the current wearer's own file (WearerFiles), so an event can only
 * ever be sent in the name of the person it was raised for. Pure Kotlin: tested without a watch.
 */
class Outbox(
    private val fileFor: () -> File,
    private val clock: Clock = Clock.SYSTEM,
    private val maxBackoffMs: Long = 5 * 60_000L,
    private val sealer: Sealer? = null,
) {
    constructor(file: File, clock: Clock = Clock.SYSTEM, maxBackoffMs: Long = 5 * 60_000L, sealer: Sealer? = null) :
        this({ file }, clock, maxBackoffMs, sealer)

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    /** Lines moved aside because they could not be read, since this outbox was opened. */
    var unreadable: Int = 0
        private set

    /** What is waiting, oldest first. Reads the file every time: the file is the truth. */
    @Synchronized
    fun pending(): List<UrgentEvent> = readAll()

    @Synchronized
    fun size(): Int = readAll().size

    /**
     * Queue an event. Returns it (with its key) so the caller can show "Sending…" for it. An event
     * whose key is already waiting is not queued twice: the one waiting is returned (a fall
     * incident's keys are derived from its id, FallOutbox, so a repeat is the same event).
     */
    @Synchronized
    fun enqueue(kind: String, path: String, body: String, idempotencyKey: String = UUID.randomUUID().toString()): UrgentEvent {
        readAll().firstOrNull { it.idempotencyKey == idempotencyKey }?.let { return it }
        val event = UrgentEvent(idempotencyKey, kind, path, body, createdAt = clock.nowMillis())
        AtomicFiles.appendLine(fileFor(), line(event))
        return event
    }

    /** Urgent events written minimised because the storage key could not be used, since this outbox was opened. */
    var degradedWrites: Int = 0
        private set

    /**
     * Sealed when the key works. When it cannot be used, the event is still written, so a Help
     * request is never lost, but minimised: what the server needs to raise the alert stays, and
     * where the person is and what they said do not. The server still opens the SOS; it says the
     * location is unavailable, which the escalation already handles.
     */
    private fun line(event: UrgentEvent): String {
        LineCodec.sealOrNull(sealer, json.encodeToString(event))?.let { return it }
        degradedWrites++
        return LineCodec.PLAIN + json.encodeToString(minimised(event))
    }

    /**
     * What to send now, oldest first: when any event is due, every waiting event, in order. An
     * event backing off after a failed attempt is never overtaken by a newer one (a fall's
     * "I'm OK" must reach the server before the next fall's start, D-092); the back-off delays
     * the whole queue's next attempt, not one event's place in it.
     */
    @Synchronized
    fun due(): List<UrgentEvent> {
        val now = clock.nowMillis()
        val all = readAll()
        return if (all.any { it.nextAttemptAt <= now }) all else emptyList()
    }

    /** The server answered (success or a definite refusal): forget the event. */
    @Synchronized
    fun complete(idempotencyKey: String) {
        writeAll(readAll().filter { it.idempotencyKey != idempotencyKey })
    }

    /**
     * The attempt failed for a reason worth retrying: back off. The events behind it wait as long
     * (they are sent after it, in order); an event queued later is due at once and brings the
     * whole queue with it.
     */
    @Synchronized
    fun failed(idempotencyKey: String, reason: String) {
        val now = clock.nowMillis()
        val events = readAll()
        val failed = events.firstOrNull { it.idempotencyKey == idempotencyKey } ?: return
        val attempts = failed.attempts + 1
        val until = now + minOf(maxBackoffMs, 5_000L shl minOf(attempts - 1, 12))
        writeAll(events.map { e ->
            if (e.idempotencyKey == idempotencyKey) e.copy(attempts = attempts, nextAttemptAt = until, lastError = reason.take(80))
            else e.copy(nextAttemptAt = maxOf(e.nextAttemptAt, until))
        })
    }

    private fun readAll(): List<UrgentEvent> {
        val file = fileFor()
        if (!file.exists()) return emptyList()
        val bad = mutableListOf<String>()
        val events = file.readLines().filter { it.isNotBlank() }.mapNotNull { line ->
            val event = LineCodec.decode(sealer, line)?.let { runCatching { json.decodeFromString<UrgentEvent>(it) }.getOrNull() }
            if (event == null) bad += line
            event
        }
        if (bad.isNotEmpty()) {
            // Never silently: moved to `<file>.unreadable`, counted, and the readable rest kept.
            quarantine(file, bad)
            unreadable += bad.size
            writeAll(events)
        }
        return events.sortedBy { it.createdAt }
    }

    private fun writeAll(events: List<UrgentEvent>) {
        AtomicFiles.write(fileFor(), events.joinToString("") { line(it) + "\n" })
    }

    companion object {
        /** Fields that say where the person is or what they said: never written unsealed. */
        val SENSITIVE_FIELDS = setOf("location", "note", "text")

        fun minimised(event: UrgentEvent): UrgentEvent {
            val body = runCatching { Json.parseToJsonElement(event.body).jsonObject }.getOrNull()
                ?: return event.copy(body = "{}", degraded = true)
            return event.copy(body = JsonObject(body.filterKeys { it !in SENSITIVE_FIELDS }).toString(), degraded = true)
        }

        const val KIND_HELP = "help"
        /** A position that came after its Help request (CareForegroundService.sendLateFix). */
        const val KIND_HELP_LOCATION = "help_location"
        const val KIND_FALL_STARTED = "fall_started"
        const val KIND_FALL_OK = "fall_ok"
        const val KIND_ANSWER = "checkin_answer"
        const val KIND_MISSED = "checkin_missed"
    }
}

/** One urgent event on disk. `path` and `body` are exactly what the server receives. */
@Serializable
data class UrgentEvent(
    val idempotencyKey: String,
    val kind: String,
    val path: String,
    val body: String,
    /** Written unsealed and minimised (the storage key could not be used): location and words left out. */
    val degraded: Boolean = false,
    val createdAt: Long,
    val attempts: Int = 0,
    val nextAttemptAt: Long = 0L,
    val lastError: String = "",
)
