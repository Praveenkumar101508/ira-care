package care.ira.watch.sensors

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import android.os.CancellationSignal
import androidx.core.content.ContextCompat
import care.ira.watch.api.LocationFix
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * One location fix, asked for ONLY when an SOS or a suspected fall starts.
 * Nothing here runs continuously and nothing is stored on the watch: the fix
 * travels with the help request and lives with the alert on the server.
 * `null` when the permission is missing, no provider answers in time, or the
 * watch has no position: the server then says "location unavailable" rather
 * than guessing.
 */
interface LocationSource {
    suspend fun currentFix(timeoutMs: Long = 15_000): LocationFix?
}

class AndroidLocationSource(private val context: Context) : LocationSource {
    private val executor = Executors.newSingleThreadExecutor()

    override suspend fun currentFix(timeoutMs: Long): LocationFix? {
        val fine = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (!fine && !coarse) return null
        val manager = context.getSystemService(LocationManager::class.java) ?: return null
        val provider = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER, LocationManager.PASSIVE_PROVIDER)
            .firstOrNull { runCatching { manager.isProviderEnabled(it) }.getOrDefault(false) } ?: return null
        return withTimeoutOrNull(timeoutMs) {
            try {
                suspendCancellableCoroutine { cont ->
                    val signal = CancellationSignal()
                    cont.invokeOnCancellation { signal.cancel() }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                        manager.getCurrentLocation(provider, signal, executor) { loc -> cont.resume(loc?.let(::fix)) }
                    } else {
                        @Suppress("DEPRECATION")
                        cont.resume(manager.getLastKnownLocation(provider)?.let(::fix))
                    }
                }
            } catch (e: SecurityException) {
                null
            }
        } ?: runCatching { manager.getLastKnownLocation(provider)?.let(::fix) }.getOrNull()
    }

    private fun fix(loc: android.location.Location): LocationFix = LocationFix(
        latitude = loc.latitude, longitude = loc.longitude,
        accuracyM = if (loc.hasAccuracy()) loc.accuracy.toDouble() else null,
        recordedAt = DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(Instant.ofEpochMilli(loc.time).atOffset(ZoneOffset.UTC)),
    )
}
