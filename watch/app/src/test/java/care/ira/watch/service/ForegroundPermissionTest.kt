package care.ira.watch.service

import android.Manifest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ForegroundPermissionTest {
    @Test fun nothingGrantedMeansTheServiceMustNotStart() {
        // The state straight after pairing, before the permission screen: starting then crashed the app.
        assertFalse(ForegroundPermission.allowed { false })
    }

    @Test fun anyOneSensorPermissionIsEnough() {
        for (p in ForegroundPermission.ANY_OF) assertTrue(p, ForegroundPermission.allowed { it == p })
    }

    @Test fun unrelatedPermissionsAreNotEnough() {
        val unrelated = setOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS,
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.BODY_SENSORS_BACKGROUND)
        assertFalse(ForegroundPermission.allowed { it in unrelated })
    }

    @Test fun theListMatchesWhatAndroidAsksForAHealthService() {
        // From the SecurityException: "any of the permissions allOf=false [ACTIVITY_RECOGNITION, BODY_SENSORS, HIGH_SAMPLING_RATE_SENSORS]".
        assertEquals(listOf("android.permission.ACTIVITY_RECOGNITION", "android.permission.BODY_SENSORS",
            "android.permission.HIGH_SAMPLING_RATE_SENSORS"), ForegroundPermission.ANY_OF)
    }
}
