package care.ira.watch.pairing

import care.ira.watch.api.ApiOutcome
import care.ira.watch.outbox.SosState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class UnpairingTest {
    @Test
    fun `unpairing is refused while anything urgent is unfinished`() {
        val refused = listOf(
            Unpairing.check(urgentPending = 1, sos = SosState.Idle, fallCountdown = null),
            Unpairing.check(urgentPending = 3, sos = SosState.Idle, fallCountdown = null),
            Unpairing.check(urgentPending = 0, sos = SosState.Idle, fallCountdown = 20),
            Unpairing.check(urgentPending = 0, sos = SosState.Sending, fallCountdown = null),
            Unpairing.check(urgentPending = 0, sos = SosState.WaitingForConnection, fallCountdown = null),
            Unpairing.check(urgentPending = 0, sos = SosState.Received, fallCountdown = null),
            Unpairing.check(urgentPending = 0, sos = SosState.OnTheWay("Sam"), fallCountdown = null),
            Unpairing.check(urgentPending = 0, sos = SosState.Arrived("Sam"), fallCountdown = null),
        )
        refused.forEach { assertTrue(it.toString(), it is Unpairing.Check.Refused) }
        assertTrue((refused[0] as Unpairing.Check.Refused).reason.contains("One urgent message"))
        assertTrue((refused[1] as Unpairing.Check.Refused).reason.contains("3 urgent messages"))
        assertEquals(Unpairing.Check.Allowed, Unpairing.check(0, SosState.Idle, null))
        assertEquals(Unpairing.Check.Allowed, Unpairing.check(0, SosState.Closed, null))
    }

    @Test
    fun `the warning says monitoring stops and the circle is told`() {
        val w = Unpairing.warning(unsentReadings = 0)
        assertTrue(w.contains("stop watching over you") && w.contains("family and carers will be told"))
        assertTrue(Unpairing.warning(unsentReadings = 12).contains("Readings not yet sent stay on this watch"))
    }

    @Test
    fun `the pairing is cleared only after the server has ended the token`() {
        val order = mutableListOf<String>()
        val ok = Unpairer({ order += "server"; ApiOutcome.Ok(Unit) }) { order += "clear" }.unpair()
        assertEquals(Unpairing.Result.Unpaired, ok)
        assertEquals(listOf("server", "clear"), order)

        // Already revoked on the server: the token is dead, so clearing here is right.
        var cleared = 0
        assertEquals(Unpairing.Result.Unpaired, Unpairer({ ApiOutcome.Unauthorised }) { cleared++ }.unpair())
        assertEquals(1, cleared)
    }

    @Test
    fun `with no connection or a server error nothing changes on the watch`() {
        var cleared = 0
        val offline = Unpairer({ ApiOutcome.Offline("no network") }) { cleared++ }.unpair()
        val failed = Unpairer({ ApiOutcome.Failed(500, "") }) { cleared++ }.unpair()
        assertEquals(0, cleared)
        assertTrue(offline is Unpairing.Result.NotNow && (offline as Unpairing.Result.NotNow).reason.contains("stays paired"))
        assertTrue(failed is Unpairing.Result.NotNow && (failed as Unpairing.Result.NotNow).reason.contains("stays paired"))
    }
}
