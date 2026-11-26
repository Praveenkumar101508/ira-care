package care.ira.watch.checkin

import care.ira.watch.core.Clock

/**
 * The check-in conversation on the watch, as a state machine with no Android in it.
 *
 *   Idle ──due──▶ Asking ──no answer after repeatAfterMs──▶ Repeating
 *                   │                                          │
 *                answered                        no answer after missAfterMs
 *                   ▼                                          ▼
 *                Answered                                    Missed
 *
 * The service feeds it time (`tick`) and events (`answered`, `helpPressed`);
 * it says what to do next through `Action`s. The timings come from the
 * person's settings on the server; the defaults match the specification
 * (repeat after 1 minute, give up 2 minutes later).
 */
class CheckinController(
    private val clock: Clock,
    private var repeatAfterMs: Long = 60_000,
    private var missAfterMs: Long = 120_000,
    /** A safety question repeats and gives up sooner: the server opens the SOS at its own deadline. */
    private val safetyRepeatAfterMs: Long = 20_000,
    private val safetyMissAfterMs: Long = 25_000,
) {
    sealed class State {
        object Idle : State()
        data class Asking(val checkinId: String, val question: String, val askedAt: Long, val safety: Boolean = false,
                          val reminderKind: String? = null) : State()
        data class Repeating(val checkinId: String, val question: String, val repeatedAt: Long, val safety: Boolean = false,
                             val reminderKind: String? = null) : State()
        data class Answered(val checkinId: String, val text: String, val via: String) : State()
        data class Missed(val checkinId: String) : State()
    }

    sealed class Action {
        data class Speak(val text: String, val louder: Boolean) : Action()
        object Vibrate : Action()
        object Listen : Action()
        data class ReportRepeated(val checkinId: String) : Action()
        /** `choice` is a reminder's button ("done", "later", "skip"); null for words or "I'm OK". */
        data class ReportAnswer(val checkinId: String, val text: String, val via: String, val choice: String? = null) : Action()
        data class ReportMissed(val checkinId: String) : Action()
        /** `incidentId` is set when the request is a fall countdown's (D-092). */
        data class RequestHelp(val via: String, val incidentId: String? = null) : Action()
        object Nothing : Action()
    }

    var state: State = State.Idle
        private set

    /** How many times an ordinary question is repeated before it counts as missed (the person's prompting level). */
    var repeats: Int = 1

    /**
     * The person's communication profile (care/personal.py): light prompting waits
     * longer and does not repeat; full prompting repeats sooner and twice.
     */
    fun configure(repeatAfterS: Int, repeats: Int) {
        repeatAfterMs = repeatAfterS.coerceIn(20, 300) * 1000L
        this.repeats = repeats.coerceIn(0, 2)
        missAfterMs = if (this.repeats == 0) repeatAfterMs * 2 else 120_000L
    }

    /**
     * A check-in came due: speak it and listen. A safety question vibrates first and is spoken loud.
     * `reminderKind` is set when the question is a reminder (the server sent its `reminder_id`):
     * the screen then shows the reminder's buttons (ReminderButtons).
     */
    fun ask(checkinId: String, question: String, safety: Boolean = false, reminderKind: String? = null): List<Action> {
        state = State.Asking(checkinId, question, clock.nowMillis(), safety, if (safety) null else reminderKind)
        return if (safety) listOf(Action.Vibrate, Action.Speak(question, louder = true), Action.Listen)
        else listOf(Action.Speak(question, louder = false), Action.Listen)
    }

    /** Time passed; returns what to do, if anything. */
    fun tick(): List<Action> {
        val now = clock.nowMillis()
        return when (val s = state) {
            is State.Asking -> if (now - s.askedAt >= (if (s.safety) safetyRepeatAfterMs else repeatAfterMs)) {
                if (!s.safety && repeats == 0) {
                    // Light prompting: one quiet question, then it is simply missed; the server decides what that means.
                    state = State.Missed(s.checkinId)
                    listOf(Action.ReportMissed(s.checkinId))
                } else {
                    state = State.Repeating(s.checkinId, s.question, now, s.safety, s.reminderKind)
                    listOf(Action.Vibrate, Action.Speak(s.question, louder = true), Action.Listen, Action.ReportRepeated(s.checkinId))
                }
            } else emptyList()
            is State.Repeating -> if (now - s.repeatedAt >= (if (s.safety) safetyMissAfterMs else missAfterMs)) {
                state = State.Missed(s.checkinId)
                listOf(Action.ReportMissed(s.checkinId))
            } else emptyList()
            else -> emptyList()
        }
    }

    /** The person spoke or pressed "I'm OK". Empty text with via=button is a bare OK. */
    fun answered(text: String, via: String): List<Action> {
        val id = openCheckinId() ?: return emptyList()
        val trimmed = text.trim()
        if (via == "voice" && looksLikeHelp(trimmed)) {
            state = State.Answered(id, trimmed, via)
            return listOf(Action.ReportAnswer(id, trimmed, via), Action.RequestHelp("voice"))
        }
        state = State.Answered(id, trimmed, via)
        return listOf(Action.ReportAnswer(id, trimmed, via))
    }

    /**
     * A reminder's button. Only for an open reminder, and only a button that reminder offers:
     * "Done" pressed on "How are you?" is not an answer to anything, so nothing is sent.
     */
    fun chose(choice: ReminderChoice): List<Action> {
        val id = openCheckinId() ?: return emptyList()
        if (choice !in ReminderButtons.choices(openReminderKind())) return emptyList()
        state = State.Answered(id, "", "button")
        return listOf(Action.ReportAnswer(id, "", "button", choice.wire))
    }

    /** The open question's reminder kind, or null when no reminder is being asked. */
    fun openReminderKind(): String? = when (val s = state) {
        is State.Asking -> s.reminderKind
        is State.Repeating -> s.reminderKind
        else -> null
    }

    /** The Help button, at any time, even with no check-in open. */
    fun helpPressed(): List<Action> = listOf(Action.RequestHelp("button"))

    fun reset() {
        state = State.Idle
    }

    fun openCheckinId(): String? = when (val s = state) {
        is State.Asking -> s.checkinId
        is State.Repeating -> s.checkinId
        else -> null
    }

    companion object {
        private val HELP = Regex("""\b(help|emergency|ambulance|can'?t get up)\b""", RegexOption.IGNORE_CASE)
        fun looksLikeHelp(text: String): Boolean = HELP.containsMatchIn(text)
    }
}
