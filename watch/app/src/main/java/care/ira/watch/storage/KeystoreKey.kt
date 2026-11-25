package care.ira.watch.storage

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * The watch's storage key, kept in the Android Keystore: it never leaves the secure hardware
 * (or the Keystore's software store where the watch has none), and it is never written to a
 * file or a backup. Lost with the app's data (an unpaired, wiped watch cannot read what it held).
 * Hardware backing is the platform's claim on each device; it is not tested here.
 */
object KeystoreKey {
    private const val ALIAS = "ira_care_storage_v1"
    private const val PROVIDER = "AndroidKeyStore"

    @Synchronized
    fun get(): SecretKey {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (store.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    fun sealer(): Sealer = AesGcmSealer { get() }
}
