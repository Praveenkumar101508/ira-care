package care.ira.watch.checkin

import care.ira.watch.checkin.FallCountdown.Action
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FallCountdownTest {
    private val clock = FakeClock(1_000_000L)
    private var ids = 0
    private val c = FallCountdown(clock) { "incident-${++ids}" }

    @Test
    fun `a suspected fall vibrates, asks, tells the server, and counts down to help`() {
        val actions = c.suspectedFall(3.4, 20, 30)
        assertEquals(listOf(Action.Vibrate, Action.Speak(FallCountdown.QUESTION), Action.ReportFallStarted(3.4, 20, 30, "incident-1")), actions)
        assertEquals(30, c.secondsLeft)
        clock.advance(10_000)
        assertEquals(listOf(Action.Vibrate, Action.Speak("${FallCountdown.QUESTION} 20 seconds.")), c.tick())
        assertTrue(c.tick().isEmpty())
        clock.advance(20_000)
        assertEquals(listOf(Action.RequestHelp("incident-1")), c.tick())
        assertEquals(null, c.secondsLeft)
    }

    @Test
    fun `I'm OK cancels and tells the server, help goes straight to the SOS`() {
        c.suspectedFall(3.0, 20, 30)
        clock.advance(5_000)
        assertEquals(listOf(Action.ReportFallOk("incident-1")), c.okPressed())
        assertTrue(c.tick().isEmpty())
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(Action.RequestHelp("incident-2")), c.helpPressed())
        assertTrue(c.okPressed().isEmpty())
    }

    @Test
    fun `the same impact detected twice during the countdown changes nothing`() {
        // Until D-092 every second detection was ignored; now only one of the same impact is
        // (FallCountdownIncidentTest covers separate detections).
        c.suspectedFall(3.0, 20, 30, impactAtMillis = 50_000)
        assertTrue(c.suspectedFall(5.0, 20, 30, impactAtMillis = 50_400).isEmpty())
        assertEquals(1, (c.state as FallCountdown.State.Counting).sameImpact)
    }
}

class FallCountdownVoiceTest {
    private val clock = FakeClock(1_000_000L)
    private val c = FallCountdown(clock) { "incident" }

    @Test
    fun `saying I'm OK during the countdown ends it like the button`() {
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(FallCountdown.Action.ReportFallOk("incident")), c.heard("I'm okay, I just dropped my cup"))
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `saying help asks for help now`() {
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(FallCountdown.Action.RequestHelp("incident")), c.heard("help me"))
    }

    @Test
    fun `other words, silence, and words outside a countdown change nothing`() {
        assertTrue(c.heard("I'm fine").isEmpty())
        c.suspectedFall(3.0, 20, 30)
        assertTrue(c.heard("what was that noise").isEmpty())
        assertTrue(c.heard(null).isEmpty())
        assertEquals(30, c.secondsLeft)
    }

    @Test
    fun `a negative, a doubt or trouble never ends the countdown as OK`() {
        for (words in listOf("I'm not OK", "I\u2019m not ok", "not fine", "I'm not okay at all", "I don't know",
                             "maybe", "I can't remember", "I'm dizzy", "ok but my hip is sore", "no")) {
            c.suspectedFall(3.0, 20, 30)
            assertTrue(words, c.heard(words).isEmpty())
            assertEquals(words, FallCountdown.State.Counting::class, c.state::class)
            c.helpPressed()
        }
    }

    @Test
    fun `trouble with a yes in it asks for help`() {
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(FallCountdown.Action.RequestHelp("incident")), c.heard("Yes, but I can't get up"))
    }

    @Test
    fun `a clear positive still ends it`() {
        for (words in listOf("I'm OK", "yes I'm fine", "I'm all right", "okay", "yes", "no problem")) {
            c.suspectedFall(3.0, 20, 30)
            assertEquals(words, listOf(FallCountdown.Action.ReportFallOk("incident")), c.heard(words))
        }
    }
}
