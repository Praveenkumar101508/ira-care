package care.ira.watch.checkin

import care.ira.watch.core.Clock

/**
 * "It looks like you may have fallen. Are you OK?" as a state machine with no
 * Android in it. The watch's fall detector starts it; the service feeds it
 * time and the buttons; it says what to do.
 *
 *   Idle ──suspected fall──▶ Counting ──"I'm OK"──▶ Idle (fall reported as OK)
 *                             │    ▲         │
 *                             │    └─────────┘ "I'm OK", but another fall was detected after the
 *                             │                question was asked: asked again, fresh countdown (D-092)
 *                     countdown ends, or "Help"
 *                             ▼
 *                     Idle (help requested, via "fall", with location)
 *
 * The countdown length is the person's setting (`fall_countdown_s`, 30 by
 * default). The server is told when the countdown starts and keeps its own
 * deadline, so a watch that dies mid-countdown still escalates.
 *
 * A detection while the countdown runs is never dropped (D-092, hazard H4):
 *
 *  * one whose impact lies within [SAME_IMPACT_MS] of the impact that started the countdown is the
 *    same impact seen twice: it is counted on the incident and changes nothing else;
 *  * any other is a separate detection: it is attached to the same incident (the server is told,
 *    under the incident's id, and opens nothing new), the wrist vibrates and asks again, and the
 *    deadline does NOT move: with no answer, help is asked for at the original deadline;
 *  * "I'm OK" covers only what was detected before the question it answers was asked. If a
 *    separate detection came after that, the OK is reported for the first incident and the
 *    person is asked again at once, with a fresh countdown and a new incident id. Each fresh
 *    countdown needs a new explicit OK, so nothing can be put off without the person answering.
 *
 * Every incident has an id, sent with each of its events; the outbox keys are derived from it
 * (`FallOutbox`), so a retry or a repeat never opens a second alert on the server.
 */
class FallCountdown(
    private val clock: Clock,
    private val newIncidentId: () -> String = { java.util.UUID.randomUUID().toString() },
) {
    sealed class State {
        object Idle : State()
        data class Counting(
            val startedAt: Long,
            val countdownMs: Long,
            val peakG: Double,
            /** The incident this countdown is asking about; every event for it carries this id. */
            val incidentId: String = "",
            /** The impact (sample time) that started this countdown, when the detector gave one. */
            val triggerImpactAt: Long? = null,
            /** Detections of that same impact, counted and otherwise ignored. */
            val sameImpact: Int = 0,
            /** Separate detections attached to this incident since the question was asked. */
            val laterDetections: Int = 0,
            /** The latest of those, which a fresh countdown asks about after an OK. */
            val lastDetection: Detection? = null,
        ) : State()
    }

    data class Detection(val impactAt: Long?, val peakG: Double, val stillSeconds: Int)

    sealed class Action {
        object Vibrate : Action()
        data class Speak(val text: String) : Action()
        /**
         * Tell the server about a detection of `incidentId`: the first (`detection` 1) opens an
         * "asking" alert; a later one is attached to the same alert and leaves its deadline alone.
         */
        data class ReportFallStarted(val peakG: Double, val stillSeconds: Int, val countdownS: Int,
                                     val incidentId: String = "", val detection: Int = 1) : Action()
        /** The person said they are OK: the server resolves the alert of `incidentId`. */
        data class ReportFallOk(val incidentId: String) : Action()
        /** The countdown ended or Help was pressed: an SOS with location, for `incidentId`. */
        data class RequestHelp(val incidentId: String) : Action()
    }

    @Volatile
    var state: State = State.Idle
        private set
    private var lastSpokenSecond = -1

    val secondsLeft: Int?
        get() = (state as? State.Counting)?.let { s ->
            ((s.countdownMs - (clock.nowMillis() - s.startedAt) + 999) / 1000).toInt().coerceAtLeast(0)
        }

    /**
     * The detector raised a suspected fall. `impactAtMillis` is the impact (rule) or peak (model)
     * in sample time, used only to tell the same impact from a separate one; null counts as separate.
     */
    @Synchronized
    fun suspectedFall(peakG: Double, stillSeconds: Int, countdownS: Int, impactAtMillis: Long? = null): List<Action> {
        val s = state as? State.Counting ?: return start(Detection(impactAtMillis, peakG, stillSeconds), countdownS * 1000L, QUESTION)
        if (impactAtMillis != null && s.triggerImpactAt != null && Math.abs(impactAtMillis - s.triggerImpactAt) <= SAME_IMPACT_MS) {
            state = s.copy(sameImpact = s.sameImpact + 1)
            return emptyList()
        }
        val n = s.laterDetections + 1
        state = s.copy(laterDetections = n, lastDetection = Detection(impactAtMillis, peakG, stillSeconds))
        val out = mutableListOf<Action>(Action.Vibrate)
        if (n == 1) out += Action.Speak(ASK_AGAIN)
        // The same incident, the same deadline: the server attaches it to the alert it already holds.
        out += Action.ReportFallStarted(peakG, stillSeconds, (s.countdownMs / 1000).toInt(), s.incidentId, detection = n + 1)
        return out
    }

    private fun start(d: Detection, countdownMs: Long, question: String): List<Action> {
        val id = newIncidentId()
        state = State.Counting(clock.nowMillis(), countdownMs, d.peakG, id, d.impactAt)
        lastSpokenSecond = -1
        return listOf(
            Action.Vibrate,
            Action.Speak(question),
            Action.ReportFallStarted(d.peakG, d.stillSeconds, (countdownMs / 1000).toInt(), id),
        )
    }

    /** Time passed. Repeats the question and a vibration every 10 seconds; help at zero. */
    @Synchronized
    fun tick(): List<Action> {
        val s = state as? State.Counting ?: return emptyList()
        val left = secondsLeft ?: 0
        if (left <= 0) {
            // The original deadline: later detections never move it.
            state = State.Idle
            return listOf(Action.RequestHelp(s.incidentId))
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
    @Synchronized
    fun heard(text: String?): List<Action> {
        if (state !is State.Counting || text.isNullOrBlank()) return emptyList()
        return when (readSpoken(text)) {
            Spoken.HELP -> helpPressed()
            Spoken.OK -> okPressed()
            Spoken.UNCLEAR -> emptyList()
        }
    }

    /**
     * "I'm OK" (the button or clear words). It covers what was detected before the question was
     * asked. A separate detection after that is not covered: the person is asked again (D-092).
     */
    @Synchronized
    fun okPressed(): List<Action> {
        val s = state as? State.Counting ?: return emptyList()
        val ok = Action.ReportFallOk(s.incidentId)
        val later = s.lastDetection
        if (s.laterDetections == 0 || later == null) {
            state = State.Idle
            return listOf(ok)
        }
        return listOf(ok) + start(later, s.countdownMs, ASK_AGAIN)
    }

    /** Help, at any moment of the countdown: the SOS at once, whatever else was detected. */
    @Synchronized
    fun helpPressed(): List<Action> {
        val s = state as? State.Counting ?: return emptyList()
        state = State.Idle
        return listOf(Action.RequestHelp(s.incidentId))
    }

    enum class Spoken { OK, HELP, UNCLEAR }

    companion object {
        const val QUESTION = "It looks like you may have fallen. Are you OK? Tap I'm OK, or Help."
        const val ASK_AGAIN = "I noticed another possible fall. Are you OK? Tap I'm OK, or Help."
        /**
         * Two detections whose impacts are this close are one impact seen twice. The same 1.5 s
         * both detectors already treat as the impact itself (FallDetector's settle time,
         * RecoveringFallDetector.SETTLE_MS); anything further apart is a separate detection.
         */
        const val SAME_IMPACT_MS = 1_500L
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
