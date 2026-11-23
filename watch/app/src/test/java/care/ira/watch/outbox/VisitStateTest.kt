package care.ira.watch.outbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VisitStateTest {
    @Test
    fun `the wrist shows who is coming and who is here, in plain words`() {
        assertEquals("Sravani is here.", VisitState.fromServer("here", "Sravani", null).text())
        val expected = VisitState.fromServer("expected", "Sravani", "2026-11-23T10:00:00+00:00")
        assertEquals(true, expected.text()!!.startsWith("Sravani is coming at "))
    }

    @Test
    fun `a late visit reads like an expected one to the person`() {
        assertEquals(
            VisitState.fromServer("expected", "Sravani", null).text(),
            VisitState.fromServer("late", "Sravani", null).text(),
        )
    }

    @Test
    fun `no visit, an unknown state or a missing name never invents anything`() {
        assertNull(VisitState.fromServer("none", null, null).text())
        assertNull(VisitState.fromServer(null, "Sravani", null).text())
        assertNull(VisitState.fromServer("something_new", "Sravani", null).text())
        assertEquals("Your carer is here.", VisitState.fromServer("here", null, null).text())
        assertNull(VisitState.localTime("not a time"))
    }
}
