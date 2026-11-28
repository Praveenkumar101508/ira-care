package care.ira.watch.testserver

import android.content.ContextWrapper
import care.ira.watch.BuildConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Release builds (run by testWearosReleaseUnitTest and testAospReleaseUnitTest): the care server
 * is the address configured at build time, https, and nothing typed on the watch can change it.
 */
class ReleaseServerAddressTest {
    @Test
    fun `release uses its configured address`() {
        val configured = System.getenv("IRA_CARE_API_BASE_URL") ?: "https://care.example.invalid"
        assertEquals(configured, BuildConfig.CARE_API_BASE_URL)
        // A context whose every call returns nothing: release must not ask it for a stored address.
        assertEquals(BuildConfig.CARE_API_BASE_URL, TestServer.baseUrl(ContextWrapper(null)))
    }

    @Test
    fun `release's address is https`() {
        assertTrue(BuildConfig.CARE_API_BASE_URL, BuildConfig.CARE_API_BASE_URL.startsWith("https://"))
    }

    @Test
    fun `release has no test server screen`() {
        assertFalse(TestServer.EDITABLE)
        assertNull(TestServer.addressScreen)
    }
}
