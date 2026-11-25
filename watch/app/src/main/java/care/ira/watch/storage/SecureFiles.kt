package care.ira.watch.storage

import java.io.File
import java.io.FileOutputStream
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/*
 * How the watch keeps what it holds on disk: sealed, written whole or not at all, never
 * silently lost, and kept apart per wearer. Pure Kotlin and javax.crypto, tested on the JVM;
 * the Android Keystore key is supplied by KeystoreKey.kt.
 */

/** Seals bytes so they can only be read with the same key, and tells if they were altered. */
interface Sealer {
    fun seal(plain: ByteArray): ByteArray
    fun open(sealed: ByteArray): ByteArray
}

/**
 * AES-256-GCM. The cipher chooses a fresh 12-byte nonce for every seal (which the Android
 * Keystore requires); it is stored in front of the ciphertext. Tampering or a wrong key fails
 * the GCM tag check, so a damaged line is detected, never misread.
 */
class AesGcmSealer(private val key: () -> SecretKey) : Sealer {
    override fun seal(plain: ByteArray): ByteArray {
        val c = Cipher.getInstance(TRANSFORMATION)
        c.init(Cipher.ENCRYPT_MODE, key())
        val iv = c.iv
        require(iv.size == IV_BYTES) { "unexpected nonce size" }
        return iv + c.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        require(sealed.size > IV_BYTES) { "too short" }
        val c = Cipher.getInstance(TRANSFORMATION)
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_BYTES))
        return c.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }

    companion object {
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
    }
}

/**
 * One stored line. `s1:` and base64 for a sealed line. A line that starts with `{` is a
 * plain-text line from before sealing: still read, and sealed the next time the file is
 * rewritten. `p1:` is plain text written when the key could not be used.
 *
 * When the key cannot be used at all (the Keystore is broken), each store decides what it may
 * still write, through [sealOrNull]: the outbox writes an urgent event MINIMISED (no location, no
 * words), so Help still goes out; the readings queue keeps readings in memory only. [encode]
 * (plain text on failure) is left for the pairing token, without which no Help can be sent.
 */
object LineCodec {
    private const val SEALED = "s1:"
    const val PLAIN = "p1:"

    /**
     * The stored line, or null when the key could not be used (the Keystore failed). With no sealer
     * at all, storage is unencrypted by configuration (the pure-Kotlin tests and tools): the line
     * is plain text, as before. The app always has one (`CareDependencies`, checked by a test).
     */
    fun sealOrNull(sealer: Sealer?, text: String): String? {
        if (sealer == null) return PLAIN + text
        return try {
            SEALED + Base64.getEncoder().encodeToString(sealer.seal(text.toByteArray(Charsets.UTF_8)))
        } catch (e: Exception) {
            null
        }
    }

    fun encode(sealer: Sealer?, text: String): String {
        if (sealer == null) return PLAIN + text
        return try {
            SEALED + Base64.getEncoder().encodeToString(sealer.seal(text.toByteArray(Charsets.UTF_8)))
        } catch (e: Exception) {
            PLAIN + text
        }
    }

    /** The line's text, or null when it cannot be read (damaged, or sealed with another key). */
    fun decode(sealer: Sealer?, line: String): String? = when {
        line.startsWith(SEALED) -> if (sealer == null) null else try {
            String(sealer.open(Base64.getDecoder().decode(line.removePrefix(SEALED))), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
        line.startsWith(PLAIN) -> line.removePrefix(PLAIN)
        line.startsWith("{") -> line // written before sealing
        else -> null
    }

    fun isSealed(line: String) = line.startsWith(SEALED)
}

/** Writes that either happen completely or not at all, and reach the disk before returning. */
object AtomicFiles {
    /** Replace the whole file: a temporary file beside it, synced, then renamed over it. */
    fun write(file: File, text: String) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, "${file.name}.tmp")
        FileOutputStream(tmp).use { out ->
            out.write(text.toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
        if (!tmp.renameTo(file)) {
            // Some file systems will not rename over an existing file: the old one goes only now.
            file.delete()
            check(tmp.renameTo(file)) { "could not replace ${file.name}" }
        }
    }

    /** Add one line and sync it. A crash can at most leave that last line incomplete. */
    fun appendLine(file: File, line: String) {
        file.parentFile?.mkdirs()
        FileOutputStream(file, true).use { out ->
            out.write((line + "\n").toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
    }

    /** A rewrite that was interrupted leaves a .tmp file; the original is still whole. */
    fun recover(file: File) {
        File(file.parentFile, "${file.name}.tmp").takeIf { it.exists() }?.delete()
    }
}

/**
 * One file per wearer: `<base>-<person>.jsonl`. Whatever the watch holds for one person is in
 * that person's file only, so after the watch is paired to somebody else nothing of the last
 * person's can ever be sent in the new person's name. The pre-sealing single file
 * (`<base>.jsonl`) is moved to the current wearer's name the first time a wearer is known.
 */
class WearerFiles(private val dir: File, private val base: String, private val person: () -> String?) {
    fun current(): File {
        val who = person()?.takeIf { it.isNotBlank() }?.let(::safe) ?: "unpaired"
        val file = File(dir, "$base-$who.jsonl")
        val legacy = File(dir, "$base.jsonl")
        if (who != "unpaired" && legacy.exists() && !file.exists()) legacy.renameTo(file)
        AtomicFiles.recover(file)
        return file
    }

    /** The other wearers' files that still hold something (never sent under this wearer). */
    fun others(): List<File> {
        val mine = current().name
        return dir.listFiles { f -> f.name.startsWith("$base-") && f.name.endsWith(".jsonl") && f.name != mine && f.length() > 0 }
            ?.toList().orEmpty()
    }

    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_-]"), "_").take(64)
}

/** Lines that could not be read are kept here, never silently thrown away. */
fun quarantine(file: File, lines: List<String>) {
    if (lines.isEmpty()) return
    val q = File(file.parentFile, "${file.name}.unreadable")
    lines.forEach { AtomicFiles.appendLine(q, it) }
}
