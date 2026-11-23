package care.ira.watch.platform

import android.content.Context
import care.ira.watch.core.Clock
import care.ira.watch.sensors.HealthServicesSensorSource
import care.ira.watch.sensors.SensorSource

/** The Wear OS flavour: readings through Health Services (Google's sensor service on every Wear OS 3 watch). */
object PlatformDrivers {
    const val NAME = "wearos"
    fun sensors(context: Context, clock: Clock): SensorSource = HealthServicesSensorSource(context, clock)
}
