package care.ira.watch.pairing

import android.content.Context
import android.content.SharedPreferences
import care.ira.watch.storage.LineCodec
import care.ira.watch.storage.Sealer

/**
 * The device token and ids from pairing, in the app's private preferences, each value sealed
 * with the watch's Keystore key (care/ira/watch/storage). A value stored in plain text by an
 * earlier version is still read, and sealed again at once. Android keeps app-private storage
 * per app and the app is excluded from backup; a revoked token stops working server-side at
 * once, and clearing the app's data unpairs the watch.
 */
class PairingStore(private val prefs: SharedPreferences, private val sealer: Sealer? = null) {
    constructor(context: Context, sealer: Sealer? = null) :
        this(context.getSharedPreferences("care_pairing", Context.MODE_PRIVATE), sealer)

    val token: String? get() = read(KEY_TOKEN)
    val deviceId: String? get() = read(KEY_DEVICE)
    val personId: String? get() = read(KEY_PERSON)
    val isPaired: Boolean get() = token != null

    fun save(token: String, deviceId: String, personId: String) {
        // commit(), not apply(): the pairing is on disk before the watch acts on it.
        prefs.edit().putString(KEY_TOKEN, LineCodec.encode(sealer, token))
            .putString(KEY_DEVICE, LineCodec.encode(sealer, deviceId))
            .putString(KEY_PERSON, LineCodec.encode(sealer, personId)).commit()
    }

    fun clear() {
        prefs.edit().clear().commit()
    }

    private fun read(key: String): String? {
        val raw = prefs.getString(key, null) ?: return null
        if (raw.startsWith("s1:") || raw.startsWith("p1:")) return LineCodec.decode(sealer, raw)
        // Plain text from before sealing: read it, and seal it now.
        if (sealer != null) prefs.edit().putString(key, LineCodec.encode(sealer, raw)).commit()
        return raw
    }

    companion object {
        private const val KEY_TOKEN = "device_token"
        private const val KEY_DEVICE = "device_id"
        private const val KEY_PERSON = "person_id"

        /** Pairing codes look like `cp1.<scope>.<SECRET>`: three dot-separated parts. */
        fun looksLikePairingCode(text: String): Boolean {
            val parts = text.trim().split(".")
            return parts.size == 3 && parts[0] == "cp1" && parts[1].isNotEmpty() && parts[2].length >= 6
        }
    }
}
