package care.ira.watch.devicepolicy

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

/** E2.m, E2.o: the wearer never reaches developer options, factory reset or safe boot; a deployment
 *  administrator can, briefly and only with a server-signed, single-use, unrevoked instruction for
 *  this device, from a closed list of actions; the window closes by itself, at expiry or at a start. */
class DevicePolicyTest {
    private class Recorder : PolicyTarget {
        val restrictions = mutableSetOf<String>()
        var keyguardDisabled: Boolean? = null
        val wipes = mutableListOf<String>()
        val calls = mutableListOf<String>()
        override fun addRestriction(key: String) { restrictions += key; calls += "add $key" }
        override fun clearRestriction(key: String) { restrictions -= key; calls += "clear $key" }
        override fun setKeyguardDisabled(disabled: Boolean) { keyguardDisabled = disabled; calls += "keyguard $disabled" }
        override fun wipe(reason: String) { wipes += reason; calls += "wipe" }
    }

    private val all = setOf("no_debugging_features", "no_factory_reset", "no_safe_boot")
    private val keys: KeyPair = newKeys()
    private var clock = 1_000_000L
    private val device = "device-1"

    private fun newKeys(): KeyPair =
        KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()

    private fun policy(target: Recorder, key: PublicKey = keys.public, nonces: NonceStore = MemoryNonceStore()) =
        DevicePolicy(target, key, device, nonces) { clock }

    private fun signText(text: String, by: KeyPair = keys): String {
        val s = Signature.getInstance("SHA256withECDSA")
        s.initSign(by.private)
        s.update(text.toByteArray(Charsets.UTF_8))
        return Base64.getEncoder().encodeToString(s.sign())
    }

    private fun signed(ins: AdminInstruction, by: KeyPair = keys) = signText(ins.payload, by)

    private fun window(nonce: String = "nonce-0123456789ab", deviceId: String = device, window: Long = 10 * 60_000L) =
        AdminInstruction(deviceId, AdminAction.MAINTENANCE_WINDOW, clock, clock + window, nonce)

    @Test fun `the baseline locks exactly developer options, factory reset and safe boot, and keeps the lock screen off`() {
        val t = Recorder()
        policy(t).applyBaseline()
        assertEquals(all, t.restrictions)
        assertEquals(true, t.keyguardDisabled)
        assertTrue(t.wipes.isEmpty())
    }

    @Test fun `a maintenance window opens developer options only, and closes by itself at expiry`() {
        val t = Recorder()
        val p = policy(t).also { it.applyBaseline() }
        val ins = window()
        val out = p.handle(ins, signed(ins))
        assertEquals(Outcome.Lifted(ins.expiresAtMs), out)
        assertEquals(setOf("no_factory_reset", "no_safe_boot"), t.restrictions)
        clock = ins.expiresAtMs - 1
        assertFalse(p.relockIfDue())
        assertTrue(p.windowOpen)
        clock = ins.expiresAtMs
        assertTrue(p.relockIfDue())
        assertFalse(p.windowOpen)
        assertEquals(all, t.restrictions)
    }

    @Test fun `restore restrictions closes a window early`() {
        val t = Recorder()
        val p = policy(t).also { it.applyBaseline() }
        val open = window()
        assertTrue(p.handle(open, signed(open)) is Outcome.Lifted)
        val restore = AdminInstruction(device, AdminAction.RESTORE_RESTRICTIONS, clock, clock + 60_000L, "nonce-restore-0123456")
        assertEquals(Outcome.Restored, p.handle(restore, signed(restore)))
        assertEquals(all, t.restrictions)
        assertFalse(p.windowOpen)
    }

    @Test fun `a factory reset happens only on a valid instruction`() {
        val t = Recorder()
        val ins = AdminInstruction(device, AdminAction.FACTORY_RESET, clock, clock + 60_000L, "nonce-reset-0123456")
        assertEquals(Outcome.Wiped, policy(t).handle(ins, signed(ins)))
        assertEquals(1, t.wipes.size)
    }

    private fun refusedSigned(payload: String, sig: String, nonces: NonceStore = MemoryNonceStore()): String {
        val t = Recorder()
        val p = policy(t, nonces = nonces).also { it.applyBaseline() }
        val calls = t.calls.size
        val out = p.handleSigned(payload, sig)
        assertTrue("$out", out is Outcome.Refused)
        assertEquals("a refusal changes nothing", calls, t.calls.size)
        assertEquals(all, t.restrictions)
        assertTrue(t.wipes.isEmpty())
        return (out as Outcome.Refused).reason
    }

    private fun refusedFor(ins: AdminInstruction, sig: String, nonces: NonceStore = MemoryNonceStore()) =
        refusedSigned(ins.payload, sig, nonces)

    @Test fun `forged, altered, foreign, expired, over-long, early and replayed instructions are refused and change nothing`() {
        val ins = window()
        assertEquals("signature does not verify", refusedFor(ins, signed(ins, newKeys())))
        assertEquals("signature does not verify", refusedFor(ins, "not-base64!"))
        val raw = Base64.getDecoder().decode(signed(ins))
        raw[raw.size - 1] = (raw[raw.size - 1].toInt() xor 0x01).toByte()
        assertEquals("an altered signature", "signature does not verify", refusedFor(ins, Base64.getEncoder().encodeToString(raw)))
        val tampered = ins.copy(action = AdminAction.FACTORY_RESET)
        assertEquals("signature does not verify", refusedFor(tampered, signed(ins)))
        val longer = ins.copy(expiresAtMs = ins.expiresAtMs + 60_000L)
        assertEquals("a stretched expiry", "signature does not verify", refusedFor(longer, signed(ins)))
        val foreign = window(deviceId = "device-2")
        assertEquals("instruction is for another device", refusedFor(foreign, signed(foreign)))
        val long = window(window = DevicePolicy.MAX_WINDOW_MS + 1)
        assertEquals("instruction window is too long", refusedFor(long, signed(long)))
        val short = window(nonce = "short")
        assertEquals("malformed instruction", refusedFor(short, signed(short)))
        val old = window()
        clock = old.expiresAtMs
        assertEquals("instruction has expired", refusedFor(old, signed(old)))
        val future = AdminInstruction(device, AdminAction.MAINTENANCE_WINDOW, clock + 10 * 60_000L, clock + 12 * 60_000L, "nonce-future-01234567")
        assertEquals("instruction is from the future", refusedFor(future, signed(future)))
        val nonces = MemoryNonceStore()
        val once = window(nonce = "nonce-once-0123456789")
        assertTrue(policy(Recorder(), nonces = nonces).handle(once, signed(once)) is Outcome.Lifted)
        assertEquals("instruction was already used", refusedFor(once, signed(once), nonces))
    }

    @Test fun `an action outside the closed list is refused even when the server's key signed it`() {
        val shell = "${AdminInstruction.VERSION}|$device|RUN_SHELL|$clock|${clock + 60_000L}|nonce-shell-0123456789"
        assertEquals("unknown action", refusedSigned(shell, signText(shell)))
        val lower = "${AdminInstruction.VERSION}|$device|maintenance_window|$clock|${clock + 60_000L}|nonce-lower-0123456789"
        assertEquals("unknown action", refusedSigned(lower, signText(lower)))
        val extra = "${AdminInstruction.VERSION}|$device|MAINTENANCE_WINDOW|$clock|${clock + 60_000L}|nonce-extra-0123456789|adb"
        assertEquals("malformed instruction", refusedSigned(extra, signText(extra)))
        val version = "ira-device-policy-v0|$device|MAINTENANCE_WINDOW|$clock|${clock + 60_000L}|nonce-ver-0123456789"
        assertEquals("malformed instruction", refusedSigned(version, signText(version)))
        val notANumber = "${AdminInstruction.VERSION}|$device|MAINTENANCE_WINDOW|soon|${clock + 60_000L}|nonce-nan-0123456789"
        assertEquals("malformed instruction", refusedSigned(notANumber, signText(notANumber)))
        val backwards = "${AdminInstruction.VERSION}|$device|MAINTENANCE_WINDOW|$clock|$clock|nonce-back-0123456789"
        assertEquals("malformed instruction", refusedSigned(backwards, signText(backwards)))
        assertEquals("malformed instruction", refusedSigned("", signText("")))
    }

    @Test fun `a revoked instruction is refused, and revoking an open window closes it at once`() {
        val nonces = MemoryNonceStore()
        val early = window(nonce = "nonce-revoked-0123456")
        policy(Recorder(), nonces = nonces).revoke(early.nonce)
        assertEquals("instruction was revoked", refusedFor(early, signed(early), nonces))

        val t = Recorder()
        val p = policy(t).also { it.applyBaseline() }
        val open = window(nonce = "nonce-open-0123456789")
        assertTrue(p.handle(open, signed(open)) is Outcome.Lifted)
        assertFalse("another nonce leaves the window alone", p.revoke("nonce-other-0123456789"))
        assertTrue(p.windowOpen)
        assertTrue(p.revoke(open.nonce))
        assertFalse(p.windowOpen)
        assertEquals(all, t.restrictions)
    }

    @Test fun `a reboot restores every restriction, and neither replay nor revocation is forgotten`() {
        val dir = Files.createTempDirectory("devicepolicy").toFile()
        try {
            val store = File(dir, "nonces")
            val t = Recorder()  // the platform keeps restrictions across a reboot; so does the recorder
            val before = DevicePolicy(t, keys.public, device, FileNonceStore(store) { clock }) { clock }
            before.applyBaseline()
            before.revoke("nonce-later-0123456789", clock + 60_000L)
            val open = window(nonce = "nonce-reboot-0123456789")
            assertTrue(before.handle(open, signed(open)) is Outcome.Lifted)
            assertEquals(setOf("no_factory_reset", "no_safe_boot"), t.restrictions)

            clock += 60_000L  // reboot inside the window
            val after = DevicePolicy(t, keys.public, device, FileNonceStore(store) { clock }) { clock }
            after.applyBaseline()
            assertEquals("the window does not survive a start", all, t.restrictions)
            assertFalse(after.windowOpen)
            assertEquals(Outcome.Refused("instruction was already used"), after.handle(open, signed(open)))
            val later = window(nonce = "nonce-later-0123456789")
            assertEquals(Outcome.Refused("instruction was revoked"), after.handle(later, signed(later)))
            assertEquals(all, t.restrictions)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun `the nonce store forgets only what expired more than a day ago`() {
        val dir = Files.createTempDirectory("devicepolicy").toFile()
        try {
            val f = File(dir, "nonces")
            val s = FileNonceStore(f) { clock }
            s.remember("nonce-old-0123456789", clock - FileNonceStore.KEEP_AFTER_EXPIRY_MS - 1)
            s.remember("nonce-new-0123456789", clock + 1)
            val reread = FileNonceStore(f) { clock }
            assertFalse(reread.seen("nonce-old-0123456789"))
            assertTrue(reread.seen("nonce-new-0123456789"))
            assertFalse(File(dir, "nonces.tmp").exists())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun `the watch accepts exactly what the server signs (care devicepolicy py fixture)`() {
        val text = javaClass.classLoader!!.getResource("device_policy_instruction.json")!!.readText()
        val root = Json.parseToJsonElement(text).jsonObject
        val pub = KeyFactory.getInstance("EC").generatePublic(
            X509EncodedKeySpec(Base64.getDecoder().decode(root["public_key"]!!.jsonPrimitive.content)))
        val cases = root["cases"]!!.jsonArray
        assertEquals(AdminAction.entries.size, cases.size)
        for (c in cases) {
            val o = c.jsonObject
            val ins = AdminInstruction(o["device_id"]!!.jsonPrimitive.content,
                AdminAction.valueOf(o["action"]!!.jsonPrimitive.content), o["issued_at_ms"]!!.jsonPrimitive.long,
                o["expires_at_ms"]!!.jsonPrimitive.long, o["nonce"]!!.jsonPrimitive.content)
            val payload = o["payload"]!!.jsonPrimitive.content
            assertEquals("the signed bytes are the same string on both sides", payload, ins.payload)
            clock = ins.issuedAtMs + 1_000L
            val out = DevicePolicy(Recorder(), pub, ins.deviceId) { clock }.handleSigned(payload, o["signature"]!!.jsonPrimitive.content)
            assertFalse("${ins.action}: $out", out is Outcome.Refused)
        }
    }
}
