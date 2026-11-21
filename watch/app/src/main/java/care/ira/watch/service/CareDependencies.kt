package care.ira.watch.service

import android.content.Context
import android.os.BatteryManager
import care.ira.watch.BuildConfig
import care.ira.watch.api.CareApi
import care.ira.watch.core.Clock
import care.ira.watch.pairing.PairingStore
import care.ira.watch.queue.OfflineQueue
import care.ira.watch.sensors.AndroidLocationSource
import care.ira.watch.sensors.HealthServicesSensorSource
import care.ira.watch.sensors.LocationSource
import care.ira.watch.sensors.SensorSource
import care.ira.watch.voice.AndroidListener
import care.ira.watch.voice.AndroidSpeaker
import care.ira.watch.voice.Listener
import care.ira.watch.voice.Speaker
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class DeviceState(val battery: Int?, val onWrist: Boolean?, val permission: Boolean?, val appVersion: String)

/** What the watch is showing right now, for the screen. */
data class WatchStatus(
    val paired: Boolean,
    val permissionGranted: Boolean,
    val question: String? = null,
    val listening: Boolean = false,
    val lastAnswer: String? = null,
    val helpSent: Boolean = false,
    val queued: Int = 0,
    val online: Boolean = true,
)

/**
 * The app's wiring, built once. Real Android implementations here; tests
 * build the pieces they need directly with fakes.
 */
class CareDependencies private constructor(context: Context) {
    val clock: Clock = Clock.SYSTEM
    val pairing = PairingStore(context)
    val queue = OfflineQueue(File(context.filesDir, "readings.jsonl"))
    val api = CareApi(BuildConfig.CARE_API_BASE_URL, { pairing.token })
    val sensors: SensorSource = HealthServicesSensorSource(context, clock)
    /** Asked once per SOS; never polled. */
    val location: LocationSource = AndroidLocationSource(context)
    val speaker: Speaker = AndroidSpeaker(context)
    val listener: Listener = AndroidListener(context)
    val sync = Syncer(this)
    val status = MutableStateFlow(WatchStatus(pairing.isPaired, sensors.backgroundPermissionGranted()))
    private val appContext = context.applicationContext

    @Volatile var lastOnWrist: Boolean? = null

    fun deviceState(): DeviceState {
        val bm = appContext.getSystemService(BatteryManager::class.java)
        val battery = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)?.takeIf { it in 0..100 }
        return DeviceState(battery, lastOnWrist, sensors.backgroundPermissionGranted(), BuildConfig.VERSION_NAME)
    }

    fun onUnauthorised() {
        pairing.clear()
        status.value = status.value.copy(paired = false)
    }

    fun statusFlow(): StateFlow<WatchStatus> = status

    companion object {
        @Volatile private var instance: CareDependencies? = null

        fun get(context: Context): CareDependencies =
            instance ?: synchronized(this) { instance ?: CareDependencies(context.applicationContext).also { instance = it } }
    }
}
