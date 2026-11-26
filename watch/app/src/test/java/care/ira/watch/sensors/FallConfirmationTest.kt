package care.ira.watch.sensors

import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.random.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Synthetic signals only (the same shapes as scripts/eval/fall_synthetic_scenarios.py, in miniature).
 * What is pinned: a fall with its orientation change is confirmed; a still wrist is always
 * confirmed whatever the orientation did; a burst of hand impacts with the wrist pointing the same
 * way and the hand carrying on is suppressed; a single slap followed by a busy hand is suppressed;
 * a saturated axis is flagged; the stage is not ready before four seconds after the impact.
 */
class FallConfirmationTest {
    private val hz = 50
    private val rnd = Random(20261126)

    private fun sample(i: Int, x: Double, y: Double, z: Double) = AccelSample(i * 1000L / hz, x, y, z)

    /** `seconds` of a wrist whose gravity reads along `g`, plus a sinusoid of `amp` g ALONG gravity (what changes |a|) and sensor noise. */
    private fun segment(from: Int, seconds: Double, g: DoubleArray, amp: Double = 0.0, periodS: Double = 1.0, noise: Double = 0.02): List<AccelSample> =
        (0 until (seconds * hz).toInt()).map { k ->
            val t = k.toDouble() / hz
            val swing = 1.0 + amp * sin(2 * Math.PI * t / periodS)
            sample(from + k, g[0] * swing + (rnd.nextDouble() - 0.5) * 2 * noise, g[1] * swing + (rnd.nextDouble() - 0.5) * 2 * noise, g[2] * swing + (rnd.nextDouble() - 0.5) * 2 * noise)
        }

    private fun impact(at: Int, g: DoubleArray, peak: Double, dir: DoubleArray): List<AccelSample> {
        val n = sqrt(dir[0] * dir[0] + dir[1] * dir[1] + dir[2] * dir[2])
        val d = doubleArrayOf(dir[0] / n, dir[1] / n, dir[2] / n)
        return listOf(
            sample(at, g[0], g[1], g[2]),
            sample(at + 1, g[0] + d[0] * peak, g[1] + d[1] * peak, g[2] + d[2] * peak),
            sample(at + 2, g[0] + d[0] * peak * 0.55, g[1] + d[1] * peak * 0.55, g[2] + d[2] * peak * 0.55),
            sample(at + 3, g[0], g[1], g[2]),
        )
    }

    private fun run(samples: List<AccelSample>): FallConfirmation = FallConfirmation().also { c -> samples.forEach(c::add) }

    private val standing = doubleArrayOf(0.0, 0.0, 1.0)
    private val lying = doubleArrayOf(sin(Math.toRadians(100.0)), 0.0, cos(Math.toRadians(100.0)))   // 100 degrees from z

    @Test fun `a fall that changes the wrist's orientation and leaves it lying still is confirmed`() {
        val out = segment(0, 3.0, standing).toMutableList()
        val impactAt = out.size
        out += impact(impactAt, lying, 4.5, lying)
        out += segment(out.size, 6.0, lying, noise = 0.03)
        val d = run(out).decide(out[impactAt + 1].atMillis)
        assertTrue(d.reason, d.confirm)
        assertTrue("turned ${d.orientationChangeDeg}", d.orientationChangeDeg > 60)
        assertEquals("still afterwards", d.reason)
    }

    @Test fun `a still wrist is confirmed even when its orientation did not change`() {
        // A fall landing with the wrist pointing as before (a person lying on their back with the arm as it was):
        // the stage must still ask. Stillness wins over orientation.
        val out = segment(0, 3.0, standing).toMutableList()
        val impactAt = out.size
        out += impact(impactAt, standing, 5.0, doubleArrayOf(1.0, 0.0, 0.0))
        out += segment(out.size, 6.0, standing, noise = 0.02)
        val d = run(out).decide(out[impactAt + 1].atMillis)
        assertTrue(d.confirm)
        assertTrue(d.orientationChangeDeg < FallConfirmation.SAME_TURN_DEG)
        assertEquals("still afterwards", d.reason)
    }

    @Test fun `clapping, the wrist pointing the same way and the hand carrying on, is suppressed`() {
        val out = segment(0, 3.0, standing).toMutableList()
        var impactAt = -1
        repeat(6) { k ->
            out += segment(out.size, 0.25, standing, amp = 0.5, periodS = 0.4)
            val at = out.size
            if (k == 2) impactAt = at + 1
            out += impact(at, standing, 3.0, doubleArrayOf(0.0, 1.0, 0.2))
        }
        out += segment(out.size, 5.0, standing, amp = 0.6, periodS = 0.8)
        val d = run(out).decide(out[impactAt].atMillis)
        assertFalse(d.reason, d.confirm)
        assertTrue("impacts ${d.impactsInWindow}", d.impactsInWindow >= FallConfirmation.REPEAT_MIN)
        assertTrue(d.orientationChangeDeg < FallConfirmation.REPEAT_TURN_DEG)
    }

    @Test fun `one slap on the table with the hand carrying on busily is suppressed`() {
        val out = segment(0, 3.0, standing).toMutableList()
        val impactAt = out.size
        out += impact(impactAt, standing, 4.0, doubleArrayOf(0.0, 1.0, 0.0))
        out += segment(out.size, 6.0, standing, amp = 0.8, periodS = 0.7)     // sd about 0.57 g: busy
        val d = run(out).decide(out[impactAt + 1].atMillis)
        assertFalse(d.reason, d.confirm)
        assertEquals("same orientation, hand carried on", d.reason)
    }

    @Test fun `a slap with the hand only gently moving afterwards is still asked about`() {
        // Between "still" and "busy" the stage does not know: it asks (the conservative side).
        val out = segment(0, 3.0, standing).toMutableList()
        val impactAt = out.size
        out += impact(impactAt, standing, 4.0, doubleArrayOf(0.0, 1.0, 0.0))
        out += segment(out.size, 6.0, standing, amp = 0.35, periodS = 1.0)    // sd about 0.25 g
        val d = run(out).decide(out[impactAt + 1].atMillis)
        assertTrue(d.reason, d.confirm)
    }

    @Test fun `a saturated axis is flagged and does not by itself suppress`() {
        val out = segment(0, 3.0, standing).toMutableList()
        val impactAt = out.size
        out += impact(impactAt, standing, 16.0, doubleArrayOf(1.0, 0.0, 0.0))
        out += segment(out.size, 6.0, standing, noise = 0.02)
        val d = run(out).decide(out[impactAt + 1].atMillis)
        assertTrue(d.clipped)
        assertTrue(d.confirm)
    }

    @Test fun `the stage is ready four seconds after the impact and not before`() {
        val c = FallConfirmation()
        assertFalse(c.ready(10_000L, 13_999L))
        assertTrue(c.ready(10_000L, 14_000L))
    }

    @Test fun `only the last thirty seconds are kept`() {
        val c = FallConfirmation()
        (0 until 40 * hz).forEach { i -> c.add(sample(i, 0.0, 0.0, 1.0)) }
        // A decision about an impact 35 s ago sees no "before" window and no "after" window: still, confirmed.
        val d = c.decide(5_000L)
        assertTrue(d.confirm)
        assertEquals(0.0, d.orientationChangeDeg, 1e-9)
    }
}
