package care.ira.watch.sensors

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import androidx.core.content.ContextCompat
import androidx.health.services.client.HealthServices
import androidx.health.services.client.PassiveListenerCallback
import androidx.health.services.client.data.DataPointContainer
import androidx.health.services.client.data.DataType
import androidx.health.services.client.data.PassiveListenerConfig
import androidx.health.services.client.data.UserActivityInfo
import androidx.health.services.client.data.UserActivityState
import care.ira.watch.api.Reading
import care.ira.watch.core.Clock
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.guava.await

/**
 * Readings from Wear OS Health Services, the sensor service every Wear OS 3+
 * watch has. Passive monitoring: the watch batches heart rate and steps and
 * delivers them without keeping the app awake. Wrist state comes from the
 * user-activity events. No vendor SDK: that is behind this interface for later.
 */
class HealthServicesSensorSource(
    private val context: Context,
    private val clock: Clock = Clock.SYSTEM,
    private val batcher: ReadingBatcher = ReadingBatcher(),
) : SensorSource {
    private val client = HealthServices.getClient(context).passiveMonitoringClient

    override fun backgroundPermissionGranted(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.BODY_SENSORS_BACKGROUND) ==
            PackageManager.PERMISSION_GRANTED

    override fun readings(): Flow<Reading> = callbackFlow {
        val callback = object : PassiveListenerCallback {
            override fun onNewDataPointsReceived(dataPoints: DataPointContainer) {
                val now = clock.nowMillis()
                dataPoints.getData(DataType.HEART_RATE_BPM).lastOrNull()?.let { point ->
                    batcher.heartRate(point.value, now)?.let { trySend(it) }
                }
                dataPoints.getData(DataType.STEPS_DAILY).lastOrNull()?.let { point ->
                    stepsDaily(point.value, now)?.let { trySend(it) }
                }
            }

            override fun onUserActivityInfoReceived(info: UserActivityInfo) {
                // Any activity state (awake, passive, asleep, exercising) means the
                // watch is being worn. Wrist-off itself comes from the off-body
                // sensor below, which fires the moment the watch is taken off.
                if (info.userActivityState != UserActivityState.USER_ACTIVITY_UNKNOWN) {
                    batcher.onWrist(true, clock.nowMillis())?.let { trySend(it) }
                }
            }
        }
        val sensorManager = context.getSystemService(SensorManager::class.java)
        val offBody = sensorManager.getDefaultSensor(Sensor.TYPE_LOW_LATENCY_OFFBODY_DETECT)
        val offBodyListener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                // 1.0 = on body, 0.0 = off body.
                batcher.onWrist(event.values.firstOrNull() == 1.0f, clock.nowMillis())?.let { trySend(it) }
            }
            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        if (offBody != null) sensorManager.registerListener(offBodyListener, offBody, SensorManager.SENSOR_DELAY_NORMAL)
        val config = PassiveListenerConfig.builder()
            .setDataTypes(setOf(DataType.HEART_RATE_BPM, DataType.STEPS_DAILY))
            .setShouldUserActivityInfoBeRequested(true)
            .build()
        client.setPassiveListenerCallback(config, callback)
        awaitClose {
            client.clearPassiveListenerCallbackAsync()
            sensorManager.unregisterListener(offBodyListener)
        }
    }

    private var lastDailySteps: Long = -1

    private fun stepsDaily(total: Long, now: Long): Reading? {
        val delta = if (lastDailySteps < 0 || total < lastDailySteps) 0.0 else (total - lastDailySteps).toDouble()
        lastDailySteps = total
        return batcher.steps(delta, now)
    }

    override suspend fun snapshot(): List<Reading> {
        val caps = runCatching { client.getCapabilitiesAsync().await() }.getOrNull()
        val now = clock.nowMillis()
        val out = mutableListOf<Reading>()
        batcher.flushSteps(now)?.let { out.add(it) }
        if (caps != null) out.add(Reading(ReadingKinds.BATTERY, batteryPercent().toDouble(), ReadingBatcher.iso(now), "%"))
        return out
    }

    fun batteryPercent(): Int {
        val bm = context.getSystemService(android.os.BatteryManager::class.java)
        return bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
    }
}
