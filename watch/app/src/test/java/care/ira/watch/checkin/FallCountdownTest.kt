package care.ira.watch.checkin

import care.ira.watch.checkin.FallCountdown.Action
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FallCountdownTest {
    private val clock = FakeClock(1_000_000L)
    private val c = FallCountdown(clock)

    @Test
    fun `a suspected fall vibrates, asks, tells the server, and counts down to help`() {
        val actions = c.suspectedFall(3.4, 20, 30)
        assertEquals(listOf(Action.Vibrate, Action.Speak(FallCountdown.QUESTION), Action.ReportFallStarted(3.4, 20, 30)), actions)
        assertEquals(30, c.secondsLeft)
        clock.advance(10_000)
        assertEquals(listOf(Action.Vibrate, Action.Speak("${FallCountdown.QUESTION} 20 seconds.")), c.tick())
        assertTrue(c.tick().isEmpty())
        clock.advance(20_000)
        assertEquals(listOf(Action.RequestHelp), c.tick())
        assertEquals(null, c.secondsLeft)
    }

    @Test
    fun `I'm OK cancels and tells the server, help goes straight to the SOS`() {
        c.suspectedFall(3.0, 20, 30)
        clock.advance(5_000)
        assertEquals(listOf(Action.ReportFallOk), c.okPressed())
        assertTrue(c.tick().isEmpty())
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(Action.RequestHelp), c.helpPressed())
        assertTrue(c.okPressed().isEmpty())
    }

    @Test
    fun `a second suspected fall during the countdown changes nothing`() {
        c.suspectedFall(3.0, 20, 30)
        assertTrue(c.suspectedFall(5.0, 20, 30).isEmpty())
    }
}

class FallCountdownVoiceTest {
    private val clock = FakeClock(1_000_000L)
    private val c = FallCountdown(clock)

    @Test
    fun `saying I'm OK during the countdown ends it like the button`() {
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(FallCountdown.Action.ReportFallOk), c.heard("I'm okay, I just dropped my cup"))
        assertEquals(FallCountdown.State.Idle, c.state)
    }

    @Test
    fun `saying help asks for help now`() {
        c.suspectedFall(3.0, 20, 30)
        assertEquals(listOf(FallCountdown.Action.RequestHelp), c.heard("help me"))
    }

    @Test
    fun `other words, silence, and words outside a countdown change nothing`() {
        assertTrue(c.heard("I'm fine").isEmpty())
        c.suspectedFall(3.0, 20, 30)
        assertTrue(c.heard("what was that noise").isEmpty())
        assertTrue(c.heard(null).isEmpty())
        assertEquals(30, c.secondsLeft)
    }
}
