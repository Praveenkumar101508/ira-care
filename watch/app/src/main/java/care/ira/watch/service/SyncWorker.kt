package care.ira.watch.service

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.DeviceReport
import care.ira.watch.outbox.SosState
import care.ira.watch.outbox.UrgentEvent
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Sends what the offline queue holds, every 15 minutes (the shortest period
 * WorkManager allows) and whenever the service asks. The foreground service
 * also syncs on its own after every check-in; this worker is the safety net
 * that runs even if the service was killed.
 */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val deps = CareDependencies.get(applicationContext)
        if (!deps.pairing.isPaired) return@withContext Result.success()
        when (deps.sync.syncOnce()) {
            SyncResult.Done, SyncResult.NothingToSend -> Result.success()
            SyncResult.Offline -> Result.retry()
            SyncResult.Unauthorised -> Result.success()   // the service shows the pairing screen
        }
    }

    companion object {
        const val NAME = "care_sync"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}

enum class SyncResult { Done, NothingToSend, Offline, Unauthorised }

/**
 * Drains the outbox first (Help, falls, answers: each with its idempotency key,
 * removed only once the server has answered), then the reading queue into
 * `/devices/me/report`, batch by batch.
 */
class Syncer(private val deps: CareDependencies) {
    /** The carer may have changed the fall settings, the heart-rate range or the upload interval. */
    private fun refreshSettings() {
        when (val me = deps.api.me()) {
            is ApiOutcome.Ok -> {
                deps.fallSettings = me.value.fall
                deps.readingSettings = me.value.readings
                deps.profile = me.value.profile
                deps.speaker.setRate(me.value.profile.speechRate)
                deps.applyServerSos(me.value.sos)
            }
            else -> {}   // keep the last known settings; the defaults until the first answer
        }
    }

    /**
     * Send every due urgent event, oldest first. Each is removed on any definite
     * answer from the server (success or a refusal such as 404 or 409: the server
     * remembers refusals under the key too). It stays, with a longer wait, when
     * the server could not be reached. Returns false when the link is down.
     */
    fun drainOutbox(): Boolean {
        for (event in deps.outbox.due()) {
            when (val out = deps.api.sendUrgent(event.path, event.body, event.idempotencyKey)) {
                is ApiOutcome.Ok -> {
                    deps.outbox.complete(event.idempotencyKey)
                    deps.onUrgentDelivered(event, out.value)
                }
                is ApiOutcome.Failed -> {
                    deps.outbox.complete(event.idempotencyKey)
                    deps.onUrgentRefused(event, out.status)
                }
                is ApiOutcome.Offline -> {
                    deps.outbox.failed(event.idempotencyKey, out.reason)
                    deps.onUrgentWaiting(event)
                    return false
                }
                is ApiOutcome.Unauthorised -> { deps.onUnauthorised(); return false }
            }
        }
        return true
    }

    fun syncOnce(): SyncResult {
        if (!drainOutbox()) return SyncResult.Offline
        refreshSettings()
        var sentAnything = false
        while (true) {
            val batch = deps.queue.peek(500)
            val state = deps.deviceState()
            if (batch.isEmpty() && !sentAnything) {
                // Nothing queued: still tell the server the watch is alive and its state.
                return when (deps.api.report(DeviceReport(emptyList(), state.battery, state.onWrist, state.permission, state.appVersion))) {
                    is ApiOutcome.Ok -> SyncResult.NothingToSend
                    is ApiOutcome.Offline -> SyncResult.Offline
                    is ApiOutcome.Unauthorised -> { deps.onUnauthorised(); SyncResult.Unauthorised }
                    is ApiOutcome.Failed -> SyncResult.Offline
                }
            }
            if (batch.isEmpty()) return SyncResult.Done
            when (deps.api.report(DeviceReport(batch, state.battery, state.onWrist, state.permission, state.appVersion))) {
                is ApiOutcome.Ok -> { deps.queue.drop(batch.size); sentAnything = true }
                is ApiOutcome.Offline -> return SyncResult.Offline
                is ApiOutcome.Unauthorised -> { deps.onUnauthorised(); return SyncResult.Unauthorised }
                is ApiOutcome.Failed -> return SyncResult.Offline
            }
        }
    }
}
