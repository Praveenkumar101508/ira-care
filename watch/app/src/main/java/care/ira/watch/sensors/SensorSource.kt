package care.ira.watch.sensors

import care.ira.watch.api.Reading
import kotlinx.coroutines.flow.Flow

/**
 * Where readings come from. The app talks to this interface only, so the
 * Health Services implementation (every Wear OS 3 watch) can be joined later
 * by a vendor sensor SDK once partner approval exists, and tests can feed
 * synthetic readings.
 */
interface SensorSource {
    /** Heart rate, steps, movement and wrist-on readings as they arrive. */
    fun readings(): Flow<Reading>

    /** Whether background sensor access is currently granted. */
    fun backgroundPermissionGranted(): Boolean

    /** A one-off reading of the current state (used after a missed check-in). */
    suspend fun snapshot(): List<Reading>
}

/** The kinds of reading the backend understands. */
object ReadingKinds {
    const val HEART_RATE = "heart_rate"
    const val STEPS = "steps"
    const val MOVEMENT = "movement"
    const val ON_WRIST = "on_wrist"
    const val BATTERY = "battery"
}
