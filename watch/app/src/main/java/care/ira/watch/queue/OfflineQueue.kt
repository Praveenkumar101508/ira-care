package care.ira.watch.queue

import care.ira.watch.api.Reading
import care.ira.watch.storage.AtomicFiles
import care.ira.watch.storage.LineCodec
import care.ira.watch.storage.Sealer
import care.ira.watch.storage.quarantine
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Readings waiting to be sent, kept on disk as one JSON object per line, so
 * nothing is lost when the watch is offline, the service is killed or the
 * watch reboots. Re-sent readings are de-duplicated by the backend on
 * (device, kind, recorded_at), so sending twice is safe and losing is not.
 *
 * Bounded: beyond `maxEntries` the OLDEST readings are dropped, with the
 * count kept, so a watch offline for a week does not fill its storage.
 *
 * Each line is sealed with the watch's Keystore key when one is given; rewrites replace the
 * file whole (AtomicFiles); a line that cannot be read is moved aside, not counted as a reading;
 * and `fileFor` is the current wearer's own file, so readings are only ever sent for the person
 * who was wearing the watch.
 */
class OfflineQueue(private val fileFor: () -> File, private val maxEntries: Int = 20_000, private val sealer: Sealer? = null) {
    constructor(file: File, maxEntries: Int = 20_000, sealer: Sealer? = null) : this({ file }, maxEntries, sealer)

    private val json = Json { ignoreUnknownKeys = true }
    var dropped: Long = 0
        private set

    @Synchronized
    fun append(readings: List<Reading>) {
        if (readings.isEmpty()) return
        val file = fileFor()
        readings.forEach { AtomicFiles.appendLine(file, LineCodec.encode(sealer, json.encodeToString(it))) }
        trim(file)
    }

    @Synchronized
    fun size(): Int = lines(fileFor()).size

    /** The next batch to send, oldest first, without removing it. */
    @Synchronized
    fun peek(limit: Int = 500): List<Reading> = readable(fileFor()).take(limit)

    /** Remove the first `count` entries once the backend has acknowledged them. */
    @Synchronized
    fun drop(count: Int) {
        val file = fileFor()
        if (!file.exists() || count <= 0) return
        rewrite(file, lines(file).drop(count))
    }

    private fun lines(file: File): List<String> = if (file.exists()) file.readLines().filter { it.isNotBlank() } else emptyList()

    /** Readable readings; unreadable lines are moved aside once, so counts and drops stay in step. */
    private fun readable(file: File): List<Reading> {
        val all = lines(file)
        val bad = mutableListOf<String>()
        val good = mutableListOf<String>()
        val out = all.mapNotNull { line ->
            val r = LineCodec.decode(sealer, line)?.let { runCatching { json.decodeFromString<Reading>(it) }.getOrNull() }
            if (r == null) bad += line else good += line
            r
        }
        if (bad.isNotEmpty()) { quarantine(file, bad); rewrite(file, good) }
        return out
    }

    private fun rewrite(file: File, lines: List<String>) {
        AtomicFiles.write(file, if (lines.isEmpty()) "" else lines.joinToString("\n") + "\n")
    }

    private fun trim(file: File) {
        val lines = lines(file)
        if (lines.size > maxEntries) {
            val excess = lines.size - maxEntries
            dropped += excess
            rewrite(file, lines.drop(excess))
        }
    }
}
