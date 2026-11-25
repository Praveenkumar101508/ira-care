package care.ira.watch.sensors

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow

/** One gyroscope sample, rad/s. */
data class GyroSample(val atMillis: Long, val x: Double, val y: Double, val z: Double)

/** Raw gyroscope samples, for IRA Care Activity on the watch. Tests feed synthetic ones. */
interface GyroscopeSource {
    fun samples(): Flow<GyroSample>
}

/**
 * The watch's gyroscope at about 20 Hz, the rate IRA Care Activity was trained at. Only the
 * activity estimate reads it, and nothing from it is stored or sent. A watch without a
 * gyroscope gives an empty flow, and then there is simply no activity estimate.
 */
class AndroidGyroscopeSource(private val context: Context) : GyroscopeSource {
    override fun samples(): Flow<GyroSample> = callbackFlow {
        val manager = context.getSystemService(SensorManager::class.java)
        val sensor = manager?.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
        if (manager == null || sensor == null) { close(); return@callbackFlow }
        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                trySend(GyroSample(System.currentTimeMillis(), event.values[0].toDouble(), event.values[1].toDouble(), event.values[2].toDouble()))
            }
            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        manager.registerListener(listener, sensor, SAMPLING_PERIOD_US)
        awaitClose { manager.unregisterListener(listener) }
    }

    companion object {
        /** 20 Hz. */
        const val SAMPLING_PERIOD_US = 50_000
    }
}
