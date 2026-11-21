package care.ira.watch.sensors

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow

/** Raw accelerometer samples in g, for the fall detector. Tests feed synthetic ones. */
interface AccelerometerSource {
    fun samples(): Flow<AccelSample>
}

/**
 * The watch's accelerometer at about 50 Hz (SENSOR_DELAY_GAME). Only the fall
 * detector reads it, and nothing from it is stored or sent: the server learns
 * a peak value and a still duration when a fall is suspected, never the signal.
 */
class AndroidAccelerometerSource(private val context: Context) : AccelerometerSource {
    override fun samples(): Flow<AccelSample> = callbackFlow {
        val manager = context.getSystemService(SensorManager::class.java)
        val sensor = manager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        if (manager == null || sensor == null) { close(); return@callbackFlow }
        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                val g = SensorManager.GRAVITY_EARTH.toDouble()
                trySend(AccelSample(System.currentTimeMillis(), event.values[0] / g, event.values[1] / g, event.values[2] / g))
            }
            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        manager.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_GAME)
        awaitClose { manager.unregisterListener(listener) }
    }
}
