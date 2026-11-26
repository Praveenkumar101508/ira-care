package care.ira.watch.checkin

import care.ira.watch.outbox.Outbox
import care.ira.watch.sensors.AccelSample
import care.ira.watch.sensors.FallEvent
import care.ira.watch.sensors.FallModelSpec
import care.ira.watch.sensors.FallSettings
import care.ira.watch.sensors.RecoveringFallDetector
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The countdown-overlap defect (D-092, hazard H4). On WEDA-FALL, 20 of 24 missed falls were
 * detected while an earlier alarm's countdown was running, and `FallCountdown` dropped the fall's
 * own detection. If the person then tapped "I'm OK" for the first prompt, the real fall had no
 * countdown and no escalation.
 *
 * Every signal here is synthetic, made up for this test; no dataset is used. The model is a fake
 * [care.ira.watch.sensors.Inference] that scores a window by its largest magnitude. These tests
 * use only the API the watch had before the fix, so they run unchanged against the old code: there,
 * five of the seven fail (the lost fall, the OK that ended the check, and the three outbox tests).
 * The deadline and the unclear-answer tests held before the fix and must keep holding.
 */
class FallCountdownOverlapTest {
    private val hz = 50
    private val spec = FallModelSpec(threshold = 0.95f, mean = listOf(0f, 0f, 0f, 0f), std = listOf(1f, 1f, 1f, 1f),
        mode = FallModelSpec.MODE_RECOVER, candidateG = 2.0f)
    /** The fake model: a window holding a hard impact scores higher than a mild one; both are over 0.95. */
    private val model = care.ira.watch.sensors.Inference { w -> if (w.maxOf { it[3] } >= 4.0f) 0.999f else 0.96f }

    private fun g(i: Int, mag: Double) = AccelSample(i * 1000L / hz, 0.0, 0.0, mag)

    /**
     * Five quiet seconds; at 5 s a sharp 2.3 g spike with arm movement after it (a stumble caught on
     * a chair, say); at 8 s a separate, stronger fall signature (a short free fall, a 4.8 g impact)
     * followed by stillness on the floor.
     */
    private fun stumbleThenFall(): List<AccelSample> {
        val out = mutableListOf<AccelSample>()
        var i = 0
        repeat(5 * hz) { out += g(i++, 1.0 + if (it % 2 == 0) 0.02 else -0.02) }
        out += g(i++, 1.8); out += g(i++, 2.3); out += g(i++, 1.4)
        while (i < 8 * hz) { out += g(i, if (i % 10 < 5) 1.25 else 0.8); i++ }
        repeat(hz / 4) { out += g(i++, 0.3) }
        out += g(i++, 3.9); out += g(i++, 4.8); out += g(i++, 1.6); out += g(i++, 1.2)
        repeat(30 * hz) { out += g(i++, 1.0 + if (it % 2 == 0) 0.03 else -0.03) }
        return out
    }

    private fun name(a: Any) = a::class.simpleName

    @Test
    fun `the real fall 3 s after an earlier alarm is not lost when the person taps I'm OK for the first prompt`() {
        val clock = FakeClock(0)
        val countdown = FallCountdown(clock)
        val detector = RecoveringFallDetector(FallSettings(), spec, model)
        val raised = mutableListOf<Pair<Long, List<FallCountdown.Action>>>()
        for (s in stumbleThenFall()) {
            clock.now = s.atMillis
            val e = detector.feed(s)
            if (e is FallEvent.SuspectedFall) raised += s.atMillis to countdown.suspectedFall(e.peakG, e.stillForSeconds, 30)
            if (s.atMillis == 15_000L) {
                // Seven seconds after the real fall the person taps "I'm OK" (answering the first prompt).
                val ok = countdown.okPressed()
                assertTrue("the first prompt's OK is reported", ok.any { name(it) == "ReportFallOk" })
                assertTrue("the fall detected after the prompt is asked about again, with a fresh countdown",
                    ok.any { it is FallCountdown.Action.ReportFallStarted })
                assertEquals(FallCountdown.State.Counting::class, countdown.state::class)
            }
            countdown.tick()
        }
        assertEquals("the pipeline flags the stumble and the fall separately", 2, raised.size)
        assertTrue(raised[0].second.any { it is FallCountdown.Action.ReportFallStarted })
        assertTrue("the second detection is not silently dropped", raised[1].second.isNotEmpty())
        // Nobody answers the second question: help is asked for at that countdown's end.
        clock.now = 15_000L + 30_000L
        assertTrue(countdown.tick().any { name(it) == "RequestHelp" })
    }

    @Test
    fun `a separate detection during an unanswered countdown never postpones the original deadline`() {
        val clock = FakeClock(1_000_000)
        val c = FallCountdown(clock)
        c.suspectedFall(2.3, 0, 30)
        clock.advance(3_000)
        c.suspectedFall(4.8, 0, 30)
        clock.advance(26_000)
        assertTrue(c.tick().none { name(it) == "RequestHelp" })
        clock.advance(1_000)
        assertTrue("help at the ORIGINAL deadline, 30 s after the first detection", c.tick().any { name(it) == "RequestHelp" })
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `I'm OK after a second detection never ends the safety check`() {
        val clock = FakeClock(1_000_000)
        val c = FallCountdown(clock)
        c.suspectedFall(2.3, 0, 30)
        clock.advance(3_000)
        c.suspectedFall(4.8, 0, 30)
        clock.advance(4_000)
        val ok = c.okPressed()
        assertTrue(ok.any { it is FallCountdown.Action.ReportFallStarted })
        assertEquals(30, c.secondsLeft)
    }

    @Test
    fun `an unclear answer after a second detection cancels nothing, and Help escalates at once`() {
        val clock = FakeClock(1_000_000)
        val c = FallCountdown(clock)
        c.suspectedFall(2.3, 0, 30)
        clock.advance(3_000)
        c.suspectedFall(4.8, 0, 30)
        for (words in listOf("I don't know", "maybe", "ok but my hip is sore", "what")) {
            assertTrue(words, c.heard(words).none { name(it) == "ReportFallOk" })
            assertEquals(FallCountdown.State.Counting::class, c.state::class)
        }
        assertTrue(c.helpPressed().any { name(it) == "RequestHelp" })
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `an event waiting on a retry still goes to the server before a newer one`() {
        val clock = FakeClock(1_000_000)
        val box = Outbox(File.createTempFile("outbox", ".jsonl").also { it.delete() }, clock)
        val ok = box.enqueue(Outbox.KIND_FALL_OK, "/fall/ok", "{}")
        box.failed(ok.idempotencyKey, "SocketTimeoutException")
        clock.advance(1)
        val started = box.enqueue(Outbox.KIND_FALL_STARTED, "/fall", "{}")
        // The second fall's start must never overtake the first fall's "I'm OK".
        assertEquals(listOf(ok.idempotencyKey, started.idempotencyKey), box.due().map { it.idempotencyKey })
    }

    @Test
    fun `events behind one that is backing off wait with it and then go in order`() {
        val clock = FakeClock(1_000_000)
        val box = Outbox(File.createTempFile("outbox", ".jsonl").also { it.delete() }, clock)
        val ok = box.enqueue(Outbox.KIND_FALL_OK, "/fall/ok", "{}")
        clock.advance(1)
        val started = box.enqueue(Outbox.KIND_FALL_STARTED, "/fall", "{}")
        box.failed(ok.idempotencyKey, "SocketTimeoutException")   // the link dropped while sending the OK
        assertTrue("nothing overtakes the OK while it backs off", box.due().isEmpty())
        clock.advance(5_000)
        assertEquals(listOf(ok.idempotencyKey, started.idempotencyKey), box.due().map { it.idempotencyKey })
    }

    @Test
    fun `the same event queued twice under one key is held once`() {
        val box = Outbox(File.createTempFile("outbox", ".jsonl").also { it.delete() }, FakeClock(1_000_000))
        box.enqueue(Outbox.KIND_FALL_STARTED, "/fall", "{}", idempotencyKey = "fall-0001-d1")
        box.enqueue(Outbox.KIND_FALL_STARTED, "/fall", "{}", idempotencyKey = "fall-0001-d1")
        assertEquals(1, box.size())
    }
}
