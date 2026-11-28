package care.ira.watch.testserver

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.Text
import care.ira.watch.BuildConfig
import care.ira.watch.api.ServerAddress

/**
 * Debug builds only: the care server's address for a test on a real watch, typed on the watch
 * before pairing and kept in the app's private preferences. A real watch cannot reach the
 * emulator's 10.0.2.2, so the stand-in device test (docs/evidence/device/GALAXY_WATCH4_TEST.md)
 * types the HTTPS address the "watch device test server (manual)" workflow prints.
 *
 * Only an address [ServerAddress.check] accepts is kept or used: https, or plain http to the
 * emulator's 10.0.2.2. A stored value that fails the check (edited by hand) is ignored and the
 * build's own address is used. The release source set has its own TestServer with no screen and
 * no storage: release always uses BuildConfig.CARE_API_BASE_URL (ReleaseServerAddressTest).
 */
object TestServer {
    const val EDITABLE = true
    private const val PREFS = "care_test_server"
    private const val KEY = "base_url"

    /** The address the watch talks to. */
    fun baseUrl(context: Context): String = resolve(stored(context), BuildConfig.CARE_API_BASE_URL)

    /** A stored address if it still passes the check, otherwise the build's own. */
    fun resolve(stored: String?, buildDefault: String): String =
        (stored?.let { ServerAddress.check(it) } as? ServerAddress.Check.Ok)?.url ?: buildDefault

    fun stored(context: Context): String? = prefs(context).getString(KEY, null)

    /** Keeps the address only when the check accepts it; returns the check either way. */
    fun save(context: Context, text: String): ServerAddress.Check {
        val check = ServerAddress.check(text)
        if (check is ServerAddress.Check.Ok) prefs(context).edit().putString(KEY, check.url).commit()
        return check
    }

    fun clear(context: Context) {
        prefs(context).edit().remove(KEY).commit()
    }

    /** The screen MainActivity shows from the pairing screen. Null in release builds. */
    val addressScreen: (@Composable (onDone: () -> Unit) -> Unit)? = { onDone -> TestServerScreen(onDone) }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}

@Composable
private fun TestServerScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    var text by remember { mutableStateOf(TestServer.stored(context) ?: "https://") }
    var message by remember { mutableStateOf("Now: " + TestServer.baseUrl(context)) }
    Column(
        modifier = Modifier.fillMaxSize().padding(14.dp),
        verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Test server", fontSize = 15.sp, textAlign = TextAlign.Center, modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(4.dp))
        Text("Test build only. Type the https:// address.", fontSize = 11.sp, textAlign = TextAlign.Center)
        Spacer(Modifier.height(6.dp))
        BasicTextField(
            value = text, onValueChange = { text = it }, singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false),
            textStyle = TextStyle(color = Color.White, fontSize = 11.sp),
            modifier = Modifier.fillMaxWidth().semantics { contentDescription = "Test server address" },
        )
        Spacer(Modifier.height(6.dp))
        Button(
            onClick = {
                when (val check = TestServer.save(context, text)) {
                    is ServerAddress.Check.Ok -> { message = "Saved."; onDone() }
                    is ServerAddress.Check.Refused -> message = check.reason
                }
            },
            modifier = Modifier.fillMaxWidth().height(40.dp).semantics { contentDescription = "Save the test server address" },
        ) { Text("Save", fontSize = 13.sp) }
        Spacer(Modifier.height(4.dp))
        Button(
            onClick = onDone,
            colors = ButtonDefaults.secondaryButtonColors(),
            modifier = Modifier.fillMaxWidth().height(36.dp).semantics { contentDescription = "Back to pairing. Nothing changes." },
        ) { Text("Back", fontSize = 12.sp) }
        Text(message, fontSize = 10.sp, textAlign = TextAlign.Center,
            modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
    }
}
