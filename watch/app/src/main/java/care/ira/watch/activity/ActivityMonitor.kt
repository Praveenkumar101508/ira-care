package care.ira.watch.activity

import kotlin.math.floor

/**
 * Runs IRA Care Activity on the watch's own motion samples and keeps the latest answer on the
 * watch. Nothing is sent: the backend has no reading kind for it, and a wellbeing context
 * signal is not an alert.
 *
 * Samples arrive at whatever rate the sensors give (the accelerometer at about 50 Hz for the
 * fall detector, the gyroscope at about 20 Hz). Every [STEP_MS] of sample time, the last
 * [ActivityFeatures.WINDOW_S] seconds of both are interpolated by timestamp onto one 20 Hz
 * grid and classified. Off the wrist, or when either sensor has a gap, there is no estimate:
 * `latest` becomes null rather than keeping a stale answer. Pure Kotlin, tested on the JVM.
 */
class ActivityMonitor(
    private val model: ActivityModel,
    /** m/s² per g: the accelerometer source reports in g. */
    private val gravity: Double = 9.80665,
) {
    private class Sample(val atMillis: Long, val x: Double, val y: Double, val z: Double)

    private val acc = ArrayDeque<Sample>()
    private val gyro = ArrayDeque<Sample>()
    private var nextAtMillis = Long.MIN_VALUE

    /** The latest answer, or null when there is none (refused, off the wrist, a gap, or not started). */
    @Volatile var latest: ActivityEstimate? = null
        private set
    @Volatile var latestAtMillis: Long = 0L
        private set

    /** False while the watch is off the wrist: nothing is estimated and the last answer is cleared. */
    @Volatile var onWrist: Boolean? = null

    /** An accelerometer sample in g (as the fall detector's source gives it). */
    @Synchronized
    fun feedAccelG(atMillis: Long, x: Double, y: Double, z: Double): ActivityEstimate? {
        acc.addLast(Sample(atMillis, x * gravity, y * gravity, z * gravity))
        return step(atMillis)
    }

    /** A gyroscope sample in rad/s. */
    @Synchronized
    fun feedGyro(atMillis: Long, x: Double, y: Double, z: Double): ActivityEstimate? {
        gyro.addLast(Sample(atMillis, x, y, z))
        return step(atMillis)
    }

    private fun step(now: Long): ActivityEstimate? {
        val keepFrom = now - KEEP_MS
        while (acc.isNotEmpty() && acc.first().atMillis < keepFrom) acc.removeFirst()
        while (gyro.isNotEmpty() && gyro.first().atMillis < keepFrom) gyro.removeFirst()
        if (onWrist == false) { latest = null; return null }
        if (nextAtMillis == Long.MIN_VALUE) nextAtMillis = now + WINDOW_MS
        if (now < nextAtMillis) return null
        val end = minOf(acc.lastOrNull()?.atMillis ?: return null, gyro.lastOrNull()?.atMillis ?: return null)
        val start = end - WINDOW_MS
        // Not yet a full window from both sensors: try again on the next sample.
        if (acc.first().atMillis > start || gyro.first().atMillis > start) return null
        nextAtMillis = now + STEP_MS
        val a = grid(acc, start) ?: return clear()
        val g = grid(gyro, start) ?: return clear()
        val est = model.estimate(a, g, ActivityFeatures.FS_HZ)
        latest = if (est.activity == null) null else est
        latestAtMillis = end
        return est
    }

    private fun clear(): ActivityEstimate? { latest = null; return null }

    /** The window on a 20 Hz grid from [start], or null when samples do not cover it without a gap. */
    private fun grid(samples: ArrayDeque<Sample>, start: Long): Array<DoubleArray>? {
        val n = (WINDOW_MS / 1000.0 * ActivityFeatures.FS_HZ).toInt()
        if (samples.size < 2) return null
        for (i in 1 until samples.size) if (samples[i].atMillis - samples[i - 1].atMillis > MAX_GAP_MS) {
            if (samples[i].atMillis > start) return null
        }
        val out = Array(n) { DoubleArray(3) }
        var j = 0
        for (k in 0 until n) {
            val t = start + floor(k * 1000.0 / ActivityFeatures.FS_HZ).toLong()
            while (j < samples.size - 2 && samples[j + 1].atMillis <= t) j++
            val s0 = samples[j]; val s1 = samples[j + 1]
            val span = (s1.atMillis - s0.atMillis).toDouble()
            val w = if (span <= 0) 0.0 else ((t - s0.atMillis) / span).coerceIn(0.0, 1.0)
            out[k][0] = s0.x + (s1.x - s0.x) * w
            out[k][1] = s0.y + (s1.y - s0.y) * w
            out[k][2] = s0.z + (s1.z - s0.z) * w
        }
        return out
    }

    companion object {
        const val WINDOW_MS = 10_000L
        /** A new answer every 10 s of sample time. */
        const val STEP_MS = 10_000L
        /** Samples older than this are dropped. */
        const val KEEP_MS = 12_000L
        /** A longer silence from either sensor inside the window means no estimate. */
        const val MAX_GAP_MS = 500L
    }
}
