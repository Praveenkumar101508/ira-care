package care.ira.watch.sensors

import care.ira.watch.api.Reading
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * Turns a stream of raw sensor samples into readings the backend wants:
 * every heart-rate sample with its own sample time (the backend keeps them all
 * and stamps when each arrived), step deltas summed per minute, wrist state only
 * when it changes. Pure Kotlin, so it is tested without a watch.
 */
class ReadingBatcher {
    private var lastHeartRateMillis: Long = -1
    private var stepsThisMinute = 0.0
    private var stepsMinute: Long = -1
    private var lastOnWrist: Boolean? = null

    /**
     * Every sample is kept, at the time it was sampled. Only an exact repeat of
     * the same instant is dropped (the backend's uniqueness is per sample time).
     */
    fun heartRate(bpm: Double, atMillis: Long): Reading? {
        if (atMillis == lastHeartRateMillis) return null
        lastHeartRateMillis = atMillis
        return Reading(ReadingKinds.HEART_RATE, bpm, iso(atMillis), "bpm")
    }

    /** Steps since the previous sample; returns the finished minute's total when a new minute starts. */
    fun steps(delta: Double, atMillis: Long): Reading? {
        val minute = atMillis / 60_000
        var out: Reading? = null
        if (stepsMinute != -1L && minute != stepsMinute) {
            out = Reading(ReadingKinds.STEPS, stepsThisMinute, iso(stepsMinute * 60_000))
            stepsThisMinute = 0.0
        }
        stepsMinute = minute
        stepsThisMinute += delta
        return out
    }

    /** Flush the current step minute (on sync, so the backend's movement window is current). */
    fun flushSteps(atMillis: Long): Reading? {
        if (stepsMinute == -1L) return null
        val out = Reading(ReadingKinds.STEPS, stepsThisMinute, iso(atMillis))
        stepsThisMinute = 0.0
        stepsMinute = atMillis / 60_000
        return out
    }

    fun onWrist(worn: Boolean, atMillis: Long): Reading? {
        if (lastOnWrist == worn) return null
        lastOnWrist = worn
        return Reading(ReadingKinds.ON_WRIST, if (worn) 1.0 else 0.0, iso(atMillis))
    }

    companion object {
        private val FORMAT = DateTimeFormatter.ISO_INSTANT
        fun iso(millis: Long): String = FORMAT.format(Instant.ofEpochMilli(millis).atOffset(ZoneOffset.UTC))
    }
}
