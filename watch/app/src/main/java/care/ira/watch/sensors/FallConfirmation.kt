package care.ira.watch.sensors

import kotlin.math.acos
import kotlin.math.max
import kotlin.math.sqrt

/**
 * The confirmation stage between a suspected fall and the question on the wrist (round 9,
 * false alarms from hand impacts).
 *
 * A detector (the rule's still window, or the v3 model two seconds after the peak) says
 * "suspected fall". Before the countdown asks the person, this stage looks at the seconds around
 * the impact: a fall changes the direction gravity reads on the wrist and leaves the arm quiet
 * or stirring; a clap, a knock or a slap on the table leaves the wrist pointing the same way
 * and the hand carrying on. It NEVER suppresses a candidate whose wrist is still afterwards,
 * whatever the orientation did: a person lying still is always asked. So it can only remove
 * alarms from a moving wrist, and only costs the model path up to two more seconds before the
 * question (the rule path has already waited 20 s and loses nothing).
 *
 * The decision is deterministic and mirrored line for line by `scripts/eval/fall_confirmation.py`,
 * which runs it on the synthetic scenarios; `tests/readiness/test_fall_confirmation.py` pins the
 * constants of the two to each other. It is off until the carer's settings turn it on
 * (`FallSettingsDto.confirmBeforeAsking`, default false): it has been measured on synthetic
 * scenarios only (docs/ai/fall/FALSE_ALARM_INVESTIGATION.md), not yet on the permitted recorded
 * falls, and nothing that could lose a real fall ships on by default before that.
 */
class FallConfirmation(private val keepMillis: Long = KEEP_MS) {
    data class Decision(
        val confirm: Boolean,
        val reason: String,
        val orientationChangeDeg: Double,
        val postMotionSdG: Double,
        val impactsInWindow: Int,
        val clipped: Boolean,
    )

    private val buffer = ArrayDeque<AccelSample>()

    /** Every accelerometer sample goes here, in time order; the last `keepMillis` are kept. */
    fun add(sample: AccelSample) {
        buffer.addLast(sample)
        while (buffer.isNotEmpty() && sample.atMillis - buffer.first().atMillis > keepMillis) buffer.removeFirst()
    }

    /** Whether enough has been seen after the impact to decide. */
    fun ready(impactAtMillis: Long, latestMillis: Long): Boolean = latestMillis >= impactAtMillis + POST_TO_MS

    fun decide(impactAtMillis: Long): Decision {
        val pre = buffer.filter { it.atMillis in (impactAtMillis - PRE_FROM_MS)..(impactAtMillis - PRE_TO_MS) }
        val post = buffer.filter { it.atMillis in (impactAtMillis + POST_FROM_MS)..(impactAtMillis + POST_TO_MS) }
        val postSd = sd(post.map { it.magnitudeG })
        val turn = orientationChangeDeg(pre, post)
        val impacts = countImpacts(impactAtMillis)
        val clipped = buffer.any {
            it.atMillis in (impactAtMillis - PRE_FROM_MS)..(impactAtMillis + POST_TO_MS) &&
                (Math.abs(it.x) >= CLIP_G || Math.abs(it.y) >= CLIP_G || Math.abs(it.z) >= CLIP_G)
        }
        return when {
            postSd < STILL_SD_G -> Decision(true, "still afterwards", turn, postSd, impacts, clipped)
            impacts >= REPEAT_MIN && turn < REPEAT_TURN_DEG ->
                Decision(false, "repeated impacts, same orientation, moving", turn, postSd, impacts, clipped)
            turn < SAME_TURN_DEG && postSd > BUSY_SD_G ->
                Decision(false, "same orientation, hand carried on", turn, postSd, impacts, clipped)
            else -> Decision(true, "orientation changed or arm not busy", turn, postSd, impacts, clipped)
        }
    }

    private fun sd(values: List<Double>): Double {
        if (values.isEmpty()) return 0.0
        val mean = values.average()
        return sqrt(values.sumOf { (it - mean) * (it - mean) } / values.size)
    }

    private fun orientationChangeDeg(pre: List<AccelSample>, post: List<AccelSample>): Double {
        if (pre.isEmpty() || post.isEmpty()) return 0.0
        val a = doubleArrayOf(pre.sumOf { it.x } / pre.size, pre.sumOf { it.y } / pre.size, pre.sumOf { it.z } / pre.size)
        val b = doubleArrayOf(post.sumOf { it.x } / post.size, post.sumOf { it.y } / post.size, post.sumOf { it.z } / post.size)
        val na = sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
        val nb = sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
        if (na < 1e-6 || nb < 1e-6) return 0.0
        val cos = ((a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (na * nb)).coerceIn(-1.0, 1.0)
        return Math.toDegrees(acos(cos))
    }

    /** Local maxima of |a| at or above IMPACT_G within ±REPEAT_WINDOW_MS of the impact, at least REPEAT_GAP_MS apart. */
    private fun countImpacts(impactAtMillis: Long): Int {
        val w = buffer.filter { it.atMillis in (impactAtMillis - REPEAT_WINDOW_MS)..(impactAtMillis + REPEAT_WINDOW_MS) }
        var count = 0
        var last = Long.MIN_VALUE / 2
        for (i in w.indices) {
            val g = w[i].magnitudeG
            if (g < IMPACT_G) continue
            val left = if (i > 0) w[i - 1].magnitudeG else Double.NEGATIVE_INFINITY
            val right = if (i + 1 < w.size) w[i + 1].magnitudeG else Double.NEGATIVE_INFINITY
            if (g >= left && g > right && w[i].atMillis - last >= REPEAT_GAP_MS) {
                count++
                last = w[i].atMillis
            }
        }
        return max(count, 0)
    }

    companion object {
        /** How much signal is kept: the "before" window needs 2 s before the impact and the rule path raises 20 s after it. */
        const val KEEP_MS = 30_000L
        const val PRE_FROM_MS = 2_000L
        const val PRE_TO_MS = 500L
        const val POST_FROM_MS = 1_500L
        const val POST_TO_MS = 4_000L
        const val STILL_SD_G = 0.15
        const val BUSY_SD_G = 0.35
        const val SAME_TURN_DEG = 20.0
        const val REPEAT_TURN_DEG = 30.0
        const val REPEAT_MIN = 3
        const val REPEAT_WINDOW_MS = 3_000L
        const val REPEAT_GAP_MS = 150L
        const val IMPACT_G = 2.0
        const val CLIP_G = 15.5
    }
}
