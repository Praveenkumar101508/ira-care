package care.ira.watch.sensors

import org.junit.Assert.assertEquals
import org.junit.Test

class LocationProviderOrderTest {
    @Test fun theFusedProviderIsAskedFirstFromAndroid12() {
        assertEquals(listOf("fused", "gps", "network", "passive"), providerOrder(31))
        assertEquals(listOf("fused", "gps", "network", "passive"), providerOrder(35))
    }

    @Test fun beforeAndroid12ThereIsNoFusedProvider() {
        assertEquals(listOf("gps", "network", "passive"), providerOrder(30))
    }
}
