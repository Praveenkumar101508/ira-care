package care.ira.watch.testserver

import care.ira.watch.BuildConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Debug builds: the typed test server address is used only when it passes ServerAddress.check. */
class TestServerDebugTest {
    @Test
    fun `the debug build can be given a test server and shows the screen`() {
        assertTrue(TestServer.EDITABLE)
        assertNotNull(TestServer.addressScreen)
    }

    @Test
    fun `a stored https address is used`() {
        assertEquals("https://calm-river-1234.trycloudflare.com",
            TestServer.resolve("https://calm-river-1234.trycloudflare.com", BuildConfig.CARE_API_BASE_URL))
    }

    @Test
    fun `nothing stored means the build's own address (the emulator's host)`() {
        assertEquals(BuildConfig.CARE_API_BASE_URL, TestServer.resolve(null, BuildConfig.CARE_API_BASE_URL))
        assertEquals("http://10.0.2.2:8000", BuildConfig.CARE_API_BASE_URL)
    }

    @Test
    fun `a stored cleartext address to any other host is ignored`() {
        for (stored in listOf("http://192.168.1.20:8000", "http://calm-river-1234.trycloudflare.com", "http://localhost:8000", "junk"))
            assertEquals(stored, BuildConfig.CARE_API_BASE_URL, TestServer.resolve(stored, BuildConfig.CARE_API_BASE_URL))
    }
}
