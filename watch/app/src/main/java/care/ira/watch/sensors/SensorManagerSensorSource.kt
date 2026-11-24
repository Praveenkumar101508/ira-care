package care.ira.watch.sensors

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.BatteryManager
import androidx.core.content.ContextCompat
import care.ira.watch.api.Reading
import care.ira.watch.core.Clock
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow

/**
 * Readings straight from the Android sensor framework: the driver for our own
 * watch on plain AOSP, where there is no Health Services. It needs exactly the
 * sensors docs/hardware/SENSOR_HAL.md asks the hardware for:
 *
 *   TYPE_HEART_RATE                 bpm with the accuracy flag (unreliable samples are dropped)
 *   TYPE_STEP_COUNTER               steps since boot (deltas are kept)
 *   TYPE_LOW_LATENCY_OFFBODY_DETECT 1.0 on the wrist, 0.0 off it
 *
 * The same source works on a Wear OS watch too (the framework sensors are
 * there), which is how a development board without Health Services is tested.
 * [SensorEventMapper] holds the arithmetic so it is tested on the JVM.
 */
class SensorManagerSensorSource(
    private val context: Context,
    private val clock: Clock = Clock.SYSTEM,
    private val mapper: SensorEventMapper = SensorEventMapper(),
) : SensorSource {

    override fun backgroundPermissionGranted(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.BODY_SENSORS_BACKGROUND) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.BODY_SENSORS) == PackageManager.PERMISSION_GRANTED

    override fun readings(): Flow<Reading> = callbackFlow {
        val manager = context.getSystemService(SensorManager::class.java)
        if (manager == null) { close(); return@callbackFlow }
        val listener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                val now = clock.nowMillis()
                val reading = when (event.sensor.type) {
                    Sensor.TYPE_HEART_RATE -> mapper.heartRate(event.values.firstOrNull() ?: 0f, event.accuracy, now)
                    Sensor.TYPE_STEP_COUNTER -> mapper.stepCounter(event.values.firstOrNull() ?: 0f, now)
                    Sensor.TYPE_LOW_LATENCY_OFFBODY_DETECT -> mapper.offBody(event.values.firstOrNull() ?: 1f, now)
                    else -> null
                }
                reading?.let { trySend(it) }
            }
            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        val wanted = listOf(
            Sensor.TYPE_HEART_RATE to SensorManager.SENSOR_DELAY_NORMAL,
            Sensor.TYPE_STEP_COUNTER to SensorManager.SENSOR_DELAY_NORMAL,
            Sensor.TYPE_LOW_LATENCY_OFFBODY_DETECT to SensorManager.SENSOR_DELAY_NORMAL,
        )
        var registered = 0
        for ((type, delay) in wanted) {
            val sensor = manager.getDefaultSensor(type) ?: continue
            // Batching: the HAL may hold samples for up to a minute and deliver them together (SENSOR_HAL.md).
            if (manager.registerListener(listener, sensor, delay, 60_000_000)) registered++
        }
        if (registered == 0) { close(); return@callbackFlow }
        awaitClose { manager.unregisterListener(listener) }
    }

    override suspend fun snapshot(): List<Reading> {
        val now = clock.nowMillis()
        val out = mutableListOf<Reading>()
        mapper.flushSteps(now)?.let { out.add(it) }
        val bm = context.getSystemService(BatteryManager::class.java)
        val battery = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        if (battery != null && battery in 0..100) out.add(Reading(ReadingKinds.BATTERY, battery.toDouble(), ReadingBatcher.iso(now), "%"))
        return out
    }
}

/**
 * The arithmetic of the framework sensors, with no Android in it, so a JVM test
 * can prove it: unreliable heart-rate samples are dropped, step-counter totals
 * become deltas (a reboot resets the counter, which counts as zero new steps),
 * and the off-body value becomes a wrist reading. Every sample keeps its own
 * time through [ReadingBatcher].
 */
class SensorEventMapper(private val batcher: ReadingBatcher = ReadingBatcher()) {
    private var lastTotal: Float = -1f

    /** Android's SENSOR_STATUS_UNRELIABLE is 0 and NO_CONTACT is -1: neither is a heart rate. */
    fun heartRate(bpm: Float, accuracy: Int, atMillis: Long): Reading? {
        if (accuracy <= 0 || bpm < 25f || bpm > 250f) return null
        return batcher.heartRate(bpm.toDouble(), atMillis)
    }

    fun stepCounter(total: Float, atMillis: Long): Reading? {
        val delta = if (lastTotal < 0f || total < lastTotal) 0.0 else (total - lastTotal).toDouble()
        lastTotal = total
        return batcher.steps(delta, atMillis)
    }

    fun offBody(value: Float, atMillis: Long): Reading? = batcher.onWrist(value >= 0.5f, atMillis)

    fun flushSteps(atMillis: Long): Reading? = batcher.flushSteps(atMillis)
}
