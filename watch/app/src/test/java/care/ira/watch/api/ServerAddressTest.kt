package care.ira.watch.api

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The rule for an address typed on a test build's watch (ServerAddress, used by src/debug TestServer). */
class ServerAddressTest {
    private fun ok(text: String) = (ServerAddress.check(text) as ServerAddress.Check.Ok).url
    private fun refused(text: String) = ServerAddress.check(text) is ServerAddress.Check.Refused

    @Test
    fun `an https address is taken and tidied`() {
        assertEquals("https://calm-river-1234.trycloudflare.com", ok("https://calm-river-1234.trycloudflare.com"))
        assertEquals("https://calm-river-1234.trycloudflare.com", ok("  HTTPS://Calm-River-1234.TryCloudflare.com/  "))
        assertEquals("https://care.example.org:8443", ok("https://care.example.org:8443"))
    }

    @Test
    fun `plain http is taken for the emulator's host address only`() {
        assertEquals("http://10.0.2.2:8000", ok("http://10.0.2.2:8000"))
        for (address in listOf(
            "http://192.168.1.20:8000", "http://care.example.org", "http://calm-river-1234.trycloudflare.com",
            "http://localhost:8000", "http://127.0.0.1:8000", "http://10.0.2.3:8000", "http://10.0.2.2.example.org",
        )) assertTrue(address, refused(address))
    }

    @Test
    fun `anything else is refused`() {
        for (address in listOf(
            "", "   ", "https://", "calm-river-1234.trycloudflare.com", "ftp://care.example.org", "ws://care.example.org",
            "https://user:pass@care.example.org", "https://care.example.org/api", "https://care.example.org/?x=1",
            "https://care.example.org/#top", "https://care example.org", "javascript:alert(1)", "file:///etc/hosts",
        )) assertTrue("'$address'", refused(address))
    }

    @Test
    fun `a refusal says what to do`() {
        val check = ServerAddress.check("http://192.168.1.20:8000") as ServerAddress.Check.Refused
        assertEquals("Use an https:// address.", check.reason)
    }
}
