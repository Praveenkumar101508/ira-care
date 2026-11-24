package care.ira.watch.sensors

import kotlin.random.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The model sits behind the detector interface: it can stop a false alarm early
 * and never invents a fall the rules did not see an impact for. With no model,
 * the behaviour is exactly the rule detector's. Signals are synthetic (50 Hz).
 */
class ModelFallDetectorTest {
    private val hz = 50
    private val rnd = Random(20261123)
    private val spec = FallModelSpec(threshold = 0.7f, mean = listOf(0f, 0f, 1f, 1f), std = listOf(1f, 1f, 1f, 1f), windowSamples = 200, rateHz = 50)

    private fun g(t: Int, mag: Double) = AccelSample(t * 1000L / hz, 0.0, 0.0, mag)
    private fun still(from: Int, seconds: Int, noise: Double = 0.05) =
        (0 until seconds * hz).map { i -> g(from + i, 1.0 + (rnd.nextDouble() - 0.5) * 2 * noise) }
    private fun fall(peakG: Double, stillS: Int = 25): List<AccelSample> {
        val out = still(0, 5).toMutableList(); var t = out.size
        repeat(hz / 4) { out += g(t++, 0.3) }
        out += g(t++, peakG); out += g(t++, peakG * 0.6); out += g(t++, 1.8); out += g(t++, 1.3)
        repeat(hz / 2) { out += g(t++, 1.0 + (rnd.nextDouble() - 0.5) * 0.3) }
        out += still(t, stillS); return out
    }
    private fun suspected(events: List<FallEvent>) = events.any { it is FallEvent.SuspectedFall }

    @Test
    fun `a model that says fall leaves the rule's still window in charge`() {
        var asked = 0
        val d = ModelFallDetector(FallSettings(), spec, { asked++; 0.95f })
        val events = d.run(fall(3.5))
        assertTrue(events.first() is FallEvent.ImpactSeen)
        assertTrue(suspected(events))
        assertEquals(1, asked)
        assertEquals(0.95f, d.lastProbability)
    }

    @Test
    fun `a model that says no clears the impact two seconds in, before anyone is asked`() {
        val d = ModelFallDetector(FallSettings(), spec, { 0.1f })
        val events = d.run(fall(3.5))
        assertTrue(events.any { it is FallEvent.ImpactSeen })
        val cleared = events.first { it is FallEvent.Cleared } as FallEvent.Cleared
        assertTrue(!suspected(events))
        val impact = (events.first { it is FallEvent.ImpactSeen } as FallEvent.ImpactSeen).atMillis
        assertTrue(cleared.atMillis - impact in 2_000L..2_300L)
    }

    @Test
    fun `an unavailable model is exactly the rules`() {
        val withModel = ModelFallDetector(FallSettings(), spec, { null }).run(fall(3.5))
        val rules = FallDetector().run(fall(3.5))
        assertEquals(rules.map { it::class }, withModel.map { it::class })
        assertTrue(suspected(withModel))
    }

    @Test
    fun `the model is never asked without an impact and nothing fires off the wrist`() {
        var asked = 0
        val d = ModelFallDetector(FallSettings(), spec, { asked++; 0.99f })
        assertTrue(d.run(still(0, 30)).isEmpty() && asked == 0)
        val off = ModelFallDetector(FallSettings(), spec, { asked++; 0.99f }); off.onWrist = false
        assertTrue(off.run(fall(6.0)).isEmpty() && asked == 0)
    }

    @Test
    fun `the window keeps the latest sample per slot and normalises as trained`() {
        val w = SampleWindow(spec)
        (0 until 400).forEach { i -> w.add(AccelSample(i * 10L, 0.0, 0.0, 1.0 + i / 1000.0)) }
        assertTrue(w.isFull)
        val snap = w.snapshot()
        assertEquals(200, snap.size)
        val norm = spec.normalise(snap)
        assertEquals(snap[0][2] - 1f, norm[0][2], 1e-6f)
        val parsed = FallModelSpec.parse("""{"threshold": 0.8, "mean": [0,0,1,1], "std": [1,1,1,1], "window_samples": 200}""")
        assertEquals(0.8f, parsed.threshold)
    }
}
