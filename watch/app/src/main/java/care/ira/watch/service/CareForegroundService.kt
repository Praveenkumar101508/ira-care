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
import care.ira.watch.api.HelpRequest
import care.ira.watch.checkin.CheckinController
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
    private var loop: Job? = null
    private var listening: Job? = null

    override fun onCreate() {
        super.onCreate()
        deps = CareDependencies.get(this)
        controller = CheckinController(deps.clock)
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH)
        lifecycleScope.launch(Dispatchers.IO) { collectReadings() }
        loop = lifecycleScope.launch(Dispatchers.IO) { mainLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_OK -> lifecycleScope.launch(Dispatchers.IO) { perform(controller.answered("", "button")) }
            ACTION_HELP -> lifecycleScope.launch(Dispatchers.IO) { perform(controller.helpPressed()) }
            ACTION_SYNC -> lifecycleScope.launch(Dispatchers.IO) { syncNow() }
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

    private suspend fun mainLoop() {
        var lastPoll = 0L
        while (currentCoroutineIsActive()) {
            if (!deps.pairing.isPaired) { delay(POLL_MS); continue }
            val now = deps.clock.nowMillis()
            perform(controller.tick())
            if (controller.openCheckinId() == null && now - lastPoll >= POLL_MS) {
                lastPoll = now
                when (val due = deps.api.dueCheckin()) {
                    is ApiOutcome.Ok -> {
                        deps.status.value = deps.status.value.copy(online = true)
                        due.value?.let { perform(controller.ask(it.id, it.questionText)) }
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
                    deps.api.help(HelpRequest(via = action.via))
                    deps.status.value = deps.status.value.copy(helpSent = true)
                    deps.speaker.speak("I am getting help for you now. Stay where you are.", louder = true)
                }
                CheckinController.Action.Nothing -> {}
            }
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
