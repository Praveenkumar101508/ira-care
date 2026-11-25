package care.ira.watch.devicepolicy

import java.security.PublicKey
import java.security.Signature
import java.util.Base64

/**
 * The single-purpose care watch's device policy (requirement E2.m; os/device/ira/ira_watch_cf/
 * SETTINGS_POLICY.md). Pure Kotlin: the decisions live here and are proved on the JVM
 * (`DevicePolicyTest`); `DevicePolicyManagerTarget` (aosp flavour) turns them into
 * DevicePolicyManager calls.
 *
 * What the person wearing the watch can never reach: developer options, factory reset, safe-mode
 * boot. The lock screen stays off. An IRA administrator can lift the developer-options
 * restriction for a short window, or factory-reset the watch, only with an instruction the IRA
 * server signed for THIS device, that has not expired, that is used once, and whose window is at
 * most [MAX_WINDOW_MS]. Nothing here ever touches an emergency route (EMERGENCY_UI.md).
 *
 * Not done here: becoming device owner. That is provisioning (E4.f), which needs an image.
 */
enum class Restriction(val userManagerKey: String) {
    // The values of UserManager.DISALLOW_*: stable public API strings.
    DEBUGGING_FEATURES("no_debugging_features"),
    FACTORY_RESET("no_factory_reset"),
    SAFE_BOOT("no_safe_boot"),
}

/** The calls the policy makes. `DevicePolicyManagerTarget` is the real one; tests record. */
interface PolicyTarget {
    fun addRestriction(key: String)
    fun clearRestriction(key: String)
    fun setKeyguardDisabled(disabled: Boolean)
    fun wipe(reason: String)
}

/** Remembers instruction nonces already used, so a captured instruction cannot be replayed. */
interface NonceStore {
    fun seen(nonce: String): Boolean
    fun remember(nonce: String, untilMs: Long)
}

class MemoryNonceStore : NonceStore {
    private val used = mutableMapOf<String, Long>()
    override fun seen(nonce: String) = nonce in used
    override fun remember(nonce: String, untilMs: Long) { used[nonce] = untilMs }
}

enum class AdminAction { LIFT_DEBUGGING, FACTORY_RESET }

/**
 * One administrator instruction, as the server signs it. The signed bytes are exactly
 * [payload]; care/devicepolicy.py on the server builds the same string.
 */
data class AdminInstruction(
    val deviceId: String,
    val action: AdminAction,
    val issuedAtMs: Long,
    val expiresAtMs: Long,
    val nonce: String,
) {
    val payload: String get() = "ira-device-policy-v1|$deviceId|${action.name}|$issuedAtMs|$expiresAtMs|$nonce"
}

sealed interface Outcome {
    data class Lifted(val relockAtMs: Long) : Outcome
    data object Wiped : Outcome
    data class Refused(val reason: String) : Outcome
}

class DevicePolicy(
    private val target: PolicyTarget,
    private val serverKey: PublicKey,
    private val deviceId: String,
    private val nonces: NonceStore = MemoryNonceStore(),
    private val now: () -> Long = System::currentTimeMillis,
) {
    private var relockAtMs: Long? = null

    /** On every start: every restriction on, the lock screen off. Idempotent. */
    fun applyBaseline() {
        Restriction.entries.forEach { target.addRestriction(it.userManagerKey) }
        target.setKeyguardDisabled(true)
        relockAtMs = null
    }

    /** An instruction from the server, as base64 of its signature over [AdminInstruction.payload]. */
    fun handle(instruction: AdminInstruction, signatureB64: String): Outcome {
        val t = now()
        val refusal = when {
            !verifies(instruction.payload, signatureB64) -> "signature does not verify"
            instruction.deviceId != deviceId -> "instruction is for another device"
            instruction.issuedAtMs > t + CLOCK_SKEW_MS -> "instruction is from the future"
            t >= instruction.expiresAtMs -> "instruction has expired"
            instruction.expiresAtMs - instruction.issuedAtMs > MAX_WINDOW_MS -> "instruction window is too long"
            instruction.nonce.length < 16 -> "instruction nonce is too short"
            nonces.seen(instruction.nonce) -> "instruction was already used"
            else -> null
        }
        if (refusal != null) return Outcome.Refused(refusal)
        nonces.remember(instruction.nonce, instruction.expiresAtMs)
        return when (instruction.action) {
            AdminAction.LIFT_DEBUGGING -> {
                // Only developer options open; factory reset and safe boot stay locked.
                target.clearRestriction(Restriction.DEBUGGING_FEATURES.userManagerKey)
                relockAtMs = instruction.expiresAtMs
                Outcome.Lifted(instruction.expiresAtMs)
            }
            AdminAction.FACTORY_RESET -> {
                target.wipe("IRA administrator instruction ${instruction.nonce.take(8)}")
                Outcome.Wiped
            }
        }
    }

    /** Called on a timer and at every start: the lifted window closes by itself. */
    fun relockIfDue(): Boolean {
        val at = relockAtMs ?: return false
        if (now() < at) return false
        target.addRestriction(Restriction.DEBUGGING_FEATURES.userManagerKey)
        relockAtMs = null
        return true
    }

    private fun verifies(payload: String, signatureB64: String): Boolean = try {
        val sig = Signature.getInstance("SHA256withECDSA")
        sig.initVerify(serverKey)
        sig.update(payload.toByteArray(Charsets.UTF_8))
        sig.verify(Base64.getDecoder().decode(signatureB64))
    } catch (_: Exception) {
        false
    }

    companion object {
        /** An administrator's window is at most 15 minutes. */
        const val MAX_WINDOW_MS: Long = 15 * 60_000L
        const val CLOCK_SKEW_MS: Long = 60_000L
    }
}
