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
     * The person spoke during the countdown. "help" asks for help now. Only an
     * unambiguous positive with no negative, doubt or distress anywhere in it
     * ("I'm OK", "yes I'm fine", "I'm okay, I just dropped my cup") ends it as the
     * button would. Everything else, including "I'm not OK", "not fine", "I don't
     * know" and "yes, but I can't get up", is never read as OK: help words ask for
     * help, the rest is ignored and the countdown runs on to the alert. The same
     * rule as the server's safety question (care/checkins.read_safety_answer).
     */
    fun heard(text: String?): List<Action> {
        if (state !is State.Counting || text.isNullOrBlank()) return emptyList()
        return when (readSpoken(text)) {
            Spoken.HELP -> helpPressed()
            Spoken.OK -> okPressed()
            Spoken.UNCLEAR -> emptyList()
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

    enum class Spoken { OK, HELP, UNCLEAR }

    companion object {
        const val QUESTION = "It looks like you may have fallen. Are you OK? Tap I'm OK, or Help."
        private val HELP = Regex("""\b(help|emergency|ambulance|cannot get up|can not get up|hurt|hurts|hurting|bleeding|cannot breathe|cannot move|on the floor|fallen|i fell)\b""")
        /** Anything here means the words are not reassurance, whatever else was said. */
        private val NOT_REASSURING = Regex("""\b(no|not|never|nope|nah|but|maybe|think|perhaps|probably|unsure|know|remember|dizzy|faint|pain|sore|ache|aching|sick|unwell|poorly|wobbly|shaky|weak|confused|scared|cannot|stuck|bad|awful|terrible|wrong)\b""")
        private val OK_START = Regex("""^(yes |yeah |yep )?(i am |im )?(ok|okay|fine|alright|all right|good|no problem)\b|^(yes|yeah|yep)$""")

        private val CONTRACTIONS = listOf(
            "can't" to "cannot", "cant" to "cannot", "don't" to "do not", "dont" to "do not", "didn't" to "did not",
            "didnt" to "did not", "i'm" to "i am", "isn't" to "is not", "wasn't" to "was not", "won't" to "will not",
            "haven't" to "have not", "doesn't" to "does not",
        )

        fun normalise(text: String): String {
            var t = text.lowercase().replace('\u2019', '\'').replace('\u2018', '\'')
            for ((from, to) in CONTRACTIONS) t = t.replace(Regex("""\b${Regex.escape(from)}\b"""), to)
            t = t.replace(Regex("""[^a-z0-9' ]+"""), " ").replace("'", "")
            return t.replace(Regex("""\s+"""), " ").trim()
        }

        /** OK only for an unambiguous positive; help for help words; unclear otherwise. */
        fun readSpoken(text: String): Spoken {
            val t = normalise(text)
            if (t.isEmpty()) return Spoken.UNCLEAR
            if (HELP.containsMatchIn(t)) return Spoken.HELP
            val withoutNoProblem = t.replace("no problem", "fine")
            if (NOT_REASSURING.containsMatchIn(withoutNoProblem)) return Spoken.UNCLEAR
            return if (OK_START.containsMatchIn(withoutNoProblem)) Spoken.OK else Spoken.UNCLEAR
        }
    }
}
