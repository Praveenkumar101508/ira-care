package care.ira.watch.queue

import care.ira.watch.api.Reading
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
 */
class OfflineQueue(private val file: File, private val maxEntries: Int = 20_000) {
    private val json = Json { ignoreUnknownKeys = true }
    var dropped: Long = 0
        private set

    @Synchronized
    fun append(readings: List<Reading>) {
        if (readings.isEmpty()) return
        file.parentFile?.mkdirs()
        file.appendText(readings.joinToString("") { json.encodeToString(it) + "\n" })
        trim()
    }

    @Synchronized
    fun size(): Int = if (file.exists()) file.useLines { lines -> lines.count { it.isNotBlank() } } else 0

    /** The next batch to send, oldest first, without removing it. */
    @Synchronized
    fun peek(limit: Int = 500): List<Reading> {
        if (!file.exists()) return emptyList()
        return file.useLines { lines ->
            lines.filter { it.isNotBlank() }.take(limit).mapNotNull { line ->
                runCatching { json.decodeFromString<Reading>(line) }.getOrNull()
            }.toList()
        }
    }

    /** Remove the first `count` entries once the backend has acknowledged them. */
    @Synchronized
    fun drop(count: Int) {
        if (!file.exists() || count <= 0) return
        val remaining = file.readLines().filter { it.isNotBlank() }.drop(count)
        file.writeText(if (remaining.isEmpty()) "" else remaining.joinToString("\n") + "\n")
    }

    private fun trim() {
        val lines = file.readLines().filter { it.isNotBlank() }
        if (lines.size > maxEntries) {
            val excess = lines.size - maxEntries
            dropped += excess
            file.writeText(lines.drop(excess).joinToString("\n") + "\n")
        }
    }
}
