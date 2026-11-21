package care.ira.watch.queue

import care.ira.watch.api.Reading
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Test

class OfflineQueueTest {
    private fun r(i: Int) = Reading("steps", i.toDouble(), "2026-11-21T10:0$i:00Z")
    private fun tmp() = File.createTempFile("queue", ".jsonl").also { it.delete() }

    @Test
    fun `append peek drop keep order and survive a reopen`() {
        val file = tmp()
        OfflineQueue(file).append(listOf(r(1), r(2), r(3)))
        val reopened = OfflineQueue(file)
        assertEquals(3, reopened.size())
        assertEquals(listOf(r(1), r(2)), reopened.peek(2))
        reopened.drop(2)
        assertEquals(listOf(r(3)), reopened.peek())
        reopened.drop(5)
        assertEquals(0, reopened.size())
        assertEquals(emptyList<Reading>(), OfflineQueue(file).peek())
    }

    @Test
    fun `a corrupt line is skipped not fatal`() {
        val file = tmp()
        val q = OfflineQueue(file)
        q.append(listOf(r(1)))
        file.appendText("not json\n")
        q.append(listOf(r(2)))
        assertEquals(listOf(r(1), r(2)), q.peek())
    }

    @Test
    fun `the queue is bounded and drops the oldest`() {
        val q = OfflineQueue(tmp(), maxEntries = 3)
        q.append(listOf(r(1), r(2), r(3), r(4), r(5)))
        assertEquals(listOf(r(3), r(4), r(5)), q.peek())
        assertEquals(2, q.dropped)
    }

    @Test
    fun `appending nothing writes nothing`() {
        val file = tmp()
        OfflineQueue(file).append(emptyList())
        assertEquals(false, file.exists())
    }
}
