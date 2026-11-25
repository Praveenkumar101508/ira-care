package care.ira.watch.storage

import care.ira.watch.checkin.FakeClock
import care.ira.watch.outbox.Outbox
import java.io.File
import java.nio.file.Files
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SecureStorageTest {
    private fun key(): SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    private val k = key()
    private val sealer = AesGcmSealer { k }
    private val clock = FakeClock(1_000_000)
    private fun dir(): File = Files.createTempDirectory("watch").toFile()

    @Test
    fun `a sealed line reads back, and nothing of it is readable on disk`() {
        val line = LineCodec.encode(sealer, """{"kind":"help","body":"latitude 51.5"}""")
        assertTrue(LineCodec.isSealed(line))
        assertFalse(line.contains("latitude"))
        assertEquals("""{"kind":"help","body":"latitude 51.5"}""", LineCodec.decode(sealer, line))
    }

    @Test
    fun `an altered line or the wrong key is unreadable, never misread`() {
        val line = LineCodec.encode(sealer, "hello")
        val chars = line.toCharArray(); chars[10] = if (chars[10] == 'A') 'B' else 'A'
        assertNull(LineCodec.decode(sealer, String(chars)))
        assertNull(LineCodec.decode(AesGcmSealer { key() }, line))
        assertNull(LineCodec.decode(sealer, "garbage"))
    }

    @Test
    fun `two seals of the same text differ (a fresh nonce each time)`() {
        assertTrue(LineCodec.encode(sealer, "same") != LineCodec.encode(sealer, "same"))
    }

    @Test
    fun `plain text from before sealing is still read`() {
        assertEquals("""{"a":1}""", LineCodec.decode(sealer, """{"a":1}"""))
        assertEquals("x", LineCodec.decode(sealer, "p1:x"))
    }

    @Test
    fun `a broken key still lets an urgent event be written, in the clear rather than lost`() {
        val broken = object : Sealer {
            override fun seal(plain: ByteArray): ByteArray = throw IllegalStateException("keystore unavailable")
            override fun open(sealed: ByteArray): ByteArray = throw IllegalStateException("keystore unavailable")
        }
        assertEquals("p1:help", LineCodec.encode(broken, "help"))
    }

    @Test
    fun `an interrupted rewrite leaves the old file whole`() {
        val d = dir(); val f = File(d, "outbox-p.jsonl")
        AtomicFiles.write(f, "one\ntwo\n")
        File(d, "outbox-p.jsonl.tmp").writeText("half a rewr") // the crash happened mid-rewrite
        AtomicFiles.recover(f)
        assertEquals("one\ntwo\n", f.readText())
        assertFalse(File(d, "outbox-p.jsonl.tmp").exists())
    }

    @Test
    fun `the outbox is sealed on disk, survives reopening, and keeps working after a rewrite`() {
        val d = dir(); val f = File(d, "outbox.jsonl")
        val box = Outbox(f, clock, sealer = sealer)
        val e = box.enqueue("help", "/api/v1/care/devices/me/help", """{"location":{"latitude":51.5}}""")
        assertFalse(f.readText().contains("latitude"))
        assertFalse(f.readText().contains(e.idempotencyKey))
        box.enqueue("fall_ok", "/x", "{}")
        box.failed(e.idempotencyKey, "offline") // a rewrite
        val reopened = Outbox(f, clock, sealer = sealer)
        assertEquals(2, reopened.size())
        assertEquals(1, reopened.pending().first { it.idempotencyKey == e.idempotencyKey }.attempts)
        assertTrue(f.readLines().all { it.startsWith("s1:") })
    }

    @Test
    fun `an outbox from before sealing is read, then sealed when next written`() {
        val d = dir(); val f = File(d, "outbox.jsonl")
        Outbox(f, clock).enqueue("help", "/h", "{}") // the old plain form: p1 lines
        f.writeText(f.readText().removePrefix("p1:")) // and the even older bare JSON line
        val box = Outbox(f, clock, sealer = sealer)
        assertEquals(1, box.size())
        box.enqueue("fall_ok", "/x", "{}")
        box.complete("not-there") // forces a rewrite
        assertTrue(f.readLines().all { it.startsWith("s1:") })
        assertEquals(2, box.size())
    }

    @Test
    fun `an unreadable line is moved aside and counted, and the rest are kept`() {
        val d = dir(); val f = File(d, "outbox.jsonl")
        val box = Outbox(f, clock, sealer = sealer)
        box.enqueue("help", "/h", "{}")
        f.appendText("s1:NOTVALIDBASE64!!\n") // damaged on disk
        box.enqueue("fall_ok", "/x", "{}")
        assertEquals(2, box.size())
        assertEquals(1, box.unreadable)
        assertTrue(File(d, "outbox.jsonl.unreadable").readText().contains("NOTVALIDBASE64"))
    }

    @Test
    fun `after pairing to another person, the last person's events are never in the new person's outbox`() {
        val d = dir()
        var person: String? = "ada"
        val files = WearerFiles(d, "outbox") { person }
        val box = Outbox({ files.current() }, clock, sealer = sealer)
        box.enqueue("help", "/h", "{}") // Ada asked for help while offline
        person = "bea"                   // the watch is re-paired to Bea
        assertEquals("nothing of Ada's to send as Bea", 0, box.size())
        box.enqueue("fall_ok", "/x", "{}")
        assertEquals(1, box.size())
        assertEquals(listOf("outbox-ada.jsonl"), files.others().map { it.name })
        person = "ada"
        assertEquals("Ada's event is still kept, under Ada", 1, box.pending().count { it.kind == "help" })
    }

    @Test
    fun `the single outbox file from before is given to the wearer who is paired now`() {
        val d = dir()
        Outbox(File(d, "outbox.jsonl"), clock).enqueue("help", "/h", "{}")
        val files = WearerFiles(d, "outbox") { "ada" }
        assertEquals(1, Outbox({ files.current() }, clock, sealer = sealer).size())
        assertFalse(File(d, "outbox.jsonl").exists())
    }
}
