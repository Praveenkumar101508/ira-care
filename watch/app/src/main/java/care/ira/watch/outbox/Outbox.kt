package care.ira.watch.outbox

import care.ira.watch.core.Clock
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
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
 * One JSON object per line, append-only between compactions, like the reading
 * queue. Pure Kotlin: tested without a watch.
 */
class Outbox(private val file: File, private val clock: Clock = Clock.SYSTEM, private val maxBackoffMs: Long = 5 * 60_000L) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    /** What is waiting, oldest first. Reads the file every time: the file is the truth. */
    @Synchronized
    fun pending(): List<UrgentEvent> = readAll()

    @Synchronized
    fun size(): Int = readAll().size

    /** Queue an event. Returns it (with its key) so the caller can show "Sending…" for it. */
    @Synchronized
    fun enqueue(kind: String, path: String, body: String, idempotencyKey: String = UUID.randomUUID().toString()): UrgentEvent {
        val event = UrgentEvent(idempotencyKey, kind, path, body, createdAt = clock.nowMillis())
        file.parentFile?.mkdirs()
        file.appendText(json.encodeToString(event) + "\n")
        return event
    }

    /** The events whose next attempt is due now, oldest first. */
    @Synchronized
    fun due(): List<UrgentEvent> {
        val now = clock.nowMillis()
        return readAll().filter { it.nextAttemptAt <= now }
    }

    /** The server answered (success or a definite refusal): forget the event. */
    @Synchronized
    fun complete(idempotencyKey: String) {
        writeAll(readAll().filter { it.idempotencyKey != idempotencyKey })
    }

    /** The attempt failed for a reason worth retrying: back off. */
    @Synchronized
    fun failed(idempotencyKey: String, reason: String) {
        val now = clock.nowMillis()
        writeAll(readAll().map { e ->
            if (e.idempotencyKey != idempotencyKey) e else {
                val attempts = e.attempts + 1
                val wait = minOf(maxBackoffMs, 5_000L shl minOf(attempts - 1, 12))
                e.copy(attempts = attempts, nextAttemptAt = now + wait, lastError = reason.take(80))
            }
        })
    }

    private fun readAll(): List<UrgentEvent> {
        if (!file.exists()) return emptyList()
        return file.readLines().filter { it.isNotBlank() }.mapNotNull { line ->
            runCatching { json.decodeFromString<UrgentEvent>(line) }.getOrNull()
        }.sortedBy { it.createdAt }
    }

    private fun writeAll(events: List<UrgentEvent>) {
        if (events.isEmpty()) { if (file.exists()) file.writeText(""); return }
        file.parentFile?.mkdirs()
        file.writeText(events.joinToString("") { json.encodeToString(it) + "\n" })
    }

    companion object {
        const val KIND_HELP = "help"
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
    val createdAt: Long,
    val attempts: Int = 0,
    val nextAttemptAt: Long = 0L,
    val lastError: String = "",
)
