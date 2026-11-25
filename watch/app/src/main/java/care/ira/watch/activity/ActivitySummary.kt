package care.ira.watch.activity

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import kotlin.math.round

/**
 * The activity data contract (docs/ai/activity/DATA_CONTRACT.md), watch side.
 *
 * A window's label is one of the model's own classes, `unknown` (the model was not confident:
 * reason `low_confidence`), or `unavailable` (no estimate at all, with the reason: `off_wrist`,
 * `sensor_gap`, `too_short` or `sensor_fault`). Nothing else is a label, and no guess is ever
 * filled in for `unknown` or `unavailable`.
 */
object ActivityLabels {
    const val UNKNOWN = ActivityModel.UNKNOWN
    const val UNAVAILABLE = "unavailable"

    const val REASON_LOW_CONFIDENCE = "low_confidence"
    const val REASON_OFF_WRIST = "off_wrist"
    const val REASON_SENSOR_GAP = "sensor_gap"
    const val REASON_TOO_SHORT = "too_short"
    /** The model refused the window's signal: flat (stuck), saturated, not finite or implausible gravity. */
    const val REASON_SENSOR_FAULT = "sensor_fault"

    val UNAVAILABLE_REASONS = setOf(REASON_OFF_WRIST, REASON_SENSOR_GAP, REASON_TOO_SHORT, REASON_SENSOR_FAULT)

    /** The contract's reason for one of the model's refusals (`ActivityFeatures.checkWindow`). */
    fun reasonForRefusal(refusal: String?): String =
        if (refusal == "too_short") REASON_TOO_SHORT else REASON_SENSOR_FAULT

    /** Groups shown to the wearer and to carers. `eating_drinking` is never shown by name. */
    val ACTIVE = setOf("walking", "stairs")
    val STILL = setOf("sitting", "standing")
}

/** One 10 s window's outcome. Kept on the watch only; it is never stored or sent. */
data class ActivityWindow(
    val startMillis: Long,
    val endMillis: Long,
    val label: String,
    val confidence: Double?,
    val reason: String?,
) {
    companion object {
        fun unavailable(start: Long, end: Long, reason: String) =
            ActivityWindow(start, end, ActivityLabels.UNAVAILABLE, null, reason)

        fun of(est: ActivityEstimate, start: Long, end: Long): ActivityWindow = when (est.activity) {
            null -> unavailable(start, end, ActivityLabels.reasonForRefusal(est.reason))
            ActivityLabels.UNKNOWN -> ActivityWindow(start, end, ActivityLabels.UNKNOWN, est.confidence, ActivityLabels.REASON_LOW_CONFIDENCE)
            else -> ActivityWindow(start, end, est.activity, est.confidence, null)
        }
    }
}

/**
 * One hour, summarised: seconds per label and seconds per reason. This is all that can leave
 * the watch (POST /care/devices/me/activity), and only under the activity-sharing consent.
 * Time the watch did not record at all (the service was not running, a sensor stopped) is in
 * neither map: the hour's length minus the sum is "not recorded".
 */
@Serializable
data class ActivityPeriod(
    @SerialName("period_start") val periodStart: String,
    @SerialName("period_end") val periodEnd: String,
    @SerialName("model_version") val modelVersion: String,
    @SerialName("model_sha256") val modelSha256: String,
    /** `device` (the watch's own sensors) or `simulated` (a simulated-sensor build). */
    val source: String,
    /** Seconds per label: the model's classes, `unknown` and `unavailable`. Zero labels are left out. */
    val seconds: Map<String, Int>,
    /** Seconds per reason: `low_confidence` (for `unknown`) and the unavailable reasons. */
    val reasons: Map<String, Int> = emptyMap(),
    /** How many 10 s windows the hour holds. */
    val windows: Int,
    /** Mean top-class probability over the windows that had one (classified or unknown), or null. */
    @SerialName("mean_confidence") val meanConfidence: Double? = null,
)

/** The model a summary names: the format string and the SHA-256 of the exact model file. */
data class ActivityModelId(val version: String, val sha256: String) {
    companion object {
        fun of(modelText: String): ActivityModelId = ActivityModelId(ActivityModel.FORMAT, sha256(modelText.toByteArray(Charsets.UTF_8)))

        fun sha256(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Hourly periods from 10 s windows. A window counts in the hour its midpoint falls in, so an
 * hour never holds more than 3,600 s. An hour is closed (and handed back) by the first window
 * of a later hour or by [flushIfDue] once the hour is over; a closed hour is never reopened.
 * The hour still open is lost if the service is killed (it is then "not recorded"). Pure Kotlin.
 */
class ActivitySummariser(
    private val model: ActivityModelId,
    private val source: String,
    private val periodMillis: Long = HOUR_MS,
) {
    private var periodStart: Long = Long.MIN_VALUE
    private val seconds = LinkedHashMap<String, Double>()
    private val reasons = LinkedHashMap<String, Double>()
    private var windows = 0
    private var confSum = 0.0
    private var confN = 0
    private var lastClosedStart: Long = Long.MIN_VALUE

    /** Add one window; returns the hours it closed (usually none, at most the one before). */
    @Synchronized
    fun add(w: ActivityWindow): List<ActivityPeriod> {
        val mid = (w.startMillis + w.endMillis) / 2
        val start = Math.floorDiv(mid, periodMillis) * periodMillis
        if (start <= lastClosedStart) return emptyList()          // late window for a closed hour
        val closed = mutableListOf<ActivityPeriod>()
        if (periodStart != Long.MIN_VALUE && start != periodStart) close()?.let { closed += it }
        periodStart = start
        val s = (w.endMillis - w.startMillis).coerceAtLeast(0) / 1000.0
        seconds.merge(w.label, s, Double::plus)
        if (w.reason != null) reasons.merge(w.reason, s, Double::plus)
        windows++
        if (w.confidence != null) { confSum += w.confidence; confN++ }
        return closed
    }

    /** Close the open hour when [nowMillis] is past its end. */
    @Synchronized
    fun flushIfDue(nowMillis: Long): ActivityPeriod? =
        if (periodStart != Long.MIN_VALUE && nowMillis >= periodStart + periodMillis) close() else null

    private fun close(): ActivityPeriod? {
        if (periodStart == Long.MIN_VALUE || windows == 0) return null
        val out = ActivityPeriod(
            periodStart = iso(periodStart), periodEnd = iso(periodStart + periodMillis),
            modelVersion = model.version, modelSha256 = model.sha256, source = source,
            seconds = rounded(seconds), reasons = rounded(reasons), windows = windows,
            meanConfidence = if (confN == 0) null else round(confSum / confN * 1000) / 1000,
        )
        lastClosedStart = periodStart
        periodStart = Long.MIN_VALUE
        seconds.clear(); reasons.clear(); windows = 0; confSum = 0.0; confN = 0
        return out
    }

    private fun rounded(m: Map<String, Double>): Map<String, Int> =
        m.mapValues { round(it.value).toInt() }.filterValues { it > 0 }.toSortedMap()

    companion object {
        const val HOUR_MS = 3_600_000L
        const val SOURCE_DEVICE = "device"
        const val SOURCE_SIMULATED = "simulated"
        fun iso(millis: Long): String = Instant.ofEpochMilli(millis).toString()
    }
}

/**
 * Minutes of walking or stairs today, by the watch's own estimate, for the wearer's screen.
 * Counted on the watch from the windows themselves, whether or not anything is shared.
 */
class ActivityToday(private val zone: () -> ZoneId = { ZoneId.systemDefault() }) {
    private var day: LocalDate? = null
    private var activeSeconds = 0.0

    @Synchronized
    fun add(w: ActivityWindow) {
        val d = Instant.ofEpochMilli(w.endMillis).atZone(zone()).toLocalDate()
        if (d != day) { day = d; activeSeconds = 0.0 }
        if (w.label in ActivityLabels.ACTIVE) activeSeconds += (w.endMillis - w.startMillis) / 1000.0
    }

    /** Whole minutes for [nowMillis]'s day; zero once the day has changed. */
    @Synchronized
    fun activeMinutes(nowMillis: Long): Int =
        if (Instant.ofEpochMilli(nowMillis).atZone(zone()).toLocalDate() == day) (activeSeconds / 60).toInt() else 0
}

/** The wearer's one modest line. An estimate, said so; never a judgement, a goal or a health word. */
object ActivityWearerText {
    fun line(activeMinutes: Int): String? = when {
        activeMinutes <= 0 -> null
        else -> "On the move today: about $activeMinutes min\n(the watch's estimate)"
    }
}
