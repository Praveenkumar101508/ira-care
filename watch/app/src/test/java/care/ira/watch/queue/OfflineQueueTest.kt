package care.ira.watch.queue

import care.ira.watch.api.Reading
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
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

    @Test
    fun `readings stay with the wearer they were taken from, sealed on disk`() {
        val dir = java.nio.file.Files.createTempDirectory("q").toFile()
        val key = javax.crypto.KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val sealer = care.ira.watch.storage.AesGcmSealer { key }
        var person: String? = "ada"
        val files = care.ira.watch.storage.WearerFiles(dir, "readings") { person }
        val q = OfflineQueue({ files.current() }, sealer = sealer)
        q.append(listOf(r(1), r(2)))
        assertTrue(java.io.File(dir, "readings-ada.jsonl").readLines().all { it.startsWith("s1:") })
        person = "bea"
        assertEquals("nothing of Ada's to send as Bea", 0, q.size())
        q.append(listOf(r(3)))
        assertEquals(1, q.peek().size)
        person = "ada"
        assertEquals(2, q.peek().size)
    }

    @Test
    fun `a damaged reading is moved aside, and the count and the drop stay in step`() {
        val file = tmp()
        val q = OfflineQueue(file)
        q.append(listOf(r(1)))
        file.appendText("s1:DAMAGED\n")
        q.append(listOf(r(2)))
        assertEquals(2, q.peek().size)
        assertEquals(2, q.size())
        q.drop(1)
        assertEquals(1, q.peek().size)
        assertTrue(java.io.File(file.parentFile, file.name + ".unreadable").readText().contains("DAMAGED"))
    }
}
