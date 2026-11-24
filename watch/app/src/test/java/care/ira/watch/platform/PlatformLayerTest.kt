package care.ira.watch.platform

import care.ira.watch.core.Clock
import care.ira.watch.sensors.ReadingKinds
import care.ira.watch.sensors.SensorEventMapper
import care.ira.watch.sensors.SimulatedSensorSource
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private class FixedClock(var now: Long) : Clock {
    override fun nowMillis(): Long = now
}

class SensorEventMapperTest {
    private val m = SensorEventMapper()

    @Test
    fun `an unreliable or impossible heart-rate sample is dropped, a good one kept at its time`() {
        assertNull(m.heartRate(72f, accuracy = 0, atMillis = 1_000))      // SENSOR_STATUS_UNRELIABLE
        assertNull(m.heartRate(72f, accuracy = -1, atMillis = 1_000))     // SENSOR_STATUS_NO_CONTACT
        assertNull(m.heartRate(0f, accuracy = 3, atMillis = 1_000))
        val r = m.heartRate(72f, accuracy = 2, atMillis = 1_000)
        assertNotNull(r)
        assertEquals(ReadingKinds.HEART_RATE, r!!.kind)
        assertEquals(72.0, r.value, 0.0)
        assertEquals("1970-01-01T00:00:01Z", r.recordedAt)
    }

    @Test
    fun `the step counter's running total becomes deltas and a reboot counts as no new steps`() {
        assertNull(m.stepCounter(1000f, 1_000))                      // the first total only sets the baseline
        assertEquals(0.0, m.stepCounter(1012f, 61_000)!!.value, 0.0)  // a new minute: the finished minute held no steps
        assertEquals(12.0, m.stepCounter(1020f, 121_000)!!.value, 0.0) // the minute with 12 steps is reported at its own time
        assertEquals(8.0, m.flushSteps(181_000)!!.value, 0.0)          // the current minute, flushed on sync
        m.stepCounter(5f, 241_000)                                     // the counter reset after a reboot: no new steps
        m.stepCounter(9f, 301_000)
        assertEquals(4.0, m.flushSteps(361_000)!!.value, 0.0)
    }

    @Test
    fun `the off-body sensor becomes a wrist reading and only changes are reported`() {
        val on = m.offBody(1f, 1_000)
        assertNotNull(on)
        assertEquals(1.0, on!!.value, 0.0)
        assertNull(m.offBody(1f, 2_000))            // still on: nothing new
        assertEquals(0.0, m.offBody(0f, 3_000)!!.value, 0.0)
    }
}

class SimulatedSensorSourceTest {
    @Test
    fun `the scripted driver is deterministic, says it is simulated, and stays within a resting range`() {
        val a = SimulatedSensorSource(FixedClock(1_700_000_000_000L)).sample(1_700_000_000_000L)
        val b = SimulatedSensorSource(FixedClock(1_700_000_000_000L)).sample(1_700_000_000_000L)
        assertEquals(a, b)
        val hr = a.first { it.kind == ReadingKinds.HEART_RATE }
        assertTrue(hr.value in 62.0..74.0)
        assertTrue(a.all { it.unit.contains("simulated") })
        assertTrue(a.any { it.kind == ReadingKinds.ON_WRIST && it.value == 1.0 })
    }

    @Test
    fun `background permission is never a question for the simulated driver`() {
        assertTrue(SimulatedSensorSource(FixedClock(0)).backgroundPermissionGranted())
    }
}

class HapticsPatternTest {
    @Test
    fun `the patterns start with no delay and are short enough for a wrist`() {
        for (p in listOf(Haptics.DEFAULT, Haptics.SHORT)) {
            assertEquals(0L, p[0])
            assertTrue(p.sum() <= 2_000L)
        }
    }
}
