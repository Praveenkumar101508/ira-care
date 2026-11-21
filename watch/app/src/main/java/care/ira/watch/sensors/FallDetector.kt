package care.ira.watch.sensors

/**
 * A suspected fall, from the accelerometer alone: a high-impact spike followed
 * by little movement for a while. Pure Kotlin, fed one sample at a time.
 *
 * This is a heuristic, not a medical device and not a clinical claim. The
 * thresholds are the person's settings (`FallSettings`), set by the carer,
 * and the synthetic-signal tests record exactly how often it is wrong on
 * signals we made up. Its accuracy on real people is UNMEASURED until it has
 * run on real watches, and `docs/DEVICE_SETUP.md` says how to turn on the
 * watch maker's own fall detection as a second layer.
 *
 * Magnitudes are in g (1.0 at rest). Samples arrive at whatever rate the
 * sensor gives; time comes from the sample, not a clock.
 */
data class FallSettings(
    /** A sample at or above this is an impact. */
    val impactG: Double = 2.5,
    /** How long after the impact the person must stay still before we suspect a fall. */
    val stillSeconds: Int = 20,
    /** A sample this far from 1 g (standing still) counts as movement. */
    val stillThresholdG: Double = 0.3,
    /** How many moving samples in the still window are tolerated (a twitch is not getting up). */
    val movingSamplesTolerated: Int = 3,
)

data class AccelSample(val atMillis: Long, val x: Double, val y: Double, val z: Double) {
    val magnitudeG: Double get() = Math.sqrt(x * x + y * y + z * z)
}

sealed class FallEvent {
    /** An impact was seen; the still window has started. */
    data class ImpactSeen(val atMillis: Long, val peakG: Double) : FallEvent()
    /** Movement after the impact: the person moved, so this was not a fall. */
    data class Cleared(val atMillis: Long) : FallEvent()
    /** Impact, then no movement for the whole window. */
    data class SuspectedFall(val impactAtMillis: Long, val peakG: Double, val stillForSeconds: Int) : FallEvent()
}

class FallDetector(private val settings: FallSettings = FallSettings()) {
    private var impactAt: Long = -1
    private var peak = 0.0
    private var movingSamples = 0
    /** Samples within this many ms of the impact belong to the impact itself (bounce, settle). */
    private val settleMs = 1_500L

    /** Whether the watch is known to be on the wrist. Off the wrist, nothing is detected
     *  (a watch dropped on a table is the classic false alarm). Unknown counts as worn. */
    var onWrist: Boolean? = null

    fun feed(sample: AccelSample): FallEvent? {
        if (onWrist == false) { reset(); return null }
        val g = sample.magnitudeG
        if (impactAt < 0) {
            if (g >= settings.impactG) {
                impactAt = sample.atMillis; peak = g; movingSamples = 0
                return FallEvent.ImpactSeen(sample.atMillis, g)
            }
            return null
        }
        val since = sample.atMillis - impactAt
        if (since <= settleMs) {
            if (g > peak) peak = g
            return null
        }
        if (Math.abs(g - 1.0) > settings.stillThresholdG) {
            movingSamples++
            if (movingSamples > settings.movingSamplesTolerated) {
                reset()
                return FallEvent.Cleared(sample.atMillis)
            }
        }
        if (since >= settings.stillSeconds * 1_000L) {
            val out = FallEvent.SuspectedFall(impactAt, peak, settings.stillSeconds)
            reset()
            return out
        }
        return null
    }

    fun reset() { impactAt = -1; peak = 0.0; movingSamples = 0 }

    /** Convenience for tests and replays: the last event of a whole signal. */
    fun run(samples: List<AccelSample>): List<FallEvent> = samples.mapNotNull { feed(it) }
}
