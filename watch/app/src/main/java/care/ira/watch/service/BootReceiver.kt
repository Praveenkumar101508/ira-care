package care.ira.watch.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** The watch rebooted: IRA comes back on its own, if the watch is paired. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && CareDependencies.get(context).pairing.isPaired) {
            CareForegroundService.start(context)
            SyncWorker.schedule(context)
        }
    }
}
