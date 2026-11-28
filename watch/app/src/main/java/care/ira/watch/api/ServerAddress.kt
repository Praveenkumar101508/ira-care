package care.ira.watch.api

import java.net.URI
import java.net.URISyntaxException

/**
 * The rule for a care server address typed on a test build's watch (testserver/TestServer in
 * `src/debug`). Only `https://host[:port]` is taken, with nothing else in it: no user name or
 * password, no path, no query. Plain `http://` is taken for the emulator's address for the host
 * machine (10.0.2.2) and nothing else, which is also the only cleartext host the debug network
 * security config allows besides the `adb reverse` loopback names, so a typed address can never
 * send the device token in the clear across a real network.
 *
 * Release builds never read a typed address: their address is fixed at build time
 * (`CARE_API_BASE_URL`, https), and `src/release` has no screen to type one.
 */
object ServerAddress {
    /** The Android emulator's address for the machine it runs on. */
    const val EMULATOR_HOST = "10.0.2.2"

    sealed interface Check {
        /** The address, normalised: lower-case scheme and host, no trailing slash. */
        data class Ok(val url: String) : Check
        data class Refused(val reason: String) : Check
    }

    fun check(text: String): Check {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return Check.Refused("Type the test server's https:// address.")
        val uri = try { URI(trimmed) } catch (e: URISyntaxException) { return Check.Refused("That is not an address.") }
        val scheme = uri.scheme?.lowercase() ?: return Check.Refused("Start the address with https://")
        val host = uri.host?.lowercase()?.takeIf { it.isNotEmpty() } ?: return Check.Refused("That address has no server name.")
        if (uri.rawUserInfo != null) return Check.Refused("No user name or password in the address.")
        if (uri.rawQuery != null || uri.rawFragment != null) return Check.Refused("Only the server's address, nothing after it.")
        val path = uri.rawPath ?: ""
        if (path.isNotEmpty() && path != "/") return Check.Refused("Only the server's address, nothing after it.")
        val port = uri.port
        if (port != -1 && port !in 1..65535) return Check.Refused("That port is not valid.")
        when (scheme) {
            "https" -> Unit
            "http" -> if (host != EMULATOR_HOST) return Check.Refused("Use an https:// address.")
            else -> return Check.Refused("Use an https:// address.")
        }
        return Check.Ok("$scheme://$host" + (if (port == -1) "" else ":$port"))
    }
}
