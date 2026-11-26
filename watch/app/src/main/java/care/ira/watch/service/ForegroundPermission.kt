package care.ira.watch.service

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat

/**
 * When Android lets the care service run in the foreground.
 *
 * The service is declared `foregroundServiceType="health"`. From targetSdk 34, starting a
 * health foreground service throws a SecurityException unless at least one of these runtime
 * permissions is granted, and the app is killed. Pairing finishes before the permission screen
 * is shown, so starting the service straight after pairing crashed the app (the Wear OS
 * emulator journey, run 45760714043). Every start goes through [CareForegroundService.start],
 * which asks this first; the permission screen starts the service once the grant arrives.
 */
object ForegroundPermission {
    /** Any one of these is enough (with FOREGROUND_SERVICE_HEALTH, which the manifest holds). */
    val ANY_OF: List<String> = listOf(
        Manifest.permission.ACTIVITY_RECOGNITION,
        Manifest.permission.BODY_SENSORS,
        Manifest.permission.HIGH_SAMPLING_RATE_SENSORS,
    )

    /** Pure, for tests: whether a health foreground service may start, given which permissions are granted. */
    fun allowed(isGranted: (String) -> Boolean): Boolean = ANY_OF.any(isGranted)

    fun allowed(context: Context): Boolean =
        allowed { ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED }
}
