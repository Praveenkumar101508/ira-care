package care.ira.watch.pairing

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingCodeTest {
    @Test
    fun `a pairing code has three parts and the right prefix`() {
        assertTrue(PairingStore.looksLikePairingCode("cp1.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.ABCDEFGH"))
        assertTrue(PairingStore.looksLikePairingCode("  cp1.scope.ABCDEFGH  "))
        assertFalse(PairingStore.looksLikePairingCode("cd1.scope.secret"))   // a device token, not a code
        assertFalse(PairingStore.looksLikePairingCode("cp1.scope"))
        assertFalse(PairingStore.looksLikePairingCode("cp1..ABCDEFGH"))
        assertFalse(PairingStore.looksLikePairingCode("cp1.scope.AB"))
        assertFalse(PairingStore.looksLikePairingCode(""))
    }
}
