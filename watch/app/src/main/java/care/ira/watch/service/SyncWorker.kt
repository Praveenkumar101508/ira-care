package care.ira.watch.service

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.DeviceReport
import care.ira.watch.pairing.PairingStore
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

/** Drains the queue into `/devices/me/report`, batch by batch. */
class Syncer(private val deps: CareDependencies) {
    /** The carer may have changed the fall settings (or turned detection off). */
    private fun refreshSettings() {
        when (val me = deps.api.me()) {
            is ApiOutcome.Ok -> deps.fallSettings = me.value.fall
            else -> {}   // keep the last known settings; the defaults until the first answer
        }
    }

    fun syncOnce(): SyncResult {
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
