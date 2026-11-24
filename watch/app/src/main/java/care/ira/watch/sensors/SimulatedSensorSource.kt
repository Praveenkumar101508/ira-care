package care.ira.watch.sensors

import care.ira.watch.api.Reading
import care.ira.watch.core.Clock
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlin.math.PI
import kotlin.math.sin

/**
 * A scripted sensor driver with no hardware behind it: the emulator, CI and the
 * presenter's browser watch use it. It is deterministic (the clock decides every
 * value), says so in every reading's unit field, and is never chosen unless the
 * build was made with IRA_WATCH_SIMULATED_SENSORS=1. A resting heart rate that
 * breathes between 62 and 74, a few steps a minute, the watch on the wrist.
 */
class SimulatedSensorSource(
    private val clock: Clock = Clock.SYSTEM,
    private val periodMs: Long = 5_000,
    private val batcher: ReadingBatcher = ReadingBatcher(),
) : SensorSource {
    override fun backgroundPermissionGranted(): Boolean = true

    /** The scripted values at one instant; pure, so a test can check them. */
    fun sample(atMillis: Long): List<Reading> {
        val out = mutableListOf<Reading>()
        val phase = (atMillis % 600_000L) / 600_000.0 * 2 * PI     // a ten-minute breath
        batcher.heartRate(68.0 + 6.0 * sin(phase), atMillis)?.let { out.add(it.copy(unit = "bpm simulated")) }
        val steps = if ((atMillis / periodMs) % 3L == 0L) 4.0 else 0.0
        batcher.steps(steps, atMillis)?.let { out.add(it.copy(unit = "steps simulated")) }
        batcher.onWrist(true, atMillis)?.let { out.add(it.copy(unit = "simulated")) }
        return out
    }

    override fun readings(): Flow<Reading> = flow {
        while (true) {
            sample(clock.nowMillis()).forEach { emit(it) }
            delay(periodMs)
        }
    }

    override suspend fun snapshot(): List<Reading> {
        val now = clock.nowMillis()
        val out = mutableListOf<Reading>()
        batcher.flushSteps(now)?.let { out.add(it.copy(unit = "steps simulated")) }
        out.add(Reading(ReadingKinds.BATTERY, 80.0, ReadingBatcher.iso(now), "% simulated"))
        return out
    }
}
