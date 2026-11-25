package care.ira.watch.sensors

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.InputStream

/**
 * The trained fall model (round 3, phase 5): a small convolutional network over a
 * 4 s window of accelerometer samples at 50 Hz (x, y, z and the magnitude, in g),
 * trained on Kaggle from SisFall and UCI HAR (docs/MODEL_CHOICE.md) and exported to
 * TensorFlow Lite. It sits BEHIND the fall detector interface: `ModelFallDetector`
 * uses the rule detector's impact gate, then asks the model whether the window
 * around the impact looks like a fall. Whenever the model cannot be loaded or
 * fails, the rule-based `FallDetector` alone is what runs.
 *
 * Pure Kotlin here; the TensorFlow Lite call is behind [Inference] so the JVM
 * tests can drive the logic with a fake and the real interpreter lives in
 * [TfLiteInference] (Android only).
 */
fun interface Inference {
    /** The probability that a (WINDOW x 4) window is a fall, or null when the model is unavailable. */
    fun fallProbability(window: Array<FloatArray>): Float?
}

/** The normalisation the model was trained with, written by the training script. */
@Serializable
data class FallModelSpec(
    val threshold: Float,
    val mean: List<Float>,
    val std: List<Float>,
    val windowSamples: Int = 200,
    val rateHz: Int = 50,
    /** "veto" (v1: the model can only clear the rule's impacts) or "recover" (v3: the model also
     *  raises falls the rule misses, from a lower impact; RecoveringFallDetector). */
    val mode: String = MODE_VETO,
    /** The impact that opens a model candidate in "recover" mode, in g. */
    val candidateG: Float = 2.0f,
    /** Where the model came from. A release build may carry only [PROVENANCE_LICENSED_V3]. */
    val provenance: String = "",
) {
    fun normalise(window: Array<FloatArray>): Array<FloatArray> =
        Array(window.size) { i -> FloatArray(4) { c -> (window[i][c] - mean[c]) / std[c] } }

    companion object {
        const val MODE_VETO = "veto"
        const val MODE_RECOVER = "recover"
        /** The fall model trained only on commercially licensed data (BITS-2, UCI HAR; MODEL_CARD_V3). */
        const val PROVENANCE_LICENSED_V3 = "ira-fall-v3-licensed"
        private val json = Json { ignoreUnknownKeys = true }
        fun parse(text: String): FallModelSpec = json.decodeFromString(serializer(), text)
        fun parse(stream: InputStream): FallModelSpec = parse(stream.bufferedReader().readText())
    }
}

/**
 * A ring of the last `windowSamples` samples, resampled to the model's rate by
 * keeping the latest sample per 1/rate slot. Feeds the model a window centred
 * about a third of the way in, as it was trained (the impact is early in the window).
 */
class SampleWindow(private val spec: FallModelSpec) {
    private val slotMs = 1000L / spec.rateHz
    private val buffer = ArrayDeque<FloatArray>()
    private var lastSlot = Long.MIN_VALUE

    fun add(sample: AccelSample) {
        val slot = sample.atMillis / slotMs
        val row = floatArrayOf(sample.x.toFloat(), sample.y.toFloat(), sample.z.toFloat(), sample.magnitudeG.toFloat())
        if (slot == lastSlot && buffer.isNotEmpty()) buffer[buffer.size - 1] = row
        else { buffer.addLast(row); lastSlot = slot }
        while (buffer.size > spec.windowSamples) buffer.removeFirst()
    }

    val isFull: Boolean get() = buffer.size >= spec.windowSamples

    fun snapshot(): Array<FloatArray> = buffer.toTypedArray()
}

/**
 * Impact from the rules, confirmation from the model, stillness from the rules.
 *
 *   rule impact seen  -> keep filling the window for two more seconds
 *   window full       -> model probability >= threshold: carry on to the rule's still check
 *                        model says no: clear (the rule alone would have waited 20 s)
 *   model unavailable -> exactly the rule detector
 *
 * The person is told nothing until the rule's still window has passed, as before;
 * the model only stops false alarms earlier and adds its own view of the impact.
 */
class ModelFallDetector(
    private val settings: FallSettings,
    private val spec: FallModelSpec,
    private val inference: Inference,
    private val onDecision: ((probability: Float?, usedModel: Boolean) -> Unit)? = null,
) {
    private val rules = FallDetector(settings)
    private val window = SampleWindow(spec)
    private var impactAt: Long = -1
    private var decided = false
    var onWrist: Boolean?
        get() = rules.onWrist
        set(value) { rules.onWrist = value }
    /** What the model said about the last impact, for the server's record. */
    var lastProbability: Float? = null
        private set

    fun feed(sample: AccelSample): FallEvent? {
        window.add(sample)
        val event = rules.feed(sample)
        if (event is FallEvent.ImpactSeen) { impactAt = event.atMillis; decided = false; lastProbability = null }
        if (event is FallEvent.Cleared || event is FallEvent.SuspectedFall) { impactAt = -1 }
        if (impactAt >= 0 && !decided && window.isFull && sample.atMillis - impactAt >= AFTER_IMPACT_MS) {
            decided = true
            val p = runCatching { inference.fallProbability(spec.normalise(window.snapshot())) }.getOrNull()
            lastProbability = p
            onDecision?.invoke(p, p != null)
            if (p != null && p < spec.threshold) {
                rules.reset(); impactAt = -1
                return FallEvent.Cleared(sample.atMillis)
            }
        }
        return event
    }

    fun run(samples: List<AccelSample>): List<FallEvent> = samples.mapNotNull { feed(it) }

    companion object {
        /** The model sees the impact and the two seconds after it, as it was trained. */
        const val AFTER_IMPACT_MS = 2_000L
    }
}
