package care.ira.watch.storage

import care.ira.watch.api.Reading
import care.ira.watch.core.Clock
import care.ira.watch.outbox.Outbox
import care.ira.watch.queue.OfflineQueue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * When the storage key cannot be used (a broken Keystore), sensitive data is not written to disk
 * in plain text, and an emergency is never lost: Help is still queued, minimised; readings are
 * held in memory until sent.
 */
class DegradedStorageTest {
    private object BrokenKeystore : Sealer {
        override fun seal(plain: ByteArray): ByteArray = throw IllegalStateException("keystore unavailable")
        override fun open(sealed: ByteArray): ByteArray = throw IllegalStateException("keystore unavailable")
    }

    private fun dir(): File = Files.createTempDirectory("ira-degraded").toFile()
    private val clock = object : Clock { override fun nowMillis() = 1_000L }
    private val helpBody = """{"via":"button","note":"I am in the garden at number 14","location":{"latitude":51.5,"longitude":-0.12}}"""

    @Test
    fun `Help is still queued when nothing can be sealed, without where the person is or what they said`() {
        val file = File(dir(), "outbox")
        val outbox = Outbox(file, clock, sealer = BrokenKeystore)
        outbox.enqueue(Outbox.KIND_HELP, "/api/v1/care/devices/me/help", helpBody, "key-1")
        val onDisk = file.readText()
        assertFalse("location on disk", onDisk.contains("51.5") || onDisk.contains("latitude"))
        assertFalse("words on disk", onDisk.contains("garden"))
        val due = outbox.due()
        assertEquals(1, due.size)
        assertEquals("help", due[0].kind)
        assertEquals("key-1", due[0].idempotencyKey)
        assertEquals("""{"via":"button"}""", due[0].body)
        assertTrue(due[0].degraded)
        assertEquals(1, outbox.degradedWrites)
    }

    @Test
    fun `with a working key the event is sealed whole, as before`() {
        val file = File(dir(), "outbox")
        val key = javax.crypto.KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val outbox = Outbox(file, clock, sealer = AesGcmSealer { key })
        outbox.enqueue(Outbox.KIND_HELP, "/api/v1/care/devices/me/help", helpBody, "key-2")
        assertFalse(file.readText().contains("garden"))
        val e = outbox.due().single()
        assertEquals(helpBody, e.body)
        assertFalse(e.degraded)
        assertEquals(0, outbox.degradedWrites)
    }

    @Test
    fun `readings are never written unencrypted, and still go out in order`() {
        val file = File(dir(), "readings")
        val q = OfflineQueue(file, sealer = BrokenKeystore)
        val rs = (1..3).map { Reading("heart_rate", 70.0 + it, "2026-11-25T08:0$it:00Z", "bpm") }
        q.append(rs)
        assertFalse("health data on disk in plain text", file.exists() && file.readText().contains("heart_rate"))
        assertEquals(3, q.heldInMemory)
        assertEquals(rs, q.peek())
        q.drop(2)
        assertEquals(listOf(rs[2]), q.peek())
        assertEquals(1, q.size())
    }

    @Test
    fun `plain lines written by an earlier version are still read`() {
        val file = File(dir(), "outbox")
        file.writeText(LineCodec.PLAIN + """{"idempotencyKey":"old","kind":"help","path":"/p","body":"{}","createdAt":1}""" + "\n")
        assertEquals("old", Outbox(file, clock, sealer = BrokenKeystore).due().single().idempotencyKey)
    }
}

class StorageWiringTest {
    @Test
    fun `the app always stores pairing, readings and urgent events through the Keystore sealer`() {
        val deps = File("src/main/java/care/ira/watch/service/CareDependencies.kt").readText()
        assertTrue(deps.contains("private val sealer = KeystoreKey.sealer()"))
        for (store in listOf("PairingStore(context, sealer)", "OfflineQueue({ readingFiles.current() }, sealer = sealer)",
                             "Outbox({ outboxFiles.current() }, clock, sealer = sealer)")) {
            assertTrue("$store wired with the sealer", deps.contains(store))
        }
    }
}
