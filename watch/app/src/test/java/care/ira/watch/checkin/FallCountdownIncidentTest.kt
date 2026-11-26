package care.ira.watch.checkin

import care.ira.watch.checkin.FallCountdown.Action
import care.ira.watch.outbox.FallOutbox
import care.ira.watch.outbox.Outbox
import java.io.File
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * D-092: a fall countdown is one incident with one id. The same impact seen twice is absorbed; a
 * separate detection is attached to the incident, asked about again and never moves the deadline;
 * "I'm OK" covers only what was detected before the question; Help escalates at once; an unclear
 * answer cancels nothing; the outbox keeps the incident's events in order and holds each once.
 * Times are synthetic (sample time in ms); nothing here comes from a dataset.
 */
class FallCountdownIncidentTest {
    private val clock = FakeClock(1_000_000L)
    private var ids = 0
    private val c = FallCountdown(clock) { "incident-${++ids}" }
    private fun counting() = c.state as FallCountdown.State.Counting

    @Test
    fun `the same impact seen twice is absorbed, with no new report and no new countdown`() {
        c.suspectedFall(3.1, 20, 30, impactAtMillis = 10_000)
        clock.advance(2_000)
        assertTrue(c.suspectedFall(3.4, 0, 30, impactAtMillis = 10_000 + FallCountdown.SAME_IMPACT_MS).isEmpty())
        assertTrue(c.suspectedFall(3.4, 0, 30, impactAtMillis = 10_000 - 300).isEmpty())
        assertEquals(2, counting().sameImpact)
        assertEquals(0, counting().laterDetections)
        assertEquals(28, c.secondsLeft)
        // "I'm OK" then ends it: nothing separate was detected.
        assertEquals(listOf(Action.ReportFallOk("incident-1")), c.okPressed())
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `a separate detection is attached to the incident, asked about again, and leaves the deadline alone`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        clock.advance(3_000)
        val second = c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)
        assertEquals(listOf(Action.Vibrate, Action.Speak(FallCountdown.ASK_AGAIN),
            Action.ReportFallStarted(4.8, 0, 30, "incident-1", detection = 2)), second)
        assertEquals(27, c.secondsLeft)
        clock.advance(2_000)
        // A third is attached too, without saying the question over again.
        assertEquals(listOf(Action.Vibrate, Action.ReportFallStarted(5.0, 0, 30, "incident-1", detection = 3)),
            c.suspectedFall(5.0, 0, 30, impactAtMillis = 15_000))
        assertEquals("incident-1", counting().incidentId)
        assertEquals(2, counting().laterDetections)
        assertEquals(25, c.secondsLeft)
    }

    @Test
    fun `a detection with no impact time is never taken for the same impact`() {
        c.suspectedFall(3.0, 20, 30, impactAtMillis = 10_000)
        assertTrue(c.suspectedFall(3.0, 20, 30).any { it is Action.ReportFallStarted })
        assertEquals(1, counting().laterDetections)
    }

    @Test
    fun `I'm OK after a separate detection reports the first incident and asks again with a fresh countdown`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        clock.advance(3_000)
        c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)
        clock.advance(4_000)
        assertEquals(listOf(Action.ReportFallOk("incident-1"), Action.Vibrate, Action.Speak(FallCountdown.ASK_AGAIN),
            Action.ReportFallStarted(4.8, 0, 30, "incident-2")), c.okPressed())
        assertEquals("incident-2", counting().incidentId)
        assertEquals(13_000L, counting().triggerImpactAt)
        assertEquals(30, c.secondsLeft)
        // Nobody answers the second question: help for the second incident at its deadline.
        clock.advance(29_000); assertTrue(c.tick().none { it is Action.RequestHelp })
        clock.advance(1_000); assertEquals(listOf(Action.RequestHelp("incident-2")), c.tick())
        // The second OK, with nothing new detected, ends it.
        c.suspectedFall(3.0, 20, 30, impactAtMillis = 90_000)
        assertEquals(listOf(Action.ReportFallOk("incident-3")), c.okPressed())
    }

    @Test
    fun `spoken OK after a separate detection also asks again`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)
        val heard = c.heard("I'm fine")
        assertEquals(Action.ReportFallOk("incident-1"), heard.first())
        assertTrue(heard.contains(Action.ReportFallStarted(4.8, 0, 30, "incident-2")))
    }

    @Test
    fun `with no answer, help is asked for once, at the original deadline, however many detections arrive`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 0)
        var helps = 0
        var reported = 0
        for (t in 1..40) {
            clock.advance(1_000)
            if (t % 2 == 0 && t < 30) reported += c.suspectedFall(4.0, 0, 30, impactAtMillis = t * 1_000L)
                .count { it is Action.ReportFallStarted }
            val actions = c.tick()
            helps += actions.count { it is Action.RequestHelp }
            if (actions.any { it is Action.RequestHelp }) assertEquals("help at 30 s, not later", 30, t)
        }
        assertEquals(1, helps)
        assertEquals("every separate detection was attached to the one incident", 14, reported)
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `an unclear answer cancels nothing and keeps the later detection pending`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        clock.advance(3_000)
        c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)
        for (words in listOf("I don't know", "not sure", "yes but I can't stand", "maybe", "")) {
            assertTrue(words, c.heard(words).isEmpty())
            assertEquals(words, "incident-1", counting().incidentId)
        }
        assertEquals(1, counting().laterDetections)
        assertEquals(27, c.secondsLeft)
        assertTrue(c.okPressed().any { it is Action.ReportFallStarted })
    }

    @Test
    fun `Help during a countdown with later detections escalates the incident at once`() {
        c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)
        assertEquals(listOf(Action.RequestHelp("incident-1")), c.helpPressed())
        assertEquals(FallCountdown.State.Idle, c.state)
        assertTrue(c.okPressed().isEmpty())
        assertNull(c.secondsLeft)
    }

    private fun box() = Outbox(File.createTempFile("outbox", ".jsonl").also { it.delete() }, clock)
    private fun body(e: care.ira.watch.outbox.UrgentEvent) = Json.parseToJsonElement(e.body).jsonObject

    /** Writes each fall action to the outbox the way the service does. */
    private fun write(outbox: Outbox, actions: List<Action>) {
        for (a in actions) when (a) {
            is Action.ReportFallStarted -> FallOutbox.started(outbox, a)
            is Action.ReportFallOk -> FallOutbox.ok(outbox, a)
            is Action.RequestHelp -> FallOutbox.help(outbox, a.incidentId, "fall", null)
            else -> {}
        }
    }

    @Test
    fun `offline, the incident's events keep their order and identity, and a repeat is held once`() {
        val file = File.createTempFile("outbox", ".jsonl").also { it.delete() }
        val outbox = Outbox(file, clock)
        val first = c.suspectedFall(2.3, 0, 30, impactAtMillis = 10_000)
        write(outbox, first); clock.advance(3_000)
        write(outbox, c.suspectedFall(4.8, 0, 30, impactAtMillis = 13_000)); clock.advance(4_000)
        // The link is down: the first attempt fails and the queue backs off.
        outbox.failed(outbox.pending().first().idempotencyKey, "UnknownHostException")
        write(outbox, c.okPressed())
        write(outbox, first)   // the same action written again (a retry of the service's step)
        val keys = listOf("fall-incident-1-d1", "fall-incident-1-d2", "fall-incident-1-ok", "fall-incident-2-d1")
        assertEquals(keys, outbox.pending().map { it.idempotencyKey })
        // A restart reads the same events, in the same order, with the same keys.
        val reopened = Outbox(file, clock)
        assertEquals(keys, reopened.pending().map { it.idempotencyKey })
        // Reconnected: everything goes, oldest first, the OK before the next incident's start.
        assertEquals(keys, reopened.due().map { it.idempotencyKey })
        val events = reopened.pending()
        assertEquals("incident-1", body(events[1]).getValue("incident_id").jsonPrimitive.content)
        assertEquals(2, body(events[1]).getValue("detection").jsonPrimitive.int)
        assertEquals("incident-1", body(events[2]).getValue("incident_id").jsonPrimitive.content)
        assertEquals(listOf(Outbox.KIND_FALL_STARTED, Outbox.KIND_FALL_STARTED, Outbox.KIND_FALL_OK, Outbox.KIND_FALL_STARTED),
            events.map { it.kind })
    }

    @Test
    fun `a fall incident's Help is one event, and carries the incident`() {
        val outbox = box()
        FallOutbox.help(outbox, "incident-9", "fall", null)
        FallOutbox.help(outbox, "incident-9", "fall", null)
        val e = outbox.pending().single()
        assertEquals("fall-incident-9-help", e.idempotencyKey)
        assertEquals("incident-9", body(e).getValue("incident_id").jsonPrimitive.content)
        assertEquals("fall", body(e).getValue("via").jsonPrimitive.content)
        assertFalse("no location is written when there is none", body(e).containsKey("location"))
    }
}
