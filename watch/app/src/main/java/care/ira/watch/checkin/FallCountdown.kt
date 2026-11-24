package care.ira.watch.checkin

import care.ira.watch.core.Clock

/**
 * "It looks like you may have fallen. Are you OK?" as a state machine with no
 * Android in it. The watch's fall detector starts it; the service feeds it
 * time and the buttons; it says what to do.
 *
 *   Idle ──suspected fall──▶ Counting ──"I'm OK"──▶ Idle (fall reported as OK)
 *                               │
 *                     countdown ends, or "Help"
 *                               ▼
 *                       Idle (help requested, via "fall", with location)
 *
 * The countdown length is the person's setting (`fall_countdown_s`, 30 by
 * default). The server is told when the countdown starts and keeps its own
 * deadline, so a watch that dies mid-countdown still escalates.
 */
class FallCountdown(private val clock: Clock) {
    sealed class State {
        object Idle : State()
        data class Counting(val startedAt: Long, val countdownMs: Long, val peakG: Double) : State()
    }

    sealed class Action {
        object Vibrate : Action()
        data class Speak(val text: String) : Action()
        /** Tell the server the countdown has started (it opens an "asking" alert). */
        data class ReportFallStarted(val peakG: Double, val stillSeconds: Int, val countdownS: Int) : Action()
        /** The person said they are OK: the server resolves the alert. */
        object ReportFallOk : Action()
        /** The countdown ended or Help was pressed: an SOS with location. */
        object RequestHelp : Action()
    }

    var state: State = State.Idle
        private set
    private var lastSpokenSecond = -1

    val secondsLeft: Int?
        get() = (state as? State.Counting)?.let { s ->
            ((s.countdownMs - (clock.nowMillis() - s.startedAt) + 999) / 1000).toInt().coerceAtLeast(0)
        }

    fun suspectedFall(peakG: Double, stillSeconds: Int, countdownS: Int): List<Action> {
        if (state is State.Counting) return emptyList()
        state = State.Counting(clock.nowMillis(), countdownS * 1000L, peakG)
        lastSpokenSecond = -1
        return listOf(
            Action.Vibrate,
            Action.Speak(QUESTION),
            Action.ReportFallStarted(peakG, stillSeconds, countdownS),
        )
    }

    /** Time passed. Repeats the question and a vibration every 10 seconds; help at zero. */
    fun tick(): List<Action> {
        val s = state as? State.Counting ?: return emptyList()
        val left = secondsLeft ?: 0
        if (left <= 0) {
            state = State.Idle
            return listOf(Action.RequestHelp)
        }
        if (left % 10 == 0 && left != lastSpokenSecond && left < s.countdownMs / 1000) {
            lastSpokenSecond = left
            return listOf(Action.Vibrate, Action.Speak("$QUESTION $left seconds."))
        }
        return emptyList()
    }

    /**
     * The person spoke during the countdown. "I'm OK", "I'm fine", "I'm all right"
     * end it as the button would; "help" asks for help now; anything else is
     * ignored and the countdown runs on. Nothing else is inferred from words.
     */
    fun heard(text: String?): List<Action> {
        if (state !is State.Counting || text.isNullOrBlank()) return emptyList()
        return when {
            HELP.containsMatchIn(text) -> helpPressed()
            OK.containsMatchIn(text) -> okPressed()
            else -> emptyList()
        }
    }

    fun okPressed(): List<Action> {
        if (state !is State.Counting) return emptyList()
        state = State.Idle
        return listOf(Action.ReportFallOk)
    }

    fun helpPressed(): List<Action> {
        if (state !is State.Counting) return emptyList()
        state = State.Idle
        return listOf(Action.RequestHelp)
    }

    companion object {
        const val QUESTION = "It looks like you may have fallen. Are you OK? Tap I'm OK, or Help."
        private val HELP = Regex("""\b(help|emergency|ambulance|can'?t get up|cant get up|hurt)\b""", RegexOption.IGNORE_CASE)
        private val OK = Regex("""\b(i'?m (ok|okay|fine|all ?right|alright|good)|(ok|okay|fine|all ?right|alright)\b|yes|no problem|didn'?t fall|just dropped)""", RegexOption.IGNORE_CASE)
    }
}
