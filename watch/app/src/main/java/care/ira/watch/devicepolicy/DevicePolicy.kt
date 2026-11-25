package care.ira.watch.devicepolicy

import java.io.File
import java.security.PublicKey
import java.security.Signature
import java.util.Base64

/**
 * The single-purpose care watch's device policy (requirements E2.m, E2.o; os/device/ira/
 * ira_watch_cf/SETTINGS_POLICY.md). Pure Kotlin: the decisions live here and are proved on the
 * JVM (`DevicePolicyTest`); `DevicePolicyManagerTarget` (aosp flavour) turns them into
 * DevicePolicyManager calls.
 *
 * What the person wearing the watch can never reach: developer options, factory reset, safe-mode
 * boot. The lock screen stays off. A deployment administrator (D-085) can open a short
 * maintenance window (developer options only), close it early, or factory-reset the watch, and
 * only with an instruction the IRA server signed for THIS device, that has not expired, that has
 * not been used or revoked, and whose window is at most [MAX_WINDOW_MS]. Nothing else can be
 * expressed: the action is one of [AdminAction] or the instruction is refused. Every start (a
 * reboot, a restarted process) puts every restriction back, so a window never outlives the
 * process that opened it. Nothing here ever touches an emergency route (EMERGENCY_UI.md).
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

/**
 * Remembers instruction nonces already used, and nonces the server revoked, so a captured
 * instruction can be neither replayed nor used after revocation. Must survive a reboot on a
 * device ([FileNonceStore]); entries can be forgotten once their instruction has expired.
 */
interface NonceStore {
    fun seen(nonce: String): Boolean
    fun remember(nonce: String, untilMs: Long)
    fun revoked(nonce: String): Boolean = false
    fun revoke(nonce: String, untilMs: Long) {}
}

class MemoryNonceStore : NonceStore {
    private val used = mutableMapOf<String, Long>()
    private val revokedNonces = mutableMapOf<String, Long>()
    override fun seen(nonce: String) = nonce in used
    override fun remember(nonce: String, untilMs: Long) { used[nonce] = untilMs }
    override fun revoked(nonce: String) = nonce in revokedNonces
    override fun revoke(nonce: String, untilMs: Long) { revokedNonces[nonce] = untilMs }
}

/**
 * A [NonceStore] kept in one file (the app's private files directory on a device), rewritten
 * whole through a temporary file and a rename, so a reboot neither forgets a used nonce nor
 * leaves half a file. Entries whose instruction expired more than a day ago are dropped.
 */
class FileNonceStore(private val file: File, private val now: () -> Long = System::currentTimeMillis) : NonceStore {
    private val used = mutableMapOf<String, Long>()
    private val revokedNonces = mutableMapOf<String, Long>()

    init {
        if (file.exists()) file.readLines().forEach { line ->
            val parts = line.split('|')
            val until = parts.getOrNull(2)?.toLongOrNull()
            if (parts.size == 3 && until != null) when (parts[0]) {
                "used" -> used[parts[1]] = until
                "revoked" -> revokedNonces[parts[1]] = until
            }
        }
    }

    override fun seen(nonce: String) = nonce in used
    override fun remember(nonce: String, untilMs: Long) { used[nonce] = untilMs; save() }
    override fun revoked(nonce: String) = nonce in revokedNonces
    override fun revoke(nonce: String, untilMs: Long) { revokedNonces[nonce] = untilMs; save() }

    private fun save() {
        val horizon = now() - KEEP_AFTER_EXPIRY_MS
        used.entries.removeAll { it.value < horizon }
        revokedNonces.entries.removeAll { it.value < horizon }
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(buildString {
            used.forEach { (n, u) -> append("used|").append(n).append('|').append(u).append('\n') }
            revokedNonces.forEach { (n, u) -> append("revoked|").append(n).append('|').append(u).append('\n') }
        })
        if (!tmp.renameTo(file)) {
            file.delete()
            check(tmp.renameTo(file)) { "could not save the nonce store" }
        }
    }

    companion object {
        const val KEEP_AFTER_EXPIRY_MS: Long = 24 * 60 * 60_000L
    }
}

/** The closed list. There is no other action, and no field for a command or an argument. */
enum class AdminAction { MAINTENANCE_WINDOW, RESTORE_RESTRICTIONS, FACTORY_RESET }

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
    val payload: String get() = "$VERSION|$deviceId|${action.name}|$issuedAtMs|$expiresAtMs|$nonce"

    companion object {
        const val VERSION = "ira-device-policy-v1"
        private val NONCE = Regex("^[0-9A-Za-z_-]{16,64}$")

        /** The instruction a signed payload describes, or the reason it describes none. */
        fun parse(payload: String): Pair<AdminInstruction?, String?> {
            val parts = payload.split('|')
            if (parts.size != 6 || parts[0] != VERSION || parts[1].isEmpty()) return null to "malformed instruction"
            val action = AdminAction.entries.firstOrNull { it.name == parts[2] } ?: return null to "unknown action"
            val issued = parts[3].toLongOrNull()
            val expires = parts[4].toLongOrNull()
            if (issued == null || expires == null || !NONCE.matches(parts[5])) return null to "malformed instruction"
            return AdminInstruction(parts[1], action, issued, expires, parts[5]) to null
        }
    }
}

sealed interface Outcome {
    data class Lifted(val relockAtMs: Long) : Outcome
    data object Restored : Outcome
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
    private var windowNonce: String? = null

    /** On every start, including after a reboot: every restriction on, the lock screen off, no
     *  window open. Idempotent. */
    fun applyBaseline() {
        Restriction.entries.forEach { target.addRestriction(it.userManagerKey) }
        target.setKeyguardDisabled(true)
        relockAtMs = null
        windowNonce = null
    }

    /** An instruction from the server as [AdminInstruction]; see [handleSigned]. */
    fun handle(instruction: AdminInstruction, signatureB64: String): Outcome = handleSigned(instruction.payload, signatureB64)

    /**
     * An instruction exactly as the server sent it: the payload string and base64 of the
     * server's signature over its UTF-8 bytes. The signature is checked first, over the bytes
     * received, so nothing unsigned is even parsed into an action. A refusal changes nothing.
     */
    fun handleSigned(payload: String, signatureB64: String): Outcome {
        if (payload.length > MAX_PAYLOAD_CHARS || !verifies(payload, signatureB64)) {
            return Outcome.Refused("signature does not verify")
        }
        val (instruction, malformed) = AdminInstruction.parse(payload)
        if (instruction == null) return Outcome.Refused(malformed ?: "malformed instruction")
        val t = now()
        val refusal = when {
            instruction.deviceId != deviceId -> "instruction is for another device"
            instruction.issuedAtMs > t + CLOCK_SKEW_MS -> "instruction is from the future"
            instruction.expiresAtMs <= instruction.issuedAtMs -> "malformed instruction"
            t >= instruction.expiresAtMs -> "instruction has expired"
            instruction.expiresAtMs - instruction.issuedAtMs > MAX_WINDOW_MS -> "instruction window is too long"
            nonces.revoked(instruction.nonce) -> "instruction was revoked"
            nonces.seen(instruction.nonce) -> "instruction was already used"
            else -> null
        }
        if (refusal != null) return Outcome.Refused(refusal)
        nonces.remember(instruction.nonce, instruction.expiresAtMs)
        return when (instruction.action) {
            AdminAction.MAINTENANCE_WINDOW -> {
                // Only developer options open; factory reset and safe boot stay locked.
                target.clearRestriction(Restriction.DEBUGGING_FEATURES.userManagerKey)
                relockAtMs = instruction.expiresAtMs
                windowNonce = instruction.nonce
                Outcome.Lifted(instruction.expiresAtMs)
            }
            AdminAction.RESTORE_RESTRICTIONS -> {
                restoreAll()
                Outcome.Restored
            }
            AdminAction.FACTORY_RESET -> {
                target.wipe("IRA administrator instruction ${instruction.nonce.take(8)}")
                Outcome.Wiped
            }
        }
    }

    /**
     * The server revoked an instruction (or the administrator who issued it). Unsigned on
     * purpose: it can only make the watch more restricted. If it opened the current window, the
     * window closes now; if it has not arrived yet, it will be refused. Returns whether a window
     * was closed.
     */
    fun revoke(nonce: String, untilMs: Long = now() + MAX_WINDOW_MS): Boolean {
        nonces.revoke(nonce, untilMs)
        if (windowNonce != nonce) return false
        restoreAll()
        return true
    }

    /** Called on a timer and at every start: the lifted window closes by itself. */
    fun relockIfDue(): Boolean {
        val at = relockAtMs ?: return false
        if (now() < at) return false
        target.addRestriction(Restriction.DEBUGGING_FEATURES.userManagerKey)
        relockAtMs = null
        windowNonce = null
        return true
    }

    /** Whether a maintenance window is open right now. */
    val windowOpen: Boolean get() = relockAtMs != null

    private fun restoreAll() {
        Restriction.entries.forEach { target.addRestriction(it.userManagerKey) }
        relockAtMs = null
        windowNonce = null
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
        const val MAX_PAYLOAD_CHARS = 400
    }
}
