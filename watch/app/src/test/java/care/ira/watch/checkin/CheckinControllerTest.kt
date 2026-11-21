package care.ira.watch.checkin

import care.ira.watch.checkin.CheckinController.Action
import care.ira.watch.checkin.CheckinController.State
import care.ira.watch.core.Clock
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FakeClock(var now: Long = 1_000_000) : Clock {
    override fun nowMillis() = now
    fun advance(ms: Long) { now += ms }
}

class CheckinControllerTest {
    private val clock = FakeClock()
    private val c = CheckinController(clock)

    @Test
    fun `asking speaks the question once and listens`() {
        val actions = c.ask("c1", "Hello Tess. Have you eaten?")
        assertEquals(listOf(Action.Speak("Hello Tess. Have you eaten?", louder = false), Action.Listen), actions)
        assertTrue(c.state is State.Asking)
        assertEquals(emptyList<Action>(), c.tick())
    }

    @Test
    fun `after a minute with no answer it repeats louder with vibration and reports`() {
        c.ask("c1", "Q")
        clock.advance(59_000)
        assertEquals(emptyList<Action>(), c.tick())
        clock.advance(1_000)
        val actions = c.tick()
        assertEquals(listOf(Action.Vibrate, Action.Speak("Q", louder = true), Action.Listen, Action.ReportRepeated("c1")), actions)
        assertTrue(c.state is State.Repeating)
    }

    @Test
    fun `two minutes after the repeat it reports a miss`() {
        c.ask("c1", "Q")
        clock.advance(60_000); c.tick()
        clock.advance(119_000)
        assertEquals(emptyList<Action>(), c.tick())
        clock.advance(1_000)
        assertEquals(listOf(Action.ReportMissed("c1")), c.tick())
        assertTrue(c.state is State.Missed)
        assertEquals(null, c.openCheckinId())
    }

    @Test
    fun `an answer by voice is reported and ends the check-in`() {
        c.ask("c1", "Q")
        assertEquals(listOf(Action.ReportAnswer("c1", "Yes I had porridge", "voice")), c.answered(" Yes I had porridge ", "voice"))
        assertTrue(c.state is State.Answered)
        assertEquals(emptyList<Action>(), c.tick())
    }

    @Test
    fun `the OK button answers with empty text`() {
        c.ask("c1", "Q")
        clock.advance(60_000); c.tick()
        assertEquals(listOf(Action.ReportAnswer("c1", "", "button")), c.answered("", "button"))
    }

    @Test
    fun `saying help also asks for help`() {
        c.ask("c1", "Q")
        val actions = c.answered("please help I can't get up", "voice")
        assertEquals(listOf(Action.ReportAnswer("c1", "please help I can't get up", "voice"), Action.RequestHelp("voice")), actions)
    }

    @Test
    fun `the help button works with no check-in open`() {
        assertEquals(listOf(Action.RequestHelp("button")), c.helpPressed())
        assertEquals(emptyList<Action>(), c.answered("hello", "voice"))
    }

    @Test
    fun `help words are recognised and ordinary words are not`() {
        assertTrue(CheckinController.looksLikeHelp("HELP me"))
        assertTrue(CheckinController.looksLikeHelp("call an ambulance"))
        assertTrue(!CheckinController.looksLikeHelp("I'm fine, very helpful day"))
    }
}
