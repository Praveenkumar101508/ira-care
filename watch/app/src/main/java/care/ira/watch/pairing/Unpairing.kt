package care.ira.watch.pairing

import care.ira.watch.api.ApiOutcome
import care.ira.watch.outbox.SosState

/**
 * Unpairing this watch from the wrist (remediation C-21). It stops IRA watching over the
 * person, so it is never a single tap:
 *
 *  1. [check]: refused outright while anything urgent is unfinished: a Help request or a fall
 *     not yet sent, a fall countdown running, or a Help request still open on the server.
 *     Unpairing then would silence the very thing the person is waiting on.
 *  2. [warning]: the person is told plainly what unpairing does, and confirms.
 *  3. [Unpairer.unpair]: the server ends the token and tells the care circle FIRST; only when
 *     it has answered is the pairing cleared here. With no connection nothing changes, so a
 *     live token is never left behind on a watch that believes it is unpaired.
 *
 * Pure Kotlin: tested without a watch.
 */
object Unpairing {
    sealed class Check {
        object Allowed : Check()
        data class Refused(val reason: String) : Check()
    }

    fun check(urgentPending: Int, sos: SosState, fallCountdown: Int?): Check = when {
        fallCountdown != null -> Check.Refused("A fall check is running. Answer it first.")
        urgentPending > 0 -> Check.Refused(
            if (urgentPending == 1) "One urgent message is still being sent. Unpair once it has gone."
            else "$urgentPending urgent messages are still being sent. Unpair once they have gone.")
        sos !is SosState.Idle && sos !is SosState.Closed ->
            Check.Refused("Your Help request is still open. Unpair once it is closed.")
        else -> Check.Allowed
    }

    /** What the person reads before confirming. */
    fun warning(unsentReadings: Int): String = buildString {
        append("Unpair this watch? IRA will stop watching over you, and your family and carers will be told.")
        if (unsentReadings > 0) append(" Readings not yet sent stay on this watch and go if it is paired to you again.")
    }

    sealed class Result {
        /** The server ended the token and told the circle; the pairing is cleared here. */
        object Unpaired : Result()
        /** Nothing changed: still paired, still watching over the person. */
        data class NotNow(val reason: String) : Result()
    }
}

/** Runs step 3: the server first, then the watch. `clear` clears the pairing and stops monitoring. */
class Unpairer(private val callServer: () -> ApiOutcome<*>, private val clear: () -> Unit) {
    fun unpair(): Unpairing.Result = when (val out = callServer()) {
        is ApiOutcome.Ok -> { clear(); Unpairing.Result.Unpaired }
        // The server no longer knows this token (revoked by a carer, or an earlier unpair that
        // answered after the watch gave up): it is already dead, so clearing here is right.
        is ApiOutcome.Unauthorised -> { clear(); Unpairing.Result.Unpaired }
        is ApiOutcome.Offline -> Unpairing.Result.NotNow("No connection. The watch stays paired; try again when it is online.")
        is ApiOutcome.Failed -> Unpairing.Result.NotNow("The server could not unpair the watch (${out.status}). It stays paired.")
    }
}
