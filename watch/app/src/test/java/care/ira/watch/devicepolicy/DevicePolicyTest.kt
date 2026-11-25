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
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

/** E2.m: the wearer never reaches developer options, factory reset or safe boot; an administrator
 *  can, briefly and only with a server-signed, single-use instruction for this device. */
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

    private val keys: KeyPair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    private var clock = 1_000_000L
    private val device = "device-1"

    private fun policy(target: Recorder, key: PublicKey = keys.public, nonces: NonceStore = MemoryNonceStore()) =
        DevicePolicy(target, key, device, nonces) { clock }

    private fun signed(ins: AdminInstruction, by: KeyPair = keys): String {
        val s = Signature.getInstance("SHA256withECDSA")
        s.initSign(by.private)
        s.update(ins.payload.toByteArray(Charsets.UTF_8))
        return Base64.getEncoder().encodeToString(s.sign())
    }

    private fun lift(nonce: String = "nonce-0123456789ab", deviceId: String = device, window: Long = 10 * 60_000L) =
        AdminInstruction(deviceId, AdminAction.LIFT_DEBUGGING, clock, clock + window, nonce)

    @Test fun `the baseline locks exactly developer options, factory reset and safe boot, and keeps the lock screen off`() {
        val t = Recorder()
        policy(t).applyBaseline()
        assertEquals(setOf("no_debugging_features", "no_factory_reset", "no_safe_boot"), t.restrictions)
        assertEquals(true, t.keyguardDisabled)
        assertTrue(t.wipes.isEmpty())
    }

    @Test fun `a valid lift opens developer options only, and the window closes by itself`() {
        val t = Recorder()
        val p = policy(t).also { it.applyBaseline() }
        val ins = lift()
        val out = p.handle(ins, signed(ins))
        assertEquals(Outcome.Lifted(ins.expiresAtMs), out)
        assertEquals(setOf("no_factory_reset", "no_safe_boot"), t.restrictions)
        assertFalse(p.relockIfDue())
        clock = ins.expiresAtMs
        assertTrue(p.relockIfDue())
        assertEquals(setOf("no_debugging_features", "no_factory_reset", "no_safe_boot"), t.restrictions)
    }

    @Test fun `a factory reset happens only on a valid instruction`() {
        val t = Recorder()
        val ins = AdminInstruction(device, AdminAction.FACTORY_RESET, clock, clock + 60_000L, "nonce-reset-0123456")
        assertEquals(Outcome.Wiped, policy(t).handle(ins, signed(ins)))
        assertEquals(1, t.wipes.size)
    }

    private fun refusedFor(ins: AdminInstruction, sig: String, nonces: NonceStore = MemoryNonceStore()): String {
        val t = Recorder()
        val p = policy(t, nonces = nonces).also { it.applyBaseline() }
        val calls = t.calls.size
        val out = p.handle(ins, sig)
        assertTrue("$out", out is Outcome.Refused)
        assertEquals("a refusal changes nothing", calls, t.calls.size)
        return (out as Outcome.Refused).reason
    }

    @Test fun `forged, foreign, expired, over-long and replayed instructions are refused and change nothing`() {
        val ins = lift()
        val other = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        assertEquals("signature does not verify", refusedFor(ins, signed(ins, other)))
        assertEquals("signature does not verify", refusedFor(ins, "not-base64!"))
        val tampered = ins.copy(action = AdminAction.FACTORY_RESET)
        assertEquals("signature does not verify", refusedFor(tampered, signed(ins)))
        val foreign = lift(deviceId = "device-2")
        assertEquals("instruction is for another device", refusedFor(foreign, signed(foreign)))
        val long = lift(window = DevicePolicy.MAX_WINDOW_MS + 1)
        assertEquals("instruction window is too long", refusedFor(long, signed(long)))
        val short = lift(nonce = "short")
        assertEquals("instruction nonce is too short", refusedFor(short, signed(short)))
        val old = lift()
        clock = old.expiresAtMs
        assertEquals("instruction has expired", refusedFor(old, signed(old)))
        val future = AdminInstruction(device, AdminAction.LIFT_DEBUGGING, clock + 10 * 60_000L, clock + 12 * 60_000L, "nonce-future-01234567")
        assertEquals("instruction is from the future", refusedFor(future, signed(future)))
        val nonces = MemoryNonceStore()
        val once = lift(nonce = "nonce-once-0123456789")
        val p = policy(Recorder(), nonces = nonces)
        assertTrue(p.handle(once, signed(once)) is Outcome.Lifted)
        assertEquals("instruction was already used", refusedFor(once, signed(once), nonces))
    }

    @Test fun `the watch accepts exactly what the server signs (care devicepolicy py fixture)`() {
        val text = javaClass.classLoader!!.getResource("device_policy_instruction.json")!!.readText()
        val root = Json.parseToJsonElement(text).jsonObject
        val pub = KeyFactory.getInstance("EC").generatePublic(
            X509EncodedKeySpec(Base64.getDecoder().decode(root["public_key"]!!.jsonPrimitive.content)))
        val cases = root["cases"]!!.jsonArray
        assertEquals(2, cases.size)
        for (c in cases) {
            val o = c.jsonObject
            val ins = AdminInstruction(o["device_id"]!!.jsonPrimitive.content,
                AdminAction.valueOf(o["action"]!!.jsonPrimitive.content), o["issued_at_ms"]!!.jsonPrimitive.long,
                o["expires_at_ms"]!!.jsonPrimitive.long, o["nonce"]!!.jsonPrimitive.content)
            assertEquals("the signed bytes are the same string on both sides", o["payload"]!!.jsonPrimitive.content, ins.payload)
            clock = ins.issuedAtMs + 1_000L
            val t = Recorder()
            val out = DevicePolicy(t, pub, ins.deviceId) { clock }.handle(ins, o["signature"]!!.jsonPrimitive.content)
            assertFalse("${ins.action}: $out", out is Outcome.Refused)
        }
    }
}
