package care.ira.watch.outbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SosStateTest {
    @Test
    fun `the wrist shows each state in plain words with the responders name`() {
        assertEquals("Sending…", SosState.Sending.text())
        assertEquals("Waiting for connection. Help request saved.", SosState.WaitingForConnection.text())
        assertEquals("Help request received.", SosState.Received.text())
        assertEquals("Sravani is on the way.", SosState.OnTheWay("Sravani").text())
        assertEquals("Sravani has arrived.", SosState.Arrived("Sravani").text())
        assertNull(SosState.Idle.text())
    }

    @Test
    fun `only the server can say help is coming and an unknown word never does`() {
        assertEquals(SosState.Received, SosState.fromServer("received", null))
        assertEquals(SosState.OnTheWay("Praveen"), SosState.fromServer("on_the_way", "Praveen"))
        assertEquals(SosState.Arrived("Someone"), SosState.fromServer("arrived", null))
        assertEquals(SosState.Closed, SosState.fromServer("closed", null))
        assertNull(SosState.fromServer("idle", null)); assertNull(SosState.fromServer(null, null))
        val unknown = SosState.fromServer("something_new", "X")
        assertTrue(unknown is SosState.Received)
        for (s in listOf(SosState.Sending, SosState.WaitingForConnection, SosState.Received)) {
            assertTrue(s.text()!!.lowercase().let { !it.contains("on the way") && !it.contains("on its way") })
        }
    }
}
