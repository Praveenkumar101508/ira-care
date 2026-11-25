package care.ira.watch.service

import android.app.Notification
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import care.ira.watch.CareApplication
import care.ira.watch.R
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.CareApi
import care.ira.watch.api.CheckinAnswer
import care.ira.watch.api.FallStart
import care.ira.watch.api.HelpRequest
import care.ira.watch.outbox.Outbox
import care.ira.watch.outbox.SosState
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
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
    private val json = Json { explicitNulls = false }

    override fun onCreate() {
        super.onCreate()
        deps = CareDependencies.get(this)
        controller = CheckinController(deps.clock)
        fallCountdown = FallCountdown(deps.clock)
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH)
        lifecycleScope.launch(Dispatchers.IO) { collectReadings() }
        lifecycleScope.launch(Dispatchers.IO) { watchForFalls() }
        deps.activity?.let { monitor ->
            lifecycleScope.launch(Dispatchers.Default) { watchActivity(monitor) }
        }
        lifecycleScope.launch(Dispatchers.IO) { followSosState() }
        loop = lifecycleScope.launch(Dispatchers.IO) { mainLoop() }
        // Anything still in the outbox from before a restart or a reboot goes first.
        deps.status.value = deps.status.value.copy(urgentPending = deps.outbox.size())
        if (deps.outbox.size() > 0) lifecycleScope.launch(Dispatchers.IO) { syncNow() }
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

    /**
     * Every sample is queued with its own sample time. A heart rate outside the
     * carer's range (held on the watch from `/devices/me`) is uploaded at once,
     * not at the next interval: the server's two-in-a-row rule then decides.
     */
    private suspend fun collectReadings() {
        deps.sensors.readings().onEach { reading ->
            if (reading.kind == ReadingKinds.ON_WRIST) deps.lastOnWrist = reading.value >= 0.5
            deps.queue.append(listOf(reading))
            deps.status.value = deps.status.value.copy(queued = deps.queue.size())
            if (reading.kind == ReadingKinds.HEART_RATE && deps.readingSettings.outOfRange(reading.value)) syncNow()
        }.collect()
    }

    /**
     * The server pushes the SOS state (received, "[Name] is on the way",
     * "[Name] has arrived", closed) over a held connection; the watch reconnects
     * when it closes, and backs off while offline.
     */
    private suspend fun followSosState() {
        var wait = 2_000L
        while (currentCoroutineIsActive()) {
            if (!deps.pairing.isPaired) { delay(POLL_MS); continue }
            deps.onCarerArrived = { id -> vibrate(); if (id != null) deps.api.acknowledgeVisit(id) }
            when (val out = deps.channel.run { deps.applyServerSos(it) }) {
                is ApiOutcome.Ok -> wait = 2_000L
                is ApiOutcome.Unauthorised -> deps.onUnauthorised()
                is ApiOutcome.Offline, is ApiOutcome.Failed -> { deps.status.value = deps.status.value.copy(online = out !is ApiOutcome.Offline); wait = minOf(wait * 2, 60_000L) }
            }
            delay(wait)
        }
    }

    /**
     * The fall detector runs on the accelerometer with the carer's thresholds.
     * Off when the carer turned it off. A suspected fall starts the countdown:
     * vibrate, ask, big buttons; the server is told at once and keeps its own
     * deadline.
     */
    private suspend fun watchForFalls() {
        var configured = fallSettings()
        var detector = newFallDetector(configured)
        deps.accelerometer.samples().onEach { sample ->
            val wanted = fallSettings()
            if (wanted != configured) { configured = wanted; detector = newFallDetector(wanted) }
            if (!deps.fallSettings.enabled) return@onEach
            detector.onWrist = deps.lastOnWrist
            when (val event = detector.feed(sample)) {
                is FallEvent.SuspectedFall ->
                    performFall(fallCountdown.suspectedFall(event.peakG, event.stillForSeconds, deps.fallSettings.countdownS))
                else -> {}
            }
        }.collect()
    }

    /**
     * IRA Care Activity on the watch's own motion: the accelerometer (shared with the fall
     * detector's source) and the gyroscope, classified every 10 s. The answer stays on the
     * watch; nothing is sent and nothing alerts on it.
     */
    private suspend fun watchActivity(monitor: care.ira.watch.activity.ActivityMonitor) = kotlinx.coroutines.coroutineScope {
        launch {
            deps.accelerometer.samples().onEach { s ->
                monitor.onWrist = deps.lastOnWrist
                monitor.feedAccelG(s.atMillis, s.x, s.y, s.z)
            }.collect()
        }
        launch { deps.gyroscope.samples().onEach { s -> monitor.feedGyro(s.atMillis, s.x, s.y, s.z) }.collect() }
    }

    /** The trained model behind the detector interface when the build carries it; the rules alone otherwise. */
    private fun newFallDetector(settings: FallSettings): FallDetecting {
        val model = deps.fallModel ?: return RuleOnly(FallDetector(settings))
        return ModelBacked(care.ira.watch.sensors.ModelFallDetector(settings, model.second, model.first))
    }

    private interface FallDetecting { var onWrist: Boolean?; fun feed(sample: care.ira.watch.sensors.AccelSample): FallEvent? }
    private class RuleOnly(private val d: FallDetector) : FallDetecting {
        override var onWrist: Boolean? get() = d.onWrist; set(v) { d.onWrist = v }
        override fun feed(sample: care.ira.watch.sensors.AccelSample) = d.feed(sample)
    }
    private class ModelBacked(private val d: care.ira.watch.sensors.ModelFallDetector) : FallDetecting {
        override var onWrist: Boolean? get() = d.onWrist; set(v) { d.onWrist = v }
        override fun feed(sample: care.ira.watch.sensors.AccelSample) = d.feed(sample)
    }

    private fun fallSettings(): FallSettings = deps.fallSettings.let {
        FallSettings(impactG = it.impactG, stillSeconds = it.stillSeconds, stillThresholdG = it.stillThresholdG)
    }

    private suspend fun performFall(actions: List<FallCountdown.Action>) {
        for (action in actions) {
            when (action) {
                FallCountdown.Action.Vibrate -> vibrate()
                is FallCountdown.Action.Speak -> deps.speaker.speak(action.text, louder = true)
                is FallCountdown.Action.ReportFallStarted -> {
                    // Through the outbox: it reaches the server even if the link is down right now.
                    deps.outbox.enqueue(Outbox.KIND_FALL_STARTED, CareApi.PATH_FALL, json.encodeToString(FallStart(action.peakG, action.stillSeconds)))
                    deps.status.value = deps.status.value.copy(sos = SosState.Sending, urgentPending = deps.outbox.size())
                    deps.sync.drainOutbox()
                }
                FallCountdown.Action.ReportFallOk -> {
                    deps.outbox.enqueue(Outbox.KIND_FALL_OK, CareApi.PATH_FALL_OK, "{}")
                    deps.status.value = deps.status.value.copy(sos = SosState.Idle, urgentPending = deps.outbox.size())
                    deps.sync.drainOutbox()
                    deps.speaker.speak("Good. I will leave you be.", louder = false)
                }
                FallCountdown.Action.RequestHelp -> perform(listOf(CheckinController.Action.RequestHelp("fall")))
            }
        }
        deps.status.value = deps.status.value.copy(fallCountdown = fallCountdown.secondsLeft)
    }

    private suspend fun mainLoop() {
        var lastPoll = 0L
        var lastUpload = 0L
        while (currentCoroutineIsActive()) {
            if (!deps.pairing.isPaired) { delay(POLL_MS); continue }
            val now = deps.clock.nowMillis()
            // Ordinary readings go up every `reading_upload_interval_s` (60 s by default, the carer's setting).
            if (now - lastUpload >= deps.readingSettings.uploadIntervalS * 1000L) { lastUpload = now; syncNow() }
            controller.configure(deps.profile.repeatAfterS, deps.profile.repeats)
            if (fallCountdown.state is FallCountdown.State.Counting) {
                performFall(fallCountdown.tick())
                listenDuringFall()
            }
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
                    deps.status.value = deps.status.value.copy(question = action.text)
                    // Voice off in the person's profile: the words stay on the screen; a louder (safety) question is still spoken.
                    if (deps.profile.voice || action.louder) deps.speaker.speak(action.text, action.louder)
                }
                CheckinController.Action.Vibrate -> if (deps.profile.vibration) vibrate()
                CheckinController.Action.Listen -> startListening()
                is CheckinController.Action.ReportRepeated -> deps.api.repeated(action.checkinId)
                is CheckinController.Action.ReportAnswer -> {
                    listening?.cancel()
                    // Answers are urgent too (an "I'm OK" to a safety question stops an SOS): outbox, then sync.
                    deps.outbox.enqueue(Outbox.KIND_ANSWER, CareApi.pathAnswer(action.checkinId), json.encodeToString(CheckinAnswer(action.text, action.via)))
                    deps.status.value = deps.status.value.copy(question = null, listening = false,
                        lastAnswer = action.text.ifBlank { getString(R.string.im_ok) }, urgentPending = deps.outbox.size())
                    controller.reset()
                    syncNow()
                }
                is CheckinController.Action.ReportMissed -> {
                    listening?.cancel()
                    deps.outbox.enqueue(Outbox.KIND_MISSED, CareApi.pathMissed(action.checkinId), "{}")
                    deps.status.value = deps.status.value.copy(question = null, listening = false, urgentPending = deps.outbox.size())
                    controller.reset()
                    syncNow()   // the server's sensor check needs the latest readings
                }
                is CheckinController.Action.RequestHelp -> {
                    // Written to the outbox first, so a kill or a reboot cannot lose it; then
                    // the person is told the truth: it is being sent, not that help is coming.
                    // Nothing slow comes before the outbox: a location fix waits at most
                    // HELP_FIX_MS (null then: the server says "location unknown", never guesses),
                    // and IRA speaks only after the request is written down and being sent.
                    // Speaking first let a slow or stuck speech engine hold the request back.
                    deps.status.value = deps.status.value.copy(sos = SosState.Sending)
                    val fix = deps.location.currentFix(HELP_FIX_MS)
                    deps.outbox.enqueue(Outbox.KIND_HELP, CareApi.PATH_HELP, json.encodeToString(HelpRequest(via = action.via, location = fix)))
                    deps.status.value = deps.status.value.copy(urgentPending = deps.outbox.size())
                    deps.sync.drainOutbox()
                    deps.speaker.speak("Sending your request for help. Stay where you are.", louder = true)
                    deps.status.value.sos.text()?.let { deps.speaker.speak(it, louder = true) }
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

    /** During a fall countdown the watch also listens: "I'm OK" ends it, "help" asks now (care/personal, phase 7). */
    private fun listenDuringFall() {
        if (listening?.isActive == true) return
        listening = lifecycleScope.launch(Dispatchers.IO) {
            val heard = deps.listener.listen(FALL_LISTEN_MS)
            if (fallCountdown.state is FallCountdown.State.Counting) performFall(fallCountdown.heard(heard))
        }
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

    private fun vibrate() = deps.haptics.pattern()

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
        /** How long a Help request waits for a location fix before it is written to the outbox. */
        const val HELP_FIX_MS = 2_000L
        const val NOTIFICATION_ID = 1
        const val ACTION_OK = "care.ira.watch.OK"
        const val ACTION_HELP = "care.ira.watch.HELP"
        const val ACTION_SYNC = "care.ira.watch.SYNC"
        const val ACTION_TALK = "care.ira.watch.TALK"
        const val POLL_MS = 60_000L
        const val TICK_MS = 5_000L
        const val FALL_LISTEN_MS = 4_000L
        const val LISTEN_MS = 20_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, CareForegroundService::class.java))
        }

        fun send(context: Context, action: String) {
            context.startForegroundService(Intent(context, CareForegroundService::class.java).setAction(action))
        }
    }
}
