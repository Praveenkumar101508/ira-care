package care.ira.watch.ui

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Text
import care.ira.watch.R
import care.ira.watch.api.ApiOutcome
import care.ira.watch.BuildConfig
import care.ira.watch.pairing.PairingStore
import care.ira.watch.service.CareDependencies
import care.ira.watch.service.CareForegroundService
import care.ira.watch.service.SyncWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Three screens, one at a time, big text and two big buttons:
 *  pairing (until the watch has a token), permission (until background
 *  sensors are allowed), then home: the question when there is one,
 *  "I'm OK" and "Help".
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val deps = CareDependencies.get(this)
        if (deps.pairing.isPaired) {
            CareForegroundService.start(this)
            SyncWorker.schedule(this)
        }
        setContent {
            MaterialTheme {
                val status by deps.statusFlow().collectAsStateWithLifecycle()
                when {
                    !status.paired -> PairingScreen(deps)
                    !status.permissionGranted -> PermissionScreen(deps)
                    else -> HomeScreen(
                        question = status.question, listening = status.listening,
                        lastAnswer = status.lastAnswer, helpSent = status.helpSent, online = status.online,
                        onOk = { CareForegroundService.send(this, CareForegroundService.ACTION_OK) },
                        onHelp = { CareForegroundService.send(this, CareForegroundService.ACTION_HELP) },
                    )
                }
            }
        }
    }

    @Composable
    private fun PairingScreen(deps: CareDependencies) {
        var code by remember { mutableStateOf("") }
        var message by remember { mutableStateOf("") }
        val scope = rememberCoroutineScope()
        Column(
            modifier = Modifier.fillMaxSize().padding(12.dp),
            verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(getString(R.string.pair_title), fontSize = 16.sp, textAlign = TextAlign.Center)
            Spacer(Modifier.height(6.dp))
            Text(getString(R.string.pair_hint), fontSize = 12.sp, textAlign = TextAlign.Center)
            Spacer(Modifier.height(8.dp))
            androidx.compose.foundation.text.BasicTextField(
                value = code, onValueChange = { code = it }, singleLine = true,
                textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 12.sp),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Button(
                enabled = PairingStore.looksLikePairingCode(code),
                onClick = {
                    scope.launch {
                        message = "Pairing…"
                        val outcome = withContext(Dispatchers.IO) { deps.api.pair(code.trim(), BuildConfig.VERSION_NAME) }
                        message = when (outcome) {
                            is ApiOutcome.Ok -> {
                                deps.pairing.save(outcome.value.deviceToken, outcome.value.id, outcome.value.personId)
                                deps.status.value = deps.status.value.copy(paired = true)
                                CareForegroundService.start(this@MainActivity)
                                SyncWorker.schedule(this@MainActivity)
                                "Paired."
                            }
                            is ApiOutcome.Offline -> "No connection. Try again."
                            is ApiOutcome.Unauthorised, is ApiOutcome.Failed -> "That code was not recognised."
                        }
                    }
                },
            ) { Text(getString(R.string.pair_button)) }
            if (message.isNotEmpty()) Text(message, fontSize = 12.sp, textAlign = TextAlign.Center)
        }
    }

    @Composable
    private fun PermissionScreen(deps: CareDependencies) {
        val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            deps.status.value = deps.status.value.copy(permissionGranted = deps.sensors.backgroundPermissionGranted())
        }
        Column(
            modifier = Modifier.fillMaxSize().padding(12.dp),
            verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(getString(R.string.permission_title), fontSize = 16.sp, textAlign = TextAlign.Center)
            Spacer(Modifier.height(6.dp))
            Text(getString(R.string.permission_hint), fontSize = 12.sp, textAlign = TextAlign.Center)
            Spacer(Modifier.height(8.dp))
            Button(onClick = {
                launcher.launch(arrayOf(Manifest.permission.BODY_SENSORS, Manifest.permission.ACTIVITY_RECOGNITION,
                    Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS))
                // Background body sensors can only be granted from system settings.
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.fromParts("package", packageName, null)))
            }) { Text(getString(R.string.permission_button)) }
        }
    }
}

@Composable
fun HomeScreen(
    question: String?, listening: Boolean, lastAnswer: String?, helpSent: Boolean, online: Boolean,
    onOk: () -> Unit, onHelp: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(10.dp),
        verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = when {
                helpSent -> "Help is on its way."
                question != null -> question
                lastAnswer != null -> "Thank you."
                else -> "All well"
            },
            fontSize = 16.sp, textAlign = TextAlign.Center,
        )
        if (listening) Text("Listening…", fontSize = 12.sp, color = Color(0xFF9FD3A0))
        if (!online) Text("Offline: saving for later", fontSize = 10.sp, color = Color(0xFFE0C068))
        Spacer(Modifier.height(10.dp))
        Button(
            onClick = onOk, modifier = Modifier.fillMaxWidth().height(44.dp),
            colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF2E7D32)),
        ) { Text("I'm OK", fontSize = 16.sp) }
        Spacer(Modifier.height(8.dp))
        Button(
            onClick = onHelp, modifier = Modifier.fillMaxWidth().height(44.dp),
            colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFFC62828)),
        ) { Text("Help", fontSize = 16.sp) }
    }
}
