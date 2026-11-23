package care.ira.watch.outbox

import care.ira.watch.checkin.FakeClock
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class OutboxTest {
    private val clock = FakeClock(1_000_000)
    private fun tmp() = File.createTempFile("outbox", ".jsonl").also { it.delete() }

    @Test
    fun `an event is on disk before anything is sent and survives a restart`() {
        val file = tmp()
        val box = Outbox(file, clock)
        val e = box.enqueue(Outbox.KIND_HELP, "/api/v1/care/devices/me/help", """{"via":"button"}""")
        assertTrue(file.exists() && file.readText().contains(e.idempotencyKey))
        // A new process opens the same file: the event is still there, same key.
        val reopened = Outbox(file, clock)
        assertEquals(listOf(e), reopened.pending())
        assertEquals(e.idempotencyKey, reopened.due().single().idempotencyKey)
    }

    @Test
    fun `completing removes only that event and keeps the order of the rest`() {
        val box = Outbox(tmp(), clock)
        val a = box.enqueue(Outbox.KIND_FALL_STARTED, "/fall", "{}")
        clock.advance(1)
        val b = box.enqueue(Outbox.KIND_ANSWER, "/answer", "{}")
        clock.advance(1)
        val c = box.enqueue(Outbox.KIND_HELP, "/help", "{}")
        box.complete(b.idempotencyKey)
        assertEquals(listOf(a.idempotencyKey, c.idempotencyKey), box.pending().map { it.idempotencyKey })
        box.complete("not-a-key")
        assertEquals(2, box.size())
    }

    @Test
    fun `a failed attempt backs off and the key never changes`() {
        val box = Outbox(tmp(), clock, maxBackoffMs = 60_000)
        val e = box.enqueue(Outbox.KIND_HELP, "/help", "{}")
        box.failed(e.idempotencyKey, "SocketTimeoutException")
        assertTrue(box.due().isEmpty())
        clock.advance(4_999); assertTrue(box.due().isEmpty())
        clock.advance(1); assertEquals(e.idempotencyKey, box.due().single().idempotencyKey)
        box.failed(e.idempotencyKey, "again")
        clock.advance(9_999); assertTrue(box.due().isEmpty())
        clock.advance(1); assertEquals(1, box.due().size)
        repeat(10) { box.failed(e.idempotencyKey, "x") }
        val waited = box.pending().single().nextAttemptAt - clock.now
        assertEquals(60_000L, waited)   // capped
        assertEquals(e.idempotencyKey, box.pending().single().idempotencyKey)
        assertEquals(12, box.pending().single().attempts)
    }

    @Test
    fun `two events never share a key and a corrupt line is skipped`() {
        val file = tmp()
        val box = Outbox(file, clock)
        val a = box.enqueue(Outbox.KIND_HELP, "/help", "{}")
        file.appendText("garbage\n")
        val b = box.enqueue(Outbox.KIND_HELP, "/help", "{}")
        assertTrue(a.idempotencyKey != b.idempotencyKey)
        assertEquals(2, box.pending().size)
    }
}
