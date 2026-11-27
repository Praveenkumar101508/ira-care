package care.ira.watch.sensors

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow

/**
 * A suspected fall handed to the care service from outside the detectors, as if a detector had
 * raised it: the service treats what comes out of [events] exactly as a [FallEvent.SuspectedFall]
 * from the accelerometer path (the countdown, the outbox, the server, D-092), so an emulator
 * journey can exercise the fall countdown without a wrist to drop.
 *
 * Nothing in this source set calls [inject]. The only caller is the synthetic-fall receiver in the
 * debug source set (`src/debug`), reached with `am broadcast`; a release build carries this object
 * with nothing feeding it (SyntheticFallReleaseTest and tests/readiness/test_watch_synthetic_fall.py
 * keep it that way). Pure Kotlin: tested on the JVM.
 */
object FallInjection {
    private val flow = MutableSharedFlow<FallEvent.SuspectedFall>(extraBufferCapacity = 16)

    /** Synthetic falls, in the order they were injected, for the service to collect. */
    val events: SharedFlow<FallEvent.SuspectedFall> get() = flow

    /** Hands the event to whoever collects [events]; false when nothing could take it. */
    fun inject(event: FallEvent.SuspectedFall): Boolean = flow.tryEmit(event)

    /**
     * An event from broadcast extras, which arrive as whatever type the sender used (`--ef`
     * gives a Float, `--es` a String, `--ei` an Int). Null when the values are not a fall the
     * detectors could have produced: a peak of 0 or more than 50 g, a negative or absurd still
     * time, or anything unreadable. `now` stands in for a missing impact time.
     */
    fun fromExtras(peakG: Any?, stillSeconds: Any?, impactAtMillis: Any?, now: Long): FallEvent.SuspectedFall? {
        val peak = number(peakG) ?: return null
        val still = number(stillSeconds)?.toInt() ?: return null
        val impact = number(impactAtMillis)?.toLong() ?: now
        if (peak.isNaN() || peak <= 0.0 || peak > 50.0 || still < 0 || still > 600) return null
        return FallEvent.SuspectedFall(impact, peak, still)
    }

    private fun number(v: Any?): Double? = when (v) {
        null -> null
        is Number -> v.toDouble()
        is String -> v.trim().toDoubleOrNull()
        else -> null
    }
}
