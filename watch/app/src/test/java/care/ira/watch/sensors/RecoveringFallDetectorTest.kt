package care.ira.watch.sensors

import kotlin.random.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Fall model v3 in "recover" mode. The model may raise a fall the rules miss; the rules still
 * raise every fall they would have raised; off the wrist nothing is raised; a missing model is
 * the rules alone. Signals are synthetic, at 50 Hz; the model is a fake [Inference].
 */
class RecoveringFallDetectorTest {
    private val hz = 50
    private val rnd = Random(20261126)
    private val spec = FallModelSpec(threshold = 0.95f, mean = listOf(0f, 0f, 0f, 0f), std = listOf(1f, 1f, 1f, 1f),
        mode = FallModelSpec.MODE_RECOVER, candidateG = 2.0f)

    private fun g(t: Int, mag: Double) = AccelSample(t * 1000L / hz, 0.0, 0.0, mag)
    private fun still(from: Int, seconds: Int, noise: Double = 0.05) =
        (0 until seconds * hz).map { i -> g(from + i, 1.0 + (rnd.nextDouble() - 0.5) * 2 * noise) }
    private fun moving(from: Int, seconds: Int) = (0 until seconds * hz).map { i -> g(from + i, if (i % 10 < 5) 1.6 else 0.5) }

    /** Five quiet seconds, a short free fall, an impact of [peakG], then [after]. */
    private fun impact(peakG: Double, after: (Int) -> List<AccelSample>): List<AccelSample> {
        val out = still(0, 5).toMutableList(); var t = out.size
        repeat(hz / 4) { out += g(t++, 0.3) }
        out += g(t++, peakG * 0.8); out += g(t++, peakG); out += g(t++, 1.6); out += g(t++, 1.2)
        out += after(t); return out
    }

    private fun falls(events: List<FallEvent>) = events.filterIsInstance<FallEvent.SuspectedFall>()

    @Test
    fun `a 2_2 g fall the rules never see is raised by the model, two seconds after its peak`() {
        val samples = impact(2.2) { still(it, 25) }
        assertTrue("the rules alone miss it", falls(FallDetector().run(samples)).isEmpty())
        val d = RecoveringFallDetector(FallSettings(), spec, { 0.99f })
        val raised = falls(d.run(samples))
        assertEquals(1, raised.size)
        assertEquals(0, raised[0].stillForSeconds)
        assertEquals(2.2, raised[0].peakG, 1e-9)
        assertEquals(0.99f, d.lastProbability)
    }

    @Test
    fun `a fall followed by wrist movement, which the rules clear, is raised by the model`() {
        val samples = impact(3.5) { moving(it, 25) }
        assertTrue(falls(FallDetector().run(samples)).isEmpty())
        assertEquals(1, falls(RecoveringFallDetector(FallSettings(), spec, { 0.99f }).run(samples)).size)
    }

    @Test
    fun `the rules stay the backstop - a still person after a hard impact is raised even when the model says no`() {
        val samples = impact(3.5) { still(it, 25) }
        val raised = falls(RecoveringFallDetector(FallSettings(), spec, { 0.01f }).run(samples))
        assertEquals(1, raised.size)
        assertEquals(FallSettings().stillSeconds, raised[0].stillForSeconds)
    }

    @Test
    fun `nothing is raised when the model says no and the person moves on`() {
        val samples = impact(3.5) { moving(it, 25) }
        assertTrue(falls(RecoveringFallDetector(FallSettings(), spec, { 0.5f }).run(samples)).isEmpty())
    }

    @Test
    fun `below the candidate impact the model is never asked`() {
        var asked = 0
        val samples = impact(1.8) { still(it, 25) }
        assertTrue(falls(RecoveringFallDetector(FallSettings(), spec, { asked++; 0.99f }).run(samples)).isEmpty())
        assertEquals(0, asked)
    }

    @Test
    fun `off the wrist nothing is raised, whatever the model says`() {
        val d = RecoveringFallDetector(FallSettings(), spec, { 0.99f })
        d.onWrist = false
        assertTrue(falls(d.run(impact(3.5) { still(it, 25) })).isEmpty())
    }

    @Test
    fun `a model that fails is the rules alone`() {
        val samples = impact(2.2) { still(it, 25) } + impact(3.5) { still(it, 25) }.map { it.copy(atMillis = it.atMillis + 60_000) }
        val withFailingModel = RecoveringFallDetector(FallSettings(), spec, { null }).run(samples)
        val rules = FallDetector().run(samples)
        assertEquals(falls(rules).size, falls(withFailingModel).size)
        assertNull(RecoveringFallDetector(FallSettings(), spec, { null }).also { it.run(samples) }.lastProbability)
    }

    @Test
    fun `the model sees the peak in the middle of its window, as it was trained`() {
        var seen: Array<FloatArray>? = null
        val samples = impact(2.6) { moving(it, 10) }
        RecoveringFallDetector(FallSettings(), spec, { w -> seen = w; 0.0f }).run(samples)
        val w = seen!!
        assertEquals(spec.windowSamples, w.size)
        val peakIndex = w.indices.maxByOrNull { w[it][3] }!!
        assertTrue("peak at index $peakIndex, expected about 100", peakIndex in 97..103)
    }

    @Test
    fun `the shipped spec is the licensed v3 in recover mode`() {
        val shipped = FallModelSpec.parse(File("src/main/assets/fall_model.json").readText())
        assertEquals(FallModelSpec.MODE_RECOVER, shipped.mode)
        assertEquals(FallModelSpec.PROVENANCE_LICENSED_V3, shipped.provenance)
        assertEquals(2.0f, shipped.candidateG, 0f)
        assertFalse(shipped.threshold <= 0.5f)
    }
}
