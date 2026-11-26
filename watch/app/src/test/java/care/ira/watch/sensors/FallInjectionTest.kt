package care.ira.watch.sensors

import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** A synthetic fall becomes the same event a detector raises, and only when it could be one. */
class FallInjectionTest {
    @Test
    fun `broadcast extras of any type become a suspected fall`() {
        // --ef gives a Float, --ei an Int, --el a Long, --es a String.
        val fromShell = FallInjection.fromExtras(3.2f, 20, 1_700_000_000_000L, now = 1L)
        assertEquals(FallEvent.SuspectedFall(1_700_000_000_000L, 3.2f.toDouble(), 20), fromShell)
        assertEquals(FallEvent.SuspectedFall(5L, 2.9, 15), FallInjection.fromExtras("2.9", "15", "5", now = 1L))
        // No impact time: now stands in, so the countdown can still tell this impact from a later one.
        assertEquals(42L, FallInjection.fromExtras(3.0, 20, null, now = 42L)?.impactAtMillis)
    }

    @Test
    fun `values no detector could produce are refused`() {
        assertNull(FallInjection.fromExtras(null, 20, null, now = 1L))
        assertNull(FallInjection.fromExtras(0.0, 20, null, now = 1L))
        assertNull(FallInjection.fromExtras(-3.0, 20, null, now = 1L))
        assertNull(FallInjection.fromExtras(51.0, 20, null, now = 1L))
        assertNull(FallInjection.fromExtras(Double.NaN, 20, null, now = 1L))
        assertNull(FallInjection.fromExtras("three g", 20, null, now = 1L))
        assertNull(FallInjection.fromExtras(3.0, -1, null, now = 1L))
        assertNull(FallInjection.fromExtras(3.0, 601, null, now = 1L))
        assertNull(FallInjection.fromExtras(3.0, null, null, now = 1L))
    }

    @Test
    fun `an injected fall reaches whoever collects the events`() = runTest {
        val event = FallEvent.SuspectedFall(1_000L, 3.1, 20)
        val collector = launch { assertEquals(event, FallInjection.events.first()) }
        testScheduler.runCurrent()   // the collector is subscribed before anything is injected
        assertTrue(FallInjection.inject(event))
        collector.join()
    }
}
