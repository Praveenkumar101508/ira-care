package care.ira.watch.activity

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.round
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * IRA Care Activity on the watch: what the wrist is doing, from one window of the
 * accelerometer (m/s²) and gyroscope (rad/s).
 *
 * A port of the backend's reference implementation, `supracloud-jarvis/ira/care/activity_estimate.py`,
 * reading the same model file (`assets/activity_model.json`, trained on WISDM, CC BY 4.0). The
 * same input checks, the same 20 Hz resampling and the same features in the same order;
 * `ActivityParityTest` holds the two together on a fixture the Python code wrote.
 *
 * A wellbeing context signal only. It is not a medical device, not a fall detector, and no
 * alert may be raised on it. It stays on the watch: the backend has no reading kind for it.
 * Pure Kotlin, tested on the JVM.
 */
object ActivityFeatures {
    const val FS_HZ = 20.0
    const val WINDOW_S = 10.0
    const val MIN_SECONDS = 8.0
    const val NFFT = 256
    val BANDS_HZ = listOf(0.3 to 1.0, 1.0 to 2.0, 2.0 to 3.0, 3.0 to 5.0, 5.0 to 10.0)
    const val AUTOCORR_LO_S = 0.3
    const val AUTOCORR_HI_S = 2.0
    const val GRAVITY_LO = 4.0
    const val GRAVITY_HI = 50.0
    const val FLAT_ACC = 1e-6
    const val FLAT_GYRO = 1e-7
    const val SATURATION_SHARE = 0.10
    const val SATURATION_MIN_ACC = 19.6
    const val SATURATION_MIN_GYRO = 5.0

    private val SIGNALS = listOf("acc_x", "acc_y", "acc_z", "acc_mag", "gyro_x", "gyro_y", "gyro_z", "gyro_mag")
    private val PER_SIGNAL: List<String> = listOf("mean", "std", "p10", "p50", "p90", "mean_abs_diff") +
        BANDS_HZ.map { (lo, hi) -> "logpow_${fmt(lo)}_${fmt(hi)}hz" } + listOf("dominant_hz", "spectral_entropy", "autocorr_peak")
    val FEATURE_NAMES: List<String> = SIGNALS.flatMap { s -> PER_SIGNAL.map { "${s}_$it" } } +
        listOf("gravity_x", "gravity_y", "gravity_z", "acc_corr_xy", "acc_corr_xz", "acc_corr_yz",
            "gyro_corr_xy", "gyro_corr_xz", "gyro_corr_yz")

    /** Python's `f"{x:g}"` for the band edges used here (0.3, 1, 2, 3, 5, 10). */
    private fun fmt(x: Double): String = if (x == floor(x)) x.toLong().toString() else x.toString()

    /** A checked window, resampled to 20 Hz and cut to the last 10 s, or the reason it was refused. */
    class Checked(val acc: Array<DoubleArray>?, val gyro: Array<DoubleArray>?, val reason: String?)

    fun checkWindow(acc: Array<DoubleArray>, gyro: Array<DoubleArray>, fsHz: Double = FS_HZ, gyroFsHz: Double = fsHz): Checked {
        fun refuse(why: String) = Checked(null, null, why)
        if (!(fsHz.isFinite() && gyroFsHz.isFinite() && fsHz > 0 && gyroFsHz > 0)) return refuse("bad_rate")
        if (acc.isEmpty() || acc.any { it.size != 3 }) return refuse("bad_accelerometer_shape")
        if (gyro.isEmpty() || gyro.any { it.size != 3 }) return refuse("bad_gyroscope_shape")
        if (acc.any { r -> r.any { !it.isFinite() } } || gyro.any { r -> r.any { !it.isFinite() } }) return refuse("not_finite")
        if (acc.size / fsHz < MIN_SECONDS || gyro.size / gyroFsHz < MIN_SECONDS) return refuse("too_short")
        if (maxPtp(acc) < FLAT_ACC) return refuse("flat_accelerometer")
        if (maxPtp(gyro) < FLAT_GYRO) return refuse("flat_gyroscope")
        if (saturated(acc, SATURATION_MIN_ACC)) return refuse("saturated_accelerometer")
        if (saturated(gyro, SATURATION_MIN_GYRO)) return refuse("saturated_gyroscope")
        val gravity = acc.sumOf { norm(it) } / acc.size
        if (gravity < GRAVITY_LO || gravity > GRAVITY_HI) return refuse("implausible_gravity")
        val a = resample(acc, fsHz)
        val g = resample(gyro, gyroFsHz)
        val n = minOf(a.size, g.size, round(WINDOW_S * FS_HZ).toInt())
        if (n < ceil(MIN_SECONDS * FS_HZ).toInt() - 1) return refuse("too_short")
        return Checked(a.copyOfRange(a.size - n, a.size), g.copyOfRange(g.size - n, g.size), null)
    }

    private fun norm(v: DoubleArray) = sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])

    private fun maxPtp(x: Array<DoubleArray>): Double =
        (0 until 3).maxOf { j -> x.maxOf { it[j] } - x.minOf { it[j] } }

    private fun saturated(x: Array<DoubleArray>, minExtreme: Double): Boolean {
        for (j in 0 until 3) {
            for (ext in doubleArrayOf(x.maxOf { it[j] }, x.minOf { it[j] })) {
                if (abs(ext) >= minExtreme && x.count { it[j] == ext }.toDouble() / x.size > SATURATION_SHARE) return true
            }
        }
        return false
    }

    /** numpy's `np.interp` onto a 20 Hz grid from sample index / rate, as the reference does. */
    fun resample(x: Array<DoubleArray>, fs: Double): Array<DoubleArray> {
        if (abs(fs - FS_HZ) < 1e-9) return Array(x.size) { x[it].copyOf() }
        val nOut = floor((x.size - 1) / fs * FS_HZ).toInt() + 1
        return Array(nOut) { k ->
            val t = k / FS_HZ
            val pos = t * fs                     // position in input samples
            val i = floor(pos).toInt().coerceIn(0, x.size - 1)
            if (i >= x.size - 1) x[x.size - 1].copyOf() else {
                val tIn0 = i / fs
                val tIn1 = (i + 1) / fs
                val w = (t - tIn0) / (tIn1 - tIn0)
                DoubleArray(3) { j -> x[i][j] + (x[i + 1][j] - x[i][j]) * w }
            }
        }
    }

    /** The 121 features of one checked window, in `FEATURE_NAMES` order. */
    fun windowFeatures(acc: Array<DoubleArray>, gyro: Array<DoubleArray>): DoubleArray {
        val n = acc.size
        val signals = Array(8) { DoubleArray(n) }
        for (t in 0 until n) {
            for (j in 0 until 3) { signals[j][t] = acc[t][j]; signals[4 + j][t] = gyro[t][j] }
            signals[3][t] = norm(acc[t]); signals[7][t] = norm(gyro[t])
        }
        val out = ArrayList<Double>(FEATURE_NAMES.size)
        for (s in signals) out.addAll(signalFeatures(s).toList())
        val gm = DoubleArray(3) { j -> acc.sumOf { it[j] } / n }
        val gn = max(norm(gm), 1e-9)
        out.addAll(gm.map { it / gn })
        out.addAll(corr3(acc)); out.addAll(corr3(gyro))
        return out.toDoubleArray()
    }

    private fun percentile(sorted: DoubleArray, q: Double): Double {
        val pos = (sorted.size - 1) * q / 100.0
        val lo = floor(pos).toInt()
        val hi = minOf(lo + 1, sorted.size - 1)
        return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
    }

    private fun signalFeatures(s: DoubleArray): DoubleArray {
        val n = s.size
        val mean = s.average()
        val std = sqrt(s.sumOf { (it - mean) * (it - mean) } / n)
        val sorted = s.sortedArray()
        val p10 = percentile(sorted, 10.0); val p50 = percentile(sorted, 50.0); val p90 = percentile(sorted, 90.0)
        var mad = 0.0
        for (i in 1 until n) mad += abs(s[i] - s[i - 1])
        mad /= (n - 1)
        val d = DoubleArray(n) { s[it] - mean }
        // np.hanning(n), the window power, and the zero-padded real FFT's power spectrum
        val w = DoubleArray(n) { if (n == 1) 1.0 else 0.5 - 0.5 * cos(2.0 * PI * it / (n - 1)) }
        val wpow = w.sumOf { it * it }
        val bins = NFFT / 2 + 1
        val spec = DoubleArray(bins)
        for (k in 0 until bins) {
            var re = 0.0; var im = 0.0
            for (t in 0 until n) {
                val ang = 2.0 * PI * k * t / NFFT
                val v = d[t] * w[t]
                re += v * cos(ang); im -= v * sin(ang)
            }
            spec[k] = (re * re + im * im) / wpow
        }
        val freqs = DoubleArray(bins) { it * FS_HZ / NFFT }
        val bands = BANDS_HZ.map { (lo, hi) ->
            var sum = 0.0
            for (k in 0 until bins) if (freqs[k] >= lo && freqs[k] < hi) sum += spec[k]
            log10(sum + 1e-8)
        }
        var dominant = Double.NaN; var best = Double.NEGATIVE_INFINITY
        for (k in 0 until bins) if (freqs[k] >= BANDS_HZ[0].first && freqs[k] < 5.0 && spec[k] > best) { best = spec[k]; dominant = freqs[k] }
        val entIdx = (0 until bins).filter { freqs[it] >= BANDS_HZ[0].first }
        val total = max(entIdx.sumOf { spec[it] }, 1e-300)
        var ent = 0.0
        for (k in entIdx) { val p = spec[k] / total; ent -= p * ln(max(p, 1e-300)) }
        ent /= ln(entIdx.size.toDouble())
        // linear (not circular) autocorrelation, normalised by lag 0
        val lo = (AUTOCORR_LO_S * FS_HZ).toInt(); val hi = (AUTOCORR_HI_S * FS_HZ).toInt()
        var ac0 = 0.0
        for (t in 0 until n) ac0 += d[t] * d[t]
        val den = max(ac0, 1e-12)
        var acPeak = Double.NEGATIVE_INFINITY
        for (lag in lo..minOf(hi, n - 1)) {
            var sum = 0.0
            for (t in 0 until n - lag) sum += d[t] * d[t + lag]
            acPeak = max(acPeak, sum / den)
        }
        return doubleArrayOf(mean, std, p10, p50, p90, mad, *bands.toDoubleArray(), dominant, ent, acPeak)
    }

    private fun corr3(x: Array<DoubleArray>): List<Double> {
        val n = x.size
        val m = DoubleArray(3) { j -> x.sumOf { it[j] } / n }
        val sd = DoubleArray(3) { j -> sqrt(x.sumOf { (it[j] - m[j]) * (it[j] - m[j]) } / n) }
        return listOf(0 to 1, 0 to 2, 1 to 2).map { (i, j) ->
            val den = sd[i] * sd[j]
            if (den > 1e-12) x.sumOf { (it[i] - m[i]) * (it[j] - m[j]) } / n / den else 0.0
        }
    }
}

/** One window's answer. `activity` is a class, "unknown" (not confident), or null when the input was refused. */
data class ActivityEstimate(
    val activity: String?,
    val confidence: Double,
    val reason: String? = null,
    val candidate: String? = null,
    val probabilities: Map<String, Double> = emptyMap(),
) {
    val reliable: Boolean get() = activity != null && activity != ActivityModel.UNKNOWN
}

/** The multilayer perceptron, read from the same JSON as the backend: standardise, ReLU, softmax. */
class ActivityModel private constructor(
    val classes: List<String>,
    private val mean: DoubleArray,
    private val scale: DoubleArray,
    private val layers: List<Pair<Array<DoubleArray>, DoubleArray>>,
    val minConfidence: Double,
) {
    fun predictProba(features: DoubleArray): DoubleArray {
        var h = DoubleArray(features.size) { (features[it] - mean[it]) / scale[it] }
        layers.forEachIndexed { k, (w, b) ->
            val out = DoubleArray(b.size) { j -> var s = b[j]; for (i in h.indices) s += h[i] * w[i][j]; s }
            h = if (k < layers.size - 1) DoubleArray(out.size) { max(out[it], 0.0) } else out
        }
        val top = h.max()
        val e = DoubleArray(h.size) { exp(h[it] - top) }
        val sum = e.sum()
        return DoubleArray(e.size) { e[it] / sum }
    }

    fun estimate(acc: Array<DoubleArray>, gyro: Array<DoubleArray>, fsHz: Double = ActivityFeatures.FS_HZ,
                 gyroFsHz: Double = fsHz): ActivityEstimate {
        val c = ActivityFeatures.checkWindow(acc, gyro, fsHz, gyroFsHz)
        if (c.reason != null) return ActivityEstimate(null, 0.0, c.reason)
        val p = predictProba(ActivityFeatures.windowFeatures(c.acc!!, c.gyro!!))
        var k = 0
        for (i in p.indices) if (p[i] > p[k]) k = i
        val probs = classes.indices.associate { classes[it] to p[it] }
        return if (p[k] < minConfidence) ActivityEstimate(UNKNOWN, p[k], "low_confidence", classes[k], probs)
        else ActivityEstimate(classes[k], p[k], null, classes[k], probs)
    }

    companion object {
        const val FORMAT = "ira-care-activity-mlp/1"
        const val UNKNOWN = "unknown"
        const val ASSET = "activity_model.json"
        const val MAX_BYTES = 2 * 1024 * 1024

        /** Parse and check the model file. Anything malformed is refused, never half-used. */
        fun parse(text: String): ActivityModel {
            require(text.length <= MAX_BYTES) { "activity model too large" }
            val spec = Json.parseToJsonElement(text).jsonObject
            require(spec["format"]?.jsonPrimitive?.content == FORMAT) { "not an activity model" }
            val names = spec.getValue("feature_names").jsonArray.map { it.jsonPrimitive.content }
            require(names == ActivityFeatures.FEATURE_NAMES) { "the model's features do not match this code's" }
            val classes = spec.getValue("classes").jsonArray.map { it.jsonPrimitive.content }
            val std = spec.getValue("standardise").jsonObject
            val mean = vector(std.getValue("mean").jsonArray)
            val scale = vector(std.getValue("scale").jsonArray)
            var width = names.size
            require(mean.size == width && scale.size == width && scale.all { it > 0 }) { "bad standardisation block" }
            val layers = spec.getValue("layers").jsonArray.map { l ->
                val o = l.jsonObject
                val w = o.getValue("W").jsonArray.map { vector(it.jsonArray) }.toTypedArray()
                val b = vector(o.getValue("b").jsonArray)
                require(w.size == width && w.all { it.size == b.size }) { "bad layer shapes" }
                width = b.size
                w to b
            }
            require(width == classes.size) { "the output layer does not match the classes" }
            require(layers.all { (w, b) -> w.all { r -> r.all { it.isFinite() } } && b.all { it.isFinite() } }) { "non-finite weights" }
            val minConfidence = (spec["min_confidence"] as? kotlinx.serialization.json.JsonPrimitive)?.double ?: 0.5
            return ActivityModel(classes, mean, scale, layers, minConfidence)
        }

        private fun vector(a: JsonArray): DoubleArray = DoubleArray(a.size) { a[it].jsonPrimitive.double }
    }
}
