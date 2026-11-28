package care.ira.watch.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/** The face never says "All well" while the watch cannot reach the server. */
class FaceTextTest {
    @Test fun allWellOnlyWhenConnectedAndNothingElseIsHappening() {
        assertEquals(FaceText.ALL_WELL, FaceText.main(null, null, null, null, null, online = true))
        assertEquals(FaceText.NO_CONNECTION, FaceText.main(null, null, null, null, null, online = false))
        assertNotEquals(FaceText.ALL_WELL, FaceText.main(null, null, null, null, null, online = false))
    }

    @Test fun helpAndQuestionsStillOutrankTheConnectionLine() {
        assertEquals("Did you fall? Are you OK?\nHelp in 7 s", FaceText.main(7, null, null, null, null, online = false))
        assertEquals("Sending for help…", FaceText.main(null, "Sending for help…", null, null, null, online = false))
        assertEquals("Ada, are you OK?", FaceText.main(null, null, "Ada, are you OK?", null, null, online = false))
    }

    @Test fun noConnectionOutranksTheThankYouAndTheVisit() {
        assertEquals(FaceText.NO_CONNECTION, FaceText.main(null, null, null, "I'm OK", null, online = false))
        assertEquals(FaceText.NO_CONNECTION, FaceText.main(null, null, null, null, "Uday is here.", online = false))
        assertEquals("Thank you.", FaceText.main(null, null, null, "I'm OK", "Uday is here.", online = true))
        assertEquals("Uday is here.", FaceText.main(null, null, null, null, "Uday is here.", online = true))
    }
}
