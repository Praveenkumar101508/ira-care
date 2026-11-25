package care.ira.watch.checkin

import care.ira.watch.api.CheckinAnswer
import care.ira.watch.api.DueCheckin
import care.ira.watch.outbox.Outbox
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Reminders answered on the wrist (D1.h): which buttons, when, and what is sent. */
class ReminderButtonsTest {
    private val clock = FakeClock()
    private val json = Json { explicitNulls = false; ignoreUnknownKeys = true }

    @Test fun eachKindOfReminderHasItsButtonsAndAnOrdinaryQuestionHasNone() {
        assertEquals(listOf(ReminderChoice.DONE, ReminderChoice.LATER, ReminderChoice.SKIP), ReminderButtons.choices("medication"))
        assertEquals(listOf(ReminderChoice.DONE, ReminderChoice.LATER, ReminderChoice.SKIP), ReminderButtons.choices("hydration"))
        assertEquals(listOf(ReminderChoice.DONE, ReminderChoice.LATER), ReminderButtons.choices("reminder"))
        assertEquals(emptyList<ReminderChoice>(), ReminderButtons.choices(null))
    }

    @Test fun everyButtonIsDescribedForAScreenReaderInWordsThatStartWithItsLabel() {
        for (c in ReminderChoice.entries) {
            assertTrue(c.name, c.description.startsWith(c.label + ".") && c.description.length > c.label.length + 10)
        }
    }

    @Test fun aFallCountdownOrALiveSosOutranksAReminderAsOnTheFace() {
        assertEquals(emptyList<ReminderChoice>(), ReminderButtons.visible("medication", fallCountdown = 12, sosActive = false))
        assertEquals(emptyList<ReminderChoice>(), ReminderButtons.visible("medication", fallCountdown = null, sosActive = true))
        assertEquals(3, ReminderButtons.visible("medication", fallCountdown = null, sosActive = false).size)
        // And the face's own words agree: a countdown and a live SOS come before the question.
        assertTrue(care.ira.watch.ui.FaceText.main(9, null, "Have you taken your tablets?", null, null, true).startsWith("Did you fall?"))
        assertEquals("Help is on its way.", care.ira.watch.ui.FaceText.main(null, "Help is on its way.", "Have you taken your tablets?", null, null, true))
    }

    @Test fun theDueCheckInSaysWhenItIsAReminder() {
        val due = json.decodeFromString<DueCheckin>(
            """{"id":"c1","status":"asked","question_kind":"medication","question_text":"Tess, have you taken it?","reminder_id":"r1"}""")
        assertEquals("medication", ReminderButtons.reminderKindOf(due))
        val plain = json.decodeFromString<DueCheckin>(
            """{"id":"c2","status":"asked","question_kind":"medication","question_text":"Have you had your tablets?"}""")
        assertNull("a scheduled medication question is not a reminder", ReminderButtons.reminderKindOf(plain))
    }

    @Test fun aButtonAnswersAnOpenReminderWithItsChoice() {
        val c = CheckinController(clock)
        c.ask("c1", "Have you taken your tablets?", reminderKind = "medication")
        assertEquals("medication", c.openReminderKind())
        val actions = c.chose(ReminderChoice.DONE)
        assertEquals(listOf(CheckinController.Action.ReportAnswer("c1", "", "button", "done")), actions)
        assertNull("answered: nothing open", c.openCheckinId())
    }

    @Test fun theReminderStaysAReminderWhenTheQuestionIsRepeated() {
        val c = CheckinController(clock)
        c.ask("c1", "Shall I tick that off?", reminderKind = "reminder")
        clock.advance(61_000)
        c.tick()
        assertTrue(c.state is CheckinController.State.Repeating)
        assertEquals("reminder", c.openReminderKind())
        assertEquals("later", (c.chose(ReminderChoice.LATER).single() as CheckinController.Action.ReportAnswer).choice)
    }

    @Test fun aButtonThatIsNotTheRemindersOrNoReminderAtAllSendsNothing() {
        val c = CheckinController(clock)
        c.ask("c1", "How are you today?")
        assertEquals(emptyList<CheckinController.Action>(), c.chose(ReminderChoice.DONE))
        assertEquals("c1", c.openCheckinId())
        val r = CheckinController(clock)
        r.ask("c2", "Shall I tick that off?", reminderKind = "reminder")
        assertEquals("a plain reminder has no 'Not today'", emptyList<CheckinController.Action>(), r.chose(ReminderChoice.SKIP))
        assertEquals(emptyList<CheckinController.Action>(), CheckinController(clock).chose(ReminderChoice.DONE))
    }

    @Test fun aSafetyQuestionIsNeverAnsweredWithAReminderButton() {
        val c = CheckinController(clock)
        c.ask("c1", "Are you OK?", safety = true, reminderKind = "medication")
        assertNull(c.openReminderKind())
        assertEquals(emptyList<CheckinController.Action>(), c.chose(ReminderChoice.DONE))
    }

    @Test fun aPressIsQueuedOfflineWithItsChoiceAndItsOwnKeyAndSurvivesARestart() {
        val file = File.createTempFile("outbox", ".jsonl").also { it.delete() }
        val box = Outbox(file, clock)
        val body = json.encodeToString(CheckinAnswer("", "button", "skip"))
        val event = box.enqueue(Outbox.KIND_ANSWER, "/api/v1/care/devices/me/checkins/c1/answer", body)
        val reopened = Outbox(file, clock).pending().single()
        assertEquals(event.idempotencyKey, reopened.idempotencyKey)
        // Encoded exactly as the service encodes it (explicitNulls = false): the choice travels, a null does not.
        assertEquals(CheckinAnswer("", "button", "skip"), json.decodeFromString<CheckinAnswer>(reopened.body))
        assertTrue(reopened.body.contains(""""choice":"skip""""))
        // Words and "I'm OK" carry no choice at all.
        assertTrue(!json.encodeToString(CheckinAnswer("", "button")).contains("choice"))
    }
}
