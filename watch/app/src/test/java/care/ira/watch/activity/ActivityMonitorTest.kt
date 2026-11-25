package care.ira.watch.activity

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import kotlin.math.PI
import kotlin.math.sin

/** The monitor on the watch's real sample rates: accelerometer about 50 Hz in g, gyroscope 20 Hz. */
class ActivityMonitorTest {
    private val model = ActivityModel.parse(File("src/main/assets/${ActivityModel.ASSET}").readText())
    private val g = 9.80665

    /** A synthetic walk (the fixture's shape): 1.8 Hz steps, 0.9 Hz arm swing. */
    private fun walk(monitor: ActivityMonitor, fromMs: Long, toMs: Long, gyroGapAt: LongRange? = null) {
        var tA = fromMs; var tG = fromMs
        while (tA < toMs || tG < toMs) {
            if (tA <= tG) {
                val s = tA / 1000.0
                monitor.feedAccelG(tA, (0.3 + 2.5 * sin(2 * PI * 1.8 * s)) / g, (-2.0 + 1.5 * sin(2 * PI * 0.9 * s + 0.4)) / g,
                    (9.5 + 3.0 * sin(2 * PI * 1.8 * s + 1.1)) / g)
                tA += 20
            } else {
                val s = tG / 1000.0
                if (gyroGapAt == null || tG !in gyroGapAt)
                    monitor.feedGyro(tG, 1.2 * sin(2 * PI * 0.9 * s) + 0.01 * sin(7.0 * s), 0.6 * sin(2 * PI * 1.8 * s), 0.4 * kotlin.math.cos(2 * PI * 0.9 * s))
                tG += 50
            }
        }
    }

    @Test fun theReferenceWalkFedAtRealRatesGivesTheReferenceAnswerAfterTenSeconds() {
        // The Python reference's 12 s walk at 50 Hz (both sensors), fed with real timestamps,
        // the accelerometer in g as the watch's source gives it. The monitor's own 20 Hz grid is
        // offset from the reference's by a few milliseconds, so the class is compared, not the bits.
        val fixture = kotlinx.serialization.json.Json.parseToJsonElement(File("src/test/resources/activity_parity.json").readText())
        val case = fixture.jsonObject.getValue("cases").jsonArray.map { it.jsonObject }
            .first { it.getValue("name").jsonPrimitive.content == "walk_50hz_12s" }
        fun rows(key: String) = case.getValue(key).jsonArray.map { r -> r.jsonArray.map { it.jsonPrimitive.double } }
        val acc = rows("acc"); val gyro = rows("gyro")
        val m = ActivityMonitor(model)
        m.onWrist = true
        for (i in acc.indices) {
            val t = i * 20L
            if (t == 9_000L) assertNull("nothing before a full window", m.latest)
            m.feedAccelG(t, acc[i][0] / g, acc[i][1] / g, acc[i][2] / g)
            m.feedGyro(t, gyro[i][0], gyro[i][1], gyro[i][2])
        }
        assertNotNull(m.latest)
        assertEquals(case.getValue("activity").jsonPrimitive.content, m.latest!!.activity)
        assertTrue(m.latest!!.reliable)
    }

    @Test fun offTheWristThereIsNoEstimateAndTheLastOneIsCleared() {
        val m = ActivityMonitor(model)
        m.onWrist = true
        walk(m, 0, 12_000)
        assertNotNull(m.latest)
        m.onWrist = false
        walk(m, 12_000, 24_000)
        assertNull(m.latest)
    }

    @Test fun aGapInEitherSensorGivesNoEstimateRatherThanAGuess() {
        val m = ActivityMonitor(model)
        m.onWrist = true
        walk(m, 0, 12_000, gyroGapAt = 3_000L..5_000L)
        assertNull(m.latest)
    }

    @Test fun theMonitorItselfSendsNothing() {
        // The monitor only hands windows to its callback; what may leave the watch, and when, is
        // decided in one place (ActivitySharing, under consent; see ActivitySharingTest).
        val src = File("src/main/java/care/ira/watch/activity/ActivityMonitor.kt").readText()
        for (word in listOf("CareApi", "Outbox", "OfflineQueue", "Reading(")) assertFalse(word, src.contains(word))
        val readers = File("src/main/java").walkTopDown().filter { it.isFile && it.extension == "kt" }
            .filter { "ActivityMonitor" in it.readText() || ".activity?.latest" in it.readText() }
            .map { it.path.substringAfter("src/main/java/") }.toSortedSet()
        assertEquals(sortedSetOf(
            "care/ira/watch/activity/ActivityMonitor.kt",
            "care/ira/watch/service/CareDependencies.kt",
            "care/ira/watch/service/CareForegroundService.kt",
        ), readers)
    }
}
