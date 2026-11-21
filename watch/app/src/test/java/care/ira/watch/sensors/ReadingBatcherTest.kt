package care.ira.watch.sensors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ReadingBatcherTest {
    private val t0 = 1_800_000_000_000L  // on a minute boundary

    @Test
    fun `one heart rate per minute the first sample wins`() {
        val b = ReadingBatcher()
        val first = b.heartRate(72.0, t0 + 1_000)
        assertEquals("heart_rate", first?.kind); assertEquals(72.0, first?.value)
        assertNull(b.heartRate(80.0, t0 + 30_000))
        assertEquals(80.5, b.heartRate(80.5, t0 + 61_000)?.value)
    }

    @Test
    fun `steps are summed per minute and emitted when the minute turns`() {
        val b = ReadingBatcher()
        assertNull(b.steps(3.0, t0 + 1_000))
        assertNull(b.steps(4.0, t0 + 40_000))
        val out = b.steps(1.0, t0 + 61_000)
        assertEquals("steps", out?.kind); assertEquals(7.0, out?.value)
        assertEquals(ReadingBatcher.iso(t0), out?.recordedAt)
        assertEquals(1.0, b.flushSteps(t0 + 70_000)?.value)
    }

    @Test
    fun `wrist state is reported only when it changes`() {
        val b = ReadingBatcher()
        assertEquals(1.0, b.onWrist(true, t0)?.value)
        assertNull(b.onWrist(true, t0 + 1000))
        assertEquals(0.0, b.onWrist(false, t0 + 2000)?.value)
    }

    @Test
    fun `timestamps are iso instants in utc`() {
        assertEquals("2027-01-15T08:00:00Z", ReadingBatcher.iso(t0))
    }
}
