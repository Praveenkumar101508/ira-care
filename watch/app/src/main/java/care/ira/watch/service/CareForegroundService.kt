package care.ira.watch.service

import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import care.ira.watch.CareApplication
import care.ira.watch.R
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.CheckinAnswer
import care.ira.watch.api.FallStart
import care.ira.watch.api.HelpRequest
import care.ira.watch.checkin.CheckinController
import care.ira.watch.checkin.FallCountdown
import care.ira.watch.sensors.FallDetector
import care.ira.watch.sensors.FallEvent
import care.ira.watch.sensors.FallSettings
import care.ira.watch.sensors.ReadingKinds
import care.ira.watch.ui.MainActivity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * IRA, all day. A foreground service of type `health` that:
 *
 *  * collects readings from the sensor source into the offline queue;
 *  * every `POLL_MS` asks the backend whether a check-in is due, speaks it,
 *    listens for the answer, repeats it louder with vibration, and reports the
 *    answer or the miss (the timings are the backend's; see CheckinController);
 *  * syncs the queue after every check-in and whenever it wakes.
 *
 * The screen (MainActivity) talks to it through `CareDependencies.status` and
 * the two intents below, so "I'm OK" and "Help" work with the app in the
 * foreground or not.
 */
class CareForegroundService : LifecycleService() {
    private lateinit var deps: CareDependencies
    private lateinit var controller: CheckinController
    private lateinit var fallCountdown: FallCountdown
    private var loop: Job? = null
    private var listening: Job? = null

    override fun onCreate() {
        super.onCreate()
        deps = CareDependencies.get(this)
        controller = CheckinController(deps.clock)
        fallCountdown = FallCountdown(deps.clock)
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH)
        lifecycleScope.launch(Dispatchers.IO) { collectReadings() }
        lifecycleScope.launch(Dispatchers.IO) { watchForFalls() }
        loop = lifecycleScope.launch(Dispatchers.IO) { mainLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_OK -> lifecycleScope.launch(Dispatchers.IO) {
                if (fallCountdown.state is FallCountdown.State.Counting) performFall(fallCountdown.okPressed())
                else perform(controller.answered("", "button"))
            }
            ACTION_HELP -> lifecycleScope.launch(Dispatchers.IO) {
                if (fallCountdown.state is FallCountdown.State.Counting) performFall(fallCountdown.helpPressed())
                else perform(controller.helpPressed())
            }
            ACTION_SYNC -> lifecycleScope.launch(Dispatchers.IO) { syncNow() }
            ACTION_TALK -> lifecycleScope.launch(Dispatchers.IO) { talk() }
        }
        return START_STICKY
    }

    private suspend fun collectReadings() {
        deps.sensors.readings().onEach { reading ->
            if (reading.kind == ReadingKinds.ON_WRIST) deps.lastOnWrist = reading.value >= 0.5
            deps.queue.append(listOf(reading))
            deps.status.value = deps.status.value.copy(queued = deps.queue.size())
        }.collect()
    }

    /**
     * The fall detector runs on the accelerometer with the carer's thresholds.
     * Off when the carer turned it off. A suspected fall starts the countdown:
     * vibrate, ask, big buttons; the server is told at once and keeps its own
     * deadline.
     */
    private suspend fun watchForFalls() {
        var detector = FallDetector(fallSettings())
        var configured = fallSettings()
        deps.accelerometer.samples().onEach { sample ->
            val wanted = fallSettings()
            if (wanted != configured) { configured = wanted; detector = FallDetector(wanted) }
            if (!deps.fallSettings.enabled) return@onEach
            detector.onWrist = deps.lastOnWrist
            when (val event = detector.feed(sample)) {
                is FallEvent.SuspectedFall ->
                    performFall(fallCountdown.suspectedFall(event.peakG, event.stillForSeconds, deps.fallSettings.countdownS))
                else -> {}
            }
        }.collect()
    }

    private fun fallSettings(): FallSettings = deps.fallSettings.let {
        FallSettings(impactG = it.impactG, stillSeconds = it.stillSeconds, stillThresholdG = it.stillThresholdG)
    }

    private suspend fun performFall(actions: List<FallCountdown.Action>) {
        for (action in actions) {
            when (action) {
                FallCountdown.Action.Vibrate -> vibrate()
                is FallCountdown.Action.Speak -> deps.speaker.speak(action.text, louder = true)
                is FallCountdown.Action.ReportFallStarted ->
                    deps.api.fallStarted(FallStart(action.peakG, action.stillSeconds))
                FallCountdown.Action.ReportFallOk -> {
                    deps.api.fallOk()
                    deps.speaker.speak("Good. I will leave you be.", louder = false)
                }
                FallCountdown.Action.RequestHelp -> perform(listOf(CheckinController.Action.RequestHelp("fall")))
            }
        }
        deps.status.value = deps.status.value.copy(fallCountdown = fallCountdown.secondsLeft)
    }

    private suspend fun mainLoop() {
        var lastPoll = 0L
        while (currentCoroutineIsActive()) {
            if (!deps.pairing.isPaired) { delay(POLL_MS); continue }
            val now = deps.clock.nowMillis()
            if (fallCountdown.state is FallCountdown.State.Counting) performFall(fallCountdown.tick())
            perform(controller.tick())
            if (controller.openCheckinId() == null && now - lastPoll >= POLL_MS) {
                lastPoll = now
                when (val due = deps.api.dueCheckin()) {
                    is ApiOutcome.Ok -> {
                        deps.status.value = deps.status.value.copy(online = true)
                        due.value?.let { perform(controller.ask(it.id, it.questionText, it.safety)) }
                    }
                    is ApiOutcome.Unauthorised -> deps.onUnauthorised()
                    is ApiOutcome.Offline -> deps.status.value = deps.status.value.copy(online = false)
                    is ApiOutcome.Failed -> {}
                }
            }
            delay(TICK_MS)
        }
    }

    private suspend fun currentCoroutineIsActive(): Boolean = kotlinx.coroutines.currentCoroutineContext().isActive

    private suspend fun perform(actions: List<CheckinController.Action>) {
        for (action in actions) {
            when (action) {
                is CheckinController.Action.Speak -> {
                    deps.status.value = deps.status.value.copy(question = action.text, helpSent = false)
                    deps.speaker.speak(action.text, action.louder)
                }
                CheckinController.Action.Vibrate -> vibrate()
                CheckinController.Action.Listen -> startListening()
                is CheckinController.Action.ReportRepeated -> deps.api.repeated(action.checkinId)
                is CheckinController.Action.ReportAnswer -> {
                    listening?.cancel()
                    deps.api.answer(action.checkinId, CheckinAnswer(action.text, action.via))
                    deps.status.value = deps.status.value.copy(question = null, listening = false,
                        lastAnswer = action.text.ifBlank { getString(R.string.im_ok) })
                    controller.reset()
                    syncNow()
                }
                is CheckinController.Action.ReportMissed -> {
                    listening?.cancel()
                    deps.api.missed(action.checkinId)
                    deps.status.value = deps.status.value.copy(question = null, listening = false)
                    controller.reset()
                    syncNow()   // the server's sensor check needs the latest readings
                }
                is CheckinController.Action.RequestHelp -> {
                    // Tell the person first; the fix can take a few seconds and must not delay that.
                    deps.speaker.speak("I am getting help for you now. Stay where you are.", louder = true)
                    val fix = deps.location.currentFix()   // null when unavailable: the server says so, never guesses
                    deps.api.help(HelpRequest(via = action.via, location = fix))
                    deps.status.value = deps.status.value.copy(helpSent = true)
                }
                CheckinController.Action.Nothing -> {}
            }
        }
    }

    /**
     * Talk to IRA: listen, send the words, speak each sentence as the server
     * streams it. A call action is spoken (the phone dials; the watch does not).
     * A safety-check action means IRA wants to ask "are you OK?" right now.
     */
    private suspend fun talk() {
        if (controller.openCheckinId() != null || fallCountdown.state is FallCountdown.State.Counting) return
        deps.speaker.speak("I'm listening.", louder = false)
        deps.status.value = deps.status.value.copy(listening = true, talking = true)
        val heard = deps.listener.listen(LISTEN_MS)
        deps.status.value = deps.status.value.copy(listening = false)
        if (heard.isNullOrBlank()) {
            deps.speaker.speak("I didn't catch that. Press Talk and try again.", louder = false)
            deps.status.value = deps.status.value.copy(talking = false)
            return
        }
        when (val out = deps.api.talk(heard) { sentence ->
            deps.status.value = deps.status.value.copy(question = sentence)
            kotlinx.coroutines.runBlocking { deps.speaker.speak(sentence, louder = false) }
        }) {
            is ApiOutcome.Ok -> {
                val kind = (out.value.action?.get("kind") as? kotlinx.serialization.json.JsonPrimitive)?.content
                if (kind == "safety_check") {
                    when (val due = deps.api.dueCheckin()) {
                        is ApiOutcome.Ok -> due.value?.let { perform(controller.ask(it.id, it.questionText, it.safety)) }
                        else -> {}
                    }
                }
            }
            is ApiOutcome.Offline -> deps.speaker.speak("I can't reach anyone right now. I'll keep trying.", louder = false)
            is ApiOutcome.Unauthorised -> deps.onUnauthorised()
            is ApiOutcome.Failed -> deps.speaker.speak("Something went wrong. Let's try again later.", louder = false)
        }
        deps.status.value = deps.status.value.copy(talking = false, question = null)
    }

    private fun startListening() {
        listening?.cancel()
        listening = lifecycleScope.launch(Dispatchers.IO) {
            deps.status.value = deps.status.value.copy(listening = true)
            val heard = deps.listener.listen(LISTEN_MS)
            deps.status.value = deps.status.value.copy(listening = false)
            if (!heard.isNullOrBlank()) perform(controller.answered(heard, "voice"))
        }
    }

    private suspend fun syncNow() = withContext(Dispatchers.IO) {
        deps.sensors.snapshot().takeIf { it.isNotEmpty() }?.let { deps.queue.append(it) }
        val result = deps.sync.syncOnce()
        deps.status.value = deps.status.value.copy(queued = deps.queue.size(), online = result != SyncResult.Offline)
    }

    private fun vibrate() {
        val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)
        }
        vibrator.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 400, 200, 400, 200, 600), -1))
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CareApplication.CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    override fun onDestroy() {
        loop?.cancel()
        listening?.cancel()
        deps.speaker.shutdown()
        deps.listener.shutdown()
        super.onDestroy()
    }

    companion object {
        const val NOTIFICATION_ID = 1
        const val ACTION_OK = "care.ira.watch.OK"
        const val ACTION_HELP = "care.ira.watch.HELP"
        const val ACTION_SYNC = "care.ira.watch.SYNC"
        const val ACTION_TALK = "care.ira.watch.TALK"
        const val POLL_MS = 60_000L
        const val TICK_MS = 5_000L
        const val LISTEN_MS = 20_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, CareForegroundService::class.java))
        }

        fun send(context: Context, action: String) {
            context.startForegroundService(Intent(context, CareForegroundService::class.java).setAction(action))
        }
    }
}
