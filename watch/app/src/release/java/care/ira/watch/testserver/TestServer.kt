package care.ira.watch.testserver

import android.content.Context
import androidx.compose.runtime.Composable
import care.ira.watch.BuildConfig

/**
 * Release builds: the care server's address is the one set at build time
 * (BuildConfig.CARE_API_BASE_URL, from IRA_CARE_API_BASE_URL; https) and nothing else. There is
 * no screen to type another and nothing stored is read. The debug source set's TestServer is
 * the test build's; ReleaseServerAddressTest and scripts/ci/check_watch_release_server.py keep
 * this one this way.
 */
object TestServer {
    const val EDITABLE = false

    @Suppress("UNUSED_PARAMETER")
    fun baseUrl(context: Context): String = BuildConfig.CARE_API_BASE_URL

    val addressScreen: (@Composable (onDone: () -> Unit) -> Unit)? = null
}
