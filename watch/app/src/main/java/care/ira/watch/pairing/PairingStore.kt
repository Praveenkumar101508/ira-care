package care.ira.watch.pairing

import android.content.Context
import android.content.SharedPreferences

/**
 * The device token and ids from pairing, in the app's private preferences.
 * Android keeps app-private storage per app; a revoked token stops working
 * server-side at once, and clearing the app's data unpairs the watch.
 */
class PairingStore(private val prefs: SharedPreferences) {
    constructor(context: Context) : this(context.getSharedPreferences("care_pairing", Context.MODE_PRIVATE))

    val token: String? get() = prefs.getString(KEY_TOKEN, null)
    val deviceId: String? get() = prefs.getString(KEY_DEVICE, null)
    val personId: String? get() = prefs.getString(KEY_PERSON, null)
    val isPaired: Boolean get() = token != null

    fun save(token: String, deviceId: String, personId: String) {
        prefs.edit().putString(KEY_TOKEN, token).putString(KEY_DEVICE, deviceId).putString(KEY_PERSON, personId).apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
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
