package care.ira.watch.outbox

/**
 * What the wrist says about a Help request or a fall, and nothing more than
 * the truth:
 *
 *   Sending            the event is in the outbox, an attempt is under way
 *   WaitingForConnection  the server could not be reached; the outbox retries
 *   Received           the server confirmed it has the request ("Help request received")
 *   OnTheWay(name)     a responder said "I'm on my way"
 *   Arrived(name)      that responder has arrived
 *   Closed             the alert was resolved
 *
 * [Received], [OnTheWay], [Arrived] and [Closed] can only come from the server
 * (`/devices/me/sos` or its stream). The watch never says help is coming on
 * its own.
 */
sealed class SosState {
    object Idle : SosState()
    object Sending : SosState()
    object WaitingForConnection : SosState()
    object Received : SosState()
    data class OnTheWay(val name: String) : SosState()
    data class Arrived(val name: String) : SosState()
    object Closed : SosState()

    /** The words on the screen (and spoken). */
    fun text(): String? = when (this) {
        Idle -> null
        Sending -> "Sending…"
        WaitingForConnection -> "Waiting for connection. Help request saved."
        Received -> "Help request received."
        is OnTheWay -> "$name is on the way."
        is Arrived -> "$name has arrived."
        Closed -> "This is now resolved."
    }

    companion object {
        /** From the server's `{state, responder_name}`; unknown states read as Received, never as help coming. */
        fun fromServer(state: String?, responderName: String?): SosState? = when (state) {
            null, "idle" -> null
            "asking" -> null
            "received" -> Received
            "on_the_way" -> OnTheWay(responderName ?: "Someone")
            "arrived" -> Arrived(responderName ?: "Someone")
            "closed" -> Closed
            else -> Received
        }
    }
}


/**
 * What the wrist says about a carer's visit, from the server only (109):
 * "Sravani is coming at 10:00", "Sravani is here". A late visit reads the
 * same as an expected one to the person: the lateness is the coordinator's
 * problem, not theirs to worry about.
 */
sealed class VisitState {
    object None : VisitState()
    data class Expected(val name: String, val time: String?) : VisitState()
    data class Here(val name: String) : VisitState()

    fun text(): String? = when (this) {
        None -> null
        is Expected -> if (time != null) "$name is coming at $time." else "$name is coming today."
        is Here -> "$name is here."
    }

    companion object {
        /** From the server's `{state, carer_name, planned_start}`; unknown states read as nothing. */
        fun fromServer(state: String?, carerName: String?, plannedStart: String?): VisitState = when (state) {
            "expected", "late" -> Expected(carerName ?: "Your carer", localTime(plannedStart))
            "here" -> Here(carerName ?: "Your carer")
            else -> None
        }

        /** "2026-11-23T10:00:00+00:00" -> "10:00" in the watch's own zone; null when unreadable. */
        fun localTime(iso: String?): String? = iso?.let {
            runCatching {
                val t = java.time.OffsetDateTime.parse(it).atZoneSameInstant(java.time.ZoneId.systemDefault())
                "%02d:%02d".format(t.hour, t.minute)
            }.getOrNull()
        }
    }
}
