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
        val provider = providerOrder(Build.VERSION.SDK_INT)
            .firstOrNull { runCatching { manager.isProviderEnabled(it) }.getOrDefault(false) } ?: return null
        return withTimeoutOrNull(timeoutMs) {
            try {
                suspendCancellableCoroutine { cont ->
                    val signal = CancellationSignal()
                    cont.invokeOnCancellation { signal.cancel() }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                        // High accuracy: the fused provider then uses GPS, not only the network's rough
                        // position (CI run 46150286394: a BALANCED request, and no position at all).
                        val request = android.location.LocationRequest.Builder(0L)
                            .setQuality(android.location.LocationRequest.QUALITY_HIGH_ACCURACY)
                            .setDurationMillis(timeoutMs.coerceAtLeast(1L)).build()
                        manager.getCurrentLocation(provider, request, signal, executor) { loc -> cont.resume(loc?.let(::fix)) }
                    } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
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

/** The location providers asked, best first. The fused provider (Android 12, API 31) is the one most
 *  Wear OS watches without their own GPS answer through; it was never asked before (CI run 46146704537:
 *  the Help alert had no position). Then GPS, the network and the passive provider. */
internal fun providerOrder(sdk: Int): List<String> = buildList {
    if (sdk >= 31) add("fused")   // LocationManager.FUSED_PROVIDER
    add(LocationManager.GPS_PROVIDER); add(LocationManager.NETWORK_PROVIDER); add(LocationManager.PASSIVE_PROVIDER)
}
