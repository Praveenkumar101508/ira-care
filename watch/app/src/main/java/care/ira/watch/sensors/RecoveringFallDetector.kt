package care.ira.watch.sensors

/**
 * Fall model v3 ("recover" mode, docs/ai/fall/MODEL_CARD_V3.md): the model can RAISE a suspected
 * fall that the rules would miss, and the rules stay in charge of everything they already catch.
 *
 *   rules     the rule detector runs unchanged: an impact at or above the carer's impactG followed
 *             by the full still window is a suspected fall, exactly as before (the backstop);
 *   candidate a sample at or above spec.candidateG (1.6 g shipped, D-115) opens a candidate; its peak is the
 *             largest magnitude in the next 1.5 s;
 *   model     two seconds after that peak the model scores the 4 s window, which then holds the
 *             peak at its middle as it was trained; at or above the threshold it is a suspected
 *             fall, without waiting for stillness (stillForSeconds = 0).
 *
 * Either path starts the same countdown, which asks "are you OK?" first. A second detection while
 * it runs is never dropped: the same impact seen twice is counted, a separate one is attached to the
 * incident and asked about again, without moving the deadline (FallCountdown, D-092). Off the wrist
 * nothing is raised. If the model
 * fails or is missing, only the rules run. Held out on BITS-2 (subject-grouped folds), the rules
 * caught 195 of 316 simulated falls with 52.84 alarms per hour of daily activity; this pipeline
 * caught 246 with at most 10.3. That is a measurement on young volunteers with a research band,
 * not older adults or this watch; real use needs the owner's and a clinical reviewer's approval.
 */
class RecoveringFallDetector(
    settings: FallSettings,
    private val spec: FallModelSpec,
    private val inference: Inference,
    private val onDecision: ((probability: Float?) -> Unit)? = null,
) {
    private val rules = FallDetector(settings)
    private val window = SampleWindow(spec)
    private var candidateAt = -1L
    private var peakAt = -1L
    private var peakG = 0.0

    var onWrist: Boolean?
        get() = rules.onWrist
        set(value) { rules.onWrist = value }

    /** What the model said about the last candidate, for the server's record. */
    var lastProbability: Float? = null
        private set

    fun feed(sample: AccelSample): FallEvent? {
        window.add(sample)
        val ruleEvent = rules.feed(sample)
        if (onWrist == false) { candidateAt = -1; return null }
        if (ruleEvent is FallEvent.SuspectedFall) { candidateAt = -1; return ruleEvent }

        val g = sample.magnitudeG
        val t = sample.atMillis
        if (candidateAt < 0) {
            if (g >= spec.candidateG) { candidateAt = t; peakAt = t; peakG = g }
            return ruleEvent
        }
        if (t - candidateAt <= SETTLE_MS) {
            if (g > peakG) { peakG = g; peakAt = t }
            return ruleEvent
        }
        if (t - peakAt >= AFTER_PEAK_MS && window.isFull) {
            candidateAt = -1
            val p = runCatching { inference.fallProbability(spec.normalise(window.snapshot())) }.getOrNull()
            lastProbability = p
            onDecision?.invoke(p)
            if (p != null && p >= spec.threshold) {
                rules.reset()
                return FallEvent.SuspectedFall(peakAt, peakG, 0)
            }
        }
        return ruleEvent
    }

    fun run(samples: List<AccelSample>): List<FallEvent> = samples.mapNotNull { feed(it) }

    companion object {
        /** The impact period: the peak is looked for this long after the candidate opens. */
        const val SETTLE_MS = 1_500L
        /** The model sees two seconds after the peak, as it was trained (peak at window index 100). */
        const val AFTER_PEAK_MS = 2_000L
    }
}
