package care.ira.watch.sensors

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.InputStream

/**
 * The trained fall model: a small convolutional network over a 4 s window of
 * accelerometer samples at 50 Hz (x, y, z and the magnitude, in g), exported to
 * TensorFlow Lite. Every build, debug and release alike, carries the same model: v3,
 * trained only on BITS-2 and UCI HAR (both CC BY 4.0; docs/ai/fall/MODEL_CARD_V3.md),
 * run in "recover" mode by `RecoveringFallDetector` (D-080, D-082). The SisFall-trained
 * v1 is in no build (D-082) and [FallModelSpec.mayRun] refuses it if it ever reappears.
 * `ModelFallDetector` (the "veto" mode) is kept as generic logic. Whenever the model
 * cannot be loaded, is refused or fails, the rule-based `FallDetector` alone is what runs.
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
    /** Where the model came from. A build of any kind may run only [PROVENANCE_LICENSED_V3]. */
    val provenance: String = "",
    /** The SHA-256 of the `.tflite` file this spec belongs to. */
    val tfliteSha256: String = "",
) {
    /**
     * Whether the watch may run this model at all (D-082): only the reviewed v3, named by its
     * licensed provenance, with the file whose SHA-256 is [tfliteSha256OnDisk] being the exact
     * reviewed file. Anything else (v1, an unlabelled model, a retrained file nobody has reviewed)
     * is refused and the rules alone run.
     */
    fun mayRun(tfliteSha256OnDisk: String): Boolean =
        provenance == PROVENANCE_LICENSED_V3 &&
            tfliteSha256.equals(tfliteSha256OnDisk, ignoreCase = true) &&
            tfliteSha256OnDisk.lowercase() in REVIEWED_TFLITE_SHA256

    fun normalise(window: Array<FloatArray>): Array<FloatArray> =
        Array(window.size) { i -> FloatArray(4) { c -> (window[i][c] - mean[c]) / std[c] } }

    companion object {
        const val MODE_VETO = "veto"
        const val MODE_RECOVER = "recover"
        /** The fall model trained only on commercially licensed data (BITS-2, UCI HAR; MODEL_CARD_V3). */
        const val PROVENANCE_LICENSED_V3 = "ira-fall-v3-licensed"
        /** The fall model files reviewed for shipping: v3 only (scripts/training/fall_model_v3.py, MODEL_CARD_V3.md). */
        val REVIEWED_TFLITE_SHA256 = setOf("2cab7c9bcbf60de51d7c42f5690ad549bcbf4f13eb6266c35bf5d0a59b063278")
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
