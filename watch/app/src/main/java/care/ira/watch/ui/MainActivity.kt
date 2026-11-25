package care.ira.watch.ui

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.BackHandler
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
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.material.Text
import care.ira.watch.R
import care.ira.watch.api.ApiOutcome
import care.ira.watch.BuildConfig
import care.ira.watch.outbox.SosState
import care.ira.watch.checkin.ReminderButtons
import care.ira.watch.checkin.ReminderChoice
import care.ira.watch.outbox.VisitState
import care.ira.watch.pairing.PairingStore
import care.ira.watch.pairing.Unpairing
import care.ira.watch.service.CareDependencies
import care.ira.watch.service.CareForegroundService
import care.ira.watch.service.SyncWorker
import care.ira.watch.service.WatchStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Three screens, one at a time, big text and two big buttons:
 *  pairing (until the watch has a token), permission (until background
 *  sensors are allowed), then home: the question when there is one,
 *  "I'm OK" and "Help". From home, About shows the open-source licences.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val deps = CareDependencies.get(this)
        if (HomePolicy.backDoesNothing(BuildConfig.PLATFORM)) {
            onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() { /* the home screen: nowhere to go back to */ }
            })
        }
        if (deps.pairing.isPaired) {
            CareForegroundService.start(this)
            SyncWorker.schedule(this)
        }
        setContent {
            MaterialTheme {
                val status by deps.statusFlow().collectAsStateWithLifecycle()
                var unpairing by remember { mutableStateOf(false) }
                var about by remember { mutableStateOf(false) }
                BackHandler(enabled = about) { about = false }
                when {
                    about -> AboutScreen(onBack = { about = false })
                    !status.paired -> { unpairing = false; PairingScreen(deps) }
                    !status.permissionGranted -> PermissionScreen(deps)
                    unpairing -> UnpairScreen(deps, status, onKeep = { unpairing = false })
                    else -> HomeScreen(
                        onUnpair = { unpairing = true },
                        onAbout = { about = true },
                        question = status.question, listening = status.listening,
                        lastAnswer = status.lastAnswer, sos = status.sos, online = status.online, visit = status.visit,
                        fallCountdown = status.fallCountdown,
                        onOk = { CareForegroundService.send(this, CareForegroundService.ACTION_OK) },
                        onHelp = { CareForegroundService.send(this, CareForegroundService.ACTION_HELP) },
                        onTalk = { CareForegroundService.send(this, CareForegroundService.ACTION_TALK) },
                        talking = status.talking,
                        activityLine = status.activityLine,
                        reminderChoices = ReminderButtons.visible(status.reminderKind, status.fallCountdown,
                            status.sos !is SosState.Idle && status.sos !is SosState.Closed),
                        onChoice = { CareForegroundService.sendChoice(this, it) },
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

    /** Unpairing: refused while something urgent is unfinished, otherwise a plain warning and
     *  "Keep paired" first. The server is asked before anything changes here (Unpairing). */
    @Composable
    private fun UnpairScreen(deps: CareDependencies, status: WatchStatus, onKeep: () -> Unit) {
        val scope = rememberCoroutineScope()
        var message by remember { mutableStateOf("") }
        var busy by remember { mutableStateOf(false) }
        val check = Unpairing.check(status.urgentPending, status.sos, status.fallCountdown)
        Column(
            modifier = Modifier.fillMaxSize().padding(12.dp),
            verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            val text = when (check) {
                is Unpairing.Check.Refused -> check.reason
                Unpairing.Check.Allowed -> message.ifEmpty { Unpairing.warning(status.queued) }
            }
            Text(text, fontSize = 13.sp, textAlign = TextAlign.Center,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = onKeep,
                modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = "Keep paired. Nothing changes." },
                colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF2E7D32)),
            ) { Text("Keep paired", fontSize = 14.sp) }
            if (check is Unpairing.Check.Allowed) {
                Spacer(Modifier.height(6.dp))
                Button(
                    enabled = !busy,
                    onClick = {
                        busy = true
                        scope.launch {
                            val result = withContext(Dispatchers.IO) { deps.unpairWatch() }
                            busy = false
                            if (result is Unpairing.Result.NotNow) message = result.reason
                        }
                    },
                    modifier = Modifier.fillMaxWidth().height(44.dp).semantics {
                        contentDescription = "Unpair. IRA stops watching over you and your family and carers are told."
                    },
                    colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF6D4C41)),
                ) { Text(if (busy) "Unpairing…" else "Unpair", fontSize = 13.sp) }
            }
        }
    }

    /** About, Open-source licences: the notices this build carries (Notices), as a scrolling list. */
    @Composable
    private fun AboutScreen(onBack: () -> Unit) {
        val text = remember { runCatching { Notices.read { assets.open(it) } }.getOrElse { "The notices could not be read: ${it.message}" } }
        val paragraphs = remember(text) { Notices.paragraphs(text) }
        ScalingLazyColumn(modifier = Modifier.fillMaxSize().padding(horizontal = 8.dp)) {
            item {
                Text("Open-source licences", fontSize = 14.sp, textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth().semantics { heading() })
            }
            item {
                Button(
                    onClick = onBack,
                    modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = "Back to the watch face." },
                    colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF37474F)),
                ) { Text("Back", fontSize = 14.sp) }
            }
            items(paragraphs.size) { i -> Text(paragraphs[i], fontSize = 11.sp, color = Color(0xFFECEFF1)) }
        }
    }

    /** Permissions granted in Settings (or at first boot) take effect as soon as the person is
     *  back here, rather than leaving them on the permission screen until a restart. */
    override fun onResume() {
        super.onResume()
        val deps = CareDependencies.get(this)
        val granted = deps.sensors.backgroundPermissionGranted()
        if (deps.status.value.permissionGranted != granted) deps.status.value = deps.status.value.copy(permissionGranted = granted)
    }

    @Composable
    private fun PermissionScreen(deps: CareDependencies) {
        val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            val granted = deps.sensors.backgroundPermissionGranted()
            deps.status.value = deps.status.value.copy(permissionGranted = granted)
            // Background body sensors can only be allowed from system settings: go there only if
            // it is still missing after the dialog.
            if (HomePolicy.needsSettings(granted)) {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.fromParts("package", packageName, null)))
            }
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
                    Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS,
                    Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
            }) { Text(getString(R.string.permission_button)) }
        }
    }
}

@Composable
fun HomeScreen(
    question: String?, listening: Boolean, lastAnswer: String?, sos: SosState, online: Boolean,
    onOk: () -> Unit, onHelp: () -> Unit, fallCountdown: Int? = null,
    onTalk: () -> Unit = {}, talking: Boolean = false, visit: VisitState = VisitState.None,
    onUnpair: () -> Unit = {},
    onAbout: () -> Unit = {},
    reminderChoices: List<ReminderChoice> = emptyList(), onChoice: (ReminderChoice) -> Unit = {},
    activityLine: String? = null,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(10.dp),
        verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            // The SOS line is the server's word, never the watch's guess (SosState.text).
            text = FaceText.main(fallCountdown, sos.text(), question, lastAnswer, visit.text(), online),
            fontSize = 16.sp, textAlign = TextAlign.Center,
            // Read aloud by a screen reader as it changes: the question, the countdown, "help is on its way".
            modifier = Modifier.semantics { heading(); liveRegion = LiveRegionMode.Assertive },
        )
        if (listening) Text("Listening…", fontSize = 12.sp, color = Color(0xFF9FD3A0))
        if (!online) Text("Offline: saving for later", fontSize = 10.sp, color = Color(0xFFE0C068))
        // The wearer's own activity estimate: small, only when nothing else is happening, and said to be an estimate.
        if (activityLine != null && question == null && fallCountdown == null && sos is SosState.Idle)
            Text(activityLine, fontSize = 11.sp, textAlign = TextAlign.Center, color = Color(0xFFB0BEC5))
        Spacer(Modifier.height(10.dp))
        if (reminderChoices.isEmpty()) {
            Button(
                onClick = onOk,
                modifier = Modifier.fillMaxWidth().height(48.dp).semantics { contentDescription = "I'm OK. Answers the question, or says you are fine after a fall." },
                colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF2E7D32)),
            ) { Text("I'm OK", fontSize = 18.sp) }
        } else {
            // A reminder: its own buttons in place of "I'm OK" (ReminderButtons). Help stays below.
            reminderChoices.forEachIndexed { i, choice ->
                if (i > 0) Spacer(Modifier.height(6.dp))
                Button(
                    onClick = { onChoice(choice) },
                    modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = choice.description },
                    colors = ButtonDefaults.buttonColors(backgroundColor = if (choice == ReminderChoice.DONE) Color(0xFF2E7D32) else Color(0xFF37474F)),
                ) { Text(choice.label, fontSize = 16.sp) }
            }
        }
        Spacer(Modifier.height(8.dp))
        Button(
            onClick = onHelp,
            modifier = Modifier.fillMaxWidth().height(48.dp).semantics { contentDescription = "Help. Tells your family and carers you need them now." },
            colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFFC62828)),
        ) { Text("Help", fontSize = 18.sp) }
        if (question == null && fallCountdown == null) {
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = onTalk, enabled = !talking,
                modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = "Talk to IRA. Say anything; IRA listens and answers." },
                colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF37474F)),
            ) { Text(if (talking) "Talking…" else "Talk to IRA", fontSize = 14.sp) }
        }
        // Small and last, and only when nothing is happening: unpairing is rare and deliberate.
        if (question == null && fallCountdown == null && sos is SosState.Idle && !talking) {
            Spacer(Modifier.height(4.dp))
            Button(
                onClick = onUnpair,
                modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = "Unpair this watch. Asks before doing anything." },
                colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF263238)),
            ) { Text("Unpair watch…", fontSize = 12.sp, color = Color(0xFFCFD8DC)) }
            Spacer(Modifier.height(4.dp))
            Button(
                onClick = onAbout,
                modifier = Modifier.fillMaxWidth().height(44.dp).semantics { contentDescription = "About. The open-source licences of the software on this watch." },
                colors = ButtonDefaults.buttonColors(backgroundColor = Color(0xFF263238)),
            ) { Text("About", fontSize = 12.sp, color = Color(0xFFCFD8DC)) }
        }
    }
}
