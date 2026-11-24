package care.ira.watch.platform

import android.content.Context
import care.ira.watch.core.Clock
import care.ira.watch.sensors.SensorManagerSensorSource
import care.ira.watch.sensors.SensorSource

/**
 * The AOSP flavour: our own watch, plain Android, no Google service on the device.
 * Readings come straight from the Android sensor framework; the hardware's sensor
 * HAL must meet docs/hardware/SENSOR_HAL.md.
 */
object PlatformDrivers {
    const val NAME = "aosp"
    fun sensors(context: Context, clock: Clock): SensorSource = SensorManagerSensorSource(context, clock)
}
