package care.ira.watch.sensors

import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Synthetic signals only. The counts asserted here are what the detector does
 * on signals WE MADE UP at 50 Hz; they say nothing about real falls. The
 * numbers are copied into docs/progress/STATUS.md with that warning.
 */
class FallDetectorTest {
    private val hz = 50
    private val rnd = Random(20261121)

    private fun g(t: Int, mag: Double): AccelSample = AccelSample(t * 1000L / hz, 0.0, 0.0, mag)

    /** Standing or lying still: 1 g with sensor noise. */
    private fun still(from: Int, seconds: Int, noise: Double = 0.05): List<AccelSample> =
        (0 until seconds * hz).map { i -> g(from + i, 1.0 + (rnd.nextDouble() - 0.5) * 2 * noise) }

    /** A fall: brief free fall, an impact of `peakG`, a bounce, then lying still for `stillS`. */
    private fun fall(peakG: Double, stillS: Int = 25): List<AccelSample> {
        val out = still(0, 2).toMutableList()
        var t = out.size
        repeat(hz / 4) { out += g(t++, 0.3) }                 // ~250 ms of near free fall
        out += g(t++, peakG); out += g(t++, peakG * 0.6); out += g(t++, 1.8); out += g(t++, 1.3)
        repeat(hz / 2) { out += g(t++, 1.0 + (rnd.nextDouble() - 0.5) * 0.3) }   // settling
        out += still(t, stillS)
        return out
    }

    /** Sitting down hard: a moderate jolt, then ordinary seated fidgeting. */
    private fun sitHard(peakG: Double): List<AccelSample> {
        val out = still(0, 2).toMutableList()
        var t = out.size
        out += g(t++, peakG); out += g(t++, 1.4)
        repeat(25 * hz) { i -> out += g(t++, 1.0 + 0.45 * sin(i / 7.0) * (if (i % 150 < 40) 1.0 else 0.2)) }
        return out
    }

    /** Arm swings while walking: periodic 0.6 to 2.2 g, never still. */
    private fun armSwing(amplitude: Double): List<AccelSample> =
        (0 until 30 * hz).map { i -> g(i, 1.0 + amplitude * cos(i / 4.0)) }

    /** The watch dropped onto a table: a sharp impact and then perfectly still. */
    private fun tableDrop(peakG: Double): List<AccelSample> {
        val out = still(0, 1).toMutableList()
        var t = out.size
        repeat(hz / 3) { out += g(t++, 0.1) }
        out += g(t++, peakG); out += g(t++, 2.0)
        out += still(t, 25, noise = 0.01)
        return out
    }

    private fun suspected(events: List<FallEvent>) = events.any { it is FallEvent.SuspectedFall }

    @Test
    fun `a fall is an impact followed by stillness`() {
        val events = FallDetector().run(fall(3.5))
        assertTrue(events.first() is FallEvent.ImpactSeen)
        val sus = events.last() as FallEvent.SuspectedFall
        assertEquals(20, sus.stillForSeconds); assertTrue(sus.peakG >= 3.5)
    }

    @Test
    fun `movement after the impact clears it`() {
        val events = FallDetector().run(sitHard(2.8))
        assertTrue(events.any { it is FallEvent.ImpactSeen })
        assertTrue(events.any { it is FallEvent.Cleared })
        assertTrue(!suspected(events))
    }

    @Test
    fun `off the wrist nothing is detected`() {
        val d = FallDetector(); d.onWrist = false
        assertTrue(d.run(tableDrop(6.0)).isEmpty())
    }

    @Test
    fun `thresholds are settings`() {
        assertTrue(!suspected(FallDetector(FallSettings(impactG = 4.0)).run(fall(3.5))))
        assertTrue(suspected(FallDetector(FallSettings(impactG = 4.0)).run(fall(4.5))))
        assertTrue(!suspected(FallDetector(FallSettings(stillSeconds = 30)).run(fall(3.5, stillS = 22))))
    }

    /**
     * The synthetic set: 20 falls, 20 hard sit-downs, 20 arm-swing walks, 10
     * table drops with the watch known to be off the wrist, 10 table drops with
     * the wrist state unknown. Counts are asserted exactly so a change in the
     * detector shows up here, and copied to STATUS.md.
     */
    @Test
    fun `synthetic set false positives and false negatives`() {
        var falseNegatives = 0; var falsePositives = 0
        val falls = (0 until 20).map { fall(2.6 + it * 0.2, stillS = 21 + it % 5) }
        falls.forEach { if (!suspected(FallDetector().run(it))) falseNegatives++ }
        val sits = (0 until 20).map { sitHard(1.5 + it * 0.1) }           // 1.5 to 3.4 g
        sits.forEach { if (suspected(FallDetector().run(it))) falsePositives++ }
        val swings = (0 until 20).map { armSwing(0.4 + it * 0.05) }        // up to 1.35 g amplitude
        swings.forEach { if (suspected(FallDetector().run(it))) falsePositives++ }
        val dropsOff = (0 until 10).map { tableDrop(4.0 + it * 0.5) }
        dropsOff.forEach { val d = FallDetector(); d.onWrist = false; if (suspected(d.run(it))) falsePositives++ }
        var unknownWristDrops = 0
        val dropsUnknown = (0 until 10).map { tableDrop(4.0 + it * 0.5) }
        dropsUnknown.forEach { if (suspected(FallDetector().run(it))) unknownWristDrops++ }
        // Recorded in STATUS.md. The table drops with the wrist state unknown are
        // indistinguishable from a fall by the accelerometer alone, and are counted.
        assertEquals("false negatives on 20 synthetic falls", 0, falseNegatives)
        assertEquals("false positives on 20 hard sit-downs, 20 arm-swing walks and 10 off-wrist table drops", 0, falsePositives)
        assertEquals("table drops with the wrist state unknown that read as a fall", 10, unknownWristDrops)
    }
}
