package care.ira.watch.debug

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import care.ira.watch.sensors.FallInjection

/**
 * Debug builds only: a synthetic suspected fall for the emulator journey, where no wrist can
 * fall. `adb shell am broadcast -n <appId>/care.ira.watch.debug.SyntheticFallReceiver
 * -a care.ira.watch.debug.SYNTHETIC_FALL --ef peak_g 3.2 --ei still_seconds 20 --el impact_at <ms>`
 * hands the event to the care service through [FallInjection], which treats it exactly as a
 * detector's event (the countdown, the outbox, the server, D-092). The receiver is exported so
 * the shell can reach it; it exists in `src/debug` alone, so no release build has it
 * (SyntheticFallReleaseTest, tests/readiness/test_watch_synthetic_fall.py).
 */
class SyntheticFallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val extras = intent.extras
        val event = FallInjection.fromExtras(extras?.get(EXTRA_PEAK_G), extras?.get(EXTRA_STILL_SECONDS) ?: DEFAULT_STILL_SECONDS,
            extras?.get(EXTRA_IMPACT_AT), System.currentTimeMillis())
        val accepted = event != null && FallInjection.inject(event)
        Log.i(TAG, "synthetic fall ${if (accepted) "injected" else "refused"}: $event")
    }

    companion object {
        const val TAG = "SyntheticFall"
        const val ACTION = "care.ira.watch.debug.SYNTHETIC_FALL"
        const val EXTRA_PEAK_G = "peak_g"
        const val EXTRA_STILL_SECONDS = "still_seconds"
        const val EXTRA_IMPACT_AT = "impact_at"
        const val DEFAULT_STILL_SECONDS = 20
    }
}
