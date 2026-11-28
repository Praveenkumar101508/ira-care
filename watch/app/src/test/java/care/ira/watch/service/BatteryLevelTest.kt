package care.ira.watch.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BatteryLevelTest {
    @Test fun theSystemBroadcastIsUsedWhenThePropertyIsUnsupported() {
        assertEquals(23, BatteryLevel.percent(Int.MIN_VALUE, 23, 100))
        assertEquals(50, BatteryLevel.percent(0, 128, 256))
    }

    @Test fun thePropertyIsTheFallbackWithoutABroadcast() {
        assertEquals(64, BatteryLevel.percent(64, null, null))
        assertEquals(64, BatteryLevel.percent(64, -1, -1))
    }

    @Test fun neitherGivesAValueSoTheBatteryIsUnknown() {
        assertNull(BatteryLevel.percent(Int.MIN_VALUE, -1, -1))
        assertNull(BatteryLevel.percent(null, null, null))
        assertNull(BatteryLevel.percent(0, 5, 0))
    }
}
