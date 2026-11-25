package care.ira.watch.activity

import care.ira.watch.api.ActivityAck
import care.ira.watch.api.ApiOutcome
import care.ira.watch.storage.AesGcmSealer
import care.ira.watch.storage.WearerFiles
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.ZoneOffset
import kotlin.math.PI
import kotlin.math.sin

/**
 * The activity data contract on the watch: windows become hourly summaries, unavailable and
 * unknown are never filled in, and nothing leaves the watch without the person's consent.
 */
class ActivitySharingTest {
    private val modelText = File("src/main/assets/${ActivityModel.ASSET}").readText()
    private val model = ActivityModel.parse(modelText)
    private val id = ActivityModelId.of(modelText)
    private val g = 9.80665
    private val hour = ActivitySummariser.HOUR_MS

    private fun w(startS: Long, label: String, conf: Double? = 0.9, reason: String? = null) =
        ActivityWindow(startS * 1000, startS * 1000 + 10_000, label, conf, reason)

    private fun queue(): ActivityQueue {
        val file = File(Files.createTempDirectory("act").toFile(), "activity.jsonl")
        return ActivityQueue({ file })
    }

    // ── the contract ──────────────────────────────────────────────────────────

    @Test fun theModelIsNamedByItsVersionAndTheHashOfTheExactFile() {
        assertEquals("ira-care-activity-mlp/1", id.version)
        // The same bytes as the backend's copy (care/models/activity_model.json); the server
        // refuses a summary from any other model.
        assertEquals(64, id.sha256.length)
        assertEquals(ActivityModelId.sha256(File("../../supracloud-jarvis/ira/care/models/activity_model.json").readBytes()), id.sha256)
    }

    @Test fun refusalsLowConfidenceAndClassesMapOntoTheClosedSetOfLabels() {
        val unknown = ActivityWindow.of(ActivityEstimate("unknown", 0.41, "low_confidence", "walking"), 0, 10_000)
        assertEquals(ActivityLabels.UNKNOWN, unknown.label)
        assertEquals(ActivityLabels.REASON_LOW_CONFIDENCE, unknown.reason)
        assertEquals("the candidate class is never carried on", 0.41, unknown.confidence!!, 0.0)
        assertEquals(ActivityLabels.REASON_TOO_SHORT, ActivityWindow.of(ActivityEstimate(null, 0.0, "too_short"), 0, 10_000).reason)
        for (r in listOf("flat_accelerometer", "flat_gyroscope", "saturated_accelerometer", "implausible_gravity", "not_finite")) {
            val x = ActivityWindow.of(ActivityEstimate(null, 0.0, r), 0, 10_000)
            assertEquals(ActivityLabels.UNAVAILABLE, x.label)
            assertEquals(ActivityLabels.REASON_SENSOR_FAULT, x.reason)
            assertNull(x.confidence)
        }
        val walking = ActivityWindow.of(ActivityEstimate("walking", 0.8, null, "walking"), 0, 10_000)
        assertEquals("walking", walking.label); assertNull(walking.reason)
    }

    // ── summarising on the watch ──────────────────────────────────────────────

    @Test fun windowsBecomeOneSummaryPerHourWithSecondsPerLabelAndReason() {
        val s = ActivitySummariser(id, ActivitySummariser.SOURCE_DEVICE)
        val out = mutableListOf<ActivityPeriod>()
        var t = 0L
        repeat(60) { out += s.add(w(t, "walking", 0.8)); t += 10 }                 // 10 min
        repeat(120) { out += s.add(w(t, "sitting", 0.9)); t += 10 }                // 20 min
        repeat(6) { out += s.add(w(t, "unknown", 0.4, "low_confidence")); t += 10 } // 1 min
        repeat(30) { out += s.add(w(t, "unavailable", null, "off_wrist")); t += 10 } // 5 min
        repeat(3) { out += s.add(w(t, "unavailable", null, "sensor_gap")); t += 10 }
        assertTrue("nothing leaves before the hour is over", out.isEmpty())
        val p = s.add(w(3600, "standing")).single()
        assertEquals("1970-01-01T00:00:00Z", p.periodStart)
        assertEquals("1970-01-01T01:00:00Z", p.periodEnd)
        assertEquals(mapOf("sitting" to 1200, "unavailable" to 330, "unknown" to 60, "walking" to 600), p.seconds)
        assertEquals(mapOf("low_confidence" to 60, "off_wrist" to 300, "sensor_gap" to 30), p.reasons)
        assertEquals(219, p.windows)
        assertEquals(id.sha256, p.modelSha256)
        assertEquals("device", p.source)
        // (60*0.8 + 120*0.9 + 6*0.4) / 186 = 0.8516, kept to three places
        assertEquals(0.852, p.meanConfidence!!, 1e-9)
        assertTrue("the rest of the hour is not recorded, not filled in", p.seconds.values.sum() < 3600)
    }

    @Test fun aWindowCountsInTheHourOfItsMidpointSoNoHourExceedsAnHour() {
        val s = ActivitySummariser(id, ActivitySummariser.SOURCE_DEVICE)
        val out = mutableListOf<ActivityPeriod>()
        // 10 s windows offset by 7 s from the hour, for two full hours
        var t = 7L
        while (t + 5 < 2 * 3600) { out += s.add(w(t, "sitting")); t += 10 }
        out += listOfNotNull(s.flushIfDue(3 * hour))
        assertEquals(2, out.size)
        out.forEach { assertTrue(it.seconds.values.sum() <= 3600 && it.windows <= 360) }
        assertEquals("every window counted once", 719, out.sumOf { it.windows })
    }

    @Test fun anHourClosesOnTimeEvenWhenNoFurtherWindowArrives() {
        val s = ActivitySummariser(id, ActivitySummariser.SOURCE_DEVICE)
        s.add(w(100, "walking"))
        assertNull(s.flushIfDue(hour - 1))
        assertEquals(10, s.flushIfDue(hour)!!.seconds["walking"])
        assertNull("closed once", s.flushIfDue(2 * hour))
        assertTrue("a late window never reopens a closed hour", s.add(w(200, "walking")).isEmpty())
        assertNull(s.flushIfDue(3 * hour))
    }

    // ── the monitor reports every window, classified or not ───────────────────

    private fun walk(m: ActivityMonitor, fromMs: Long, toMs: Long, gyroGapAt: LongRange? = null, flat: Boolean = false) {
        var tA = fromMs; var tG = fromMs
        while (tA < toMs || tG < toMs) {
            if (tA <= tG) {
                val s = tA / 1000.0
                if (flat) m.feedAccelG(tA, 0.0, 0.0, 1.0)
                else m.feedAccelG(tA, (0.3 + 2.5 * sin(2 * PI * 1.8 * s)) / g, (-2.0 + 1.5 * sin(2 * PI * 0.9 * s + 0.4)) / g,
                    (9.5 + 3.0 * sin(2 * PI * 1.8 * s + 1.1)) / g)
                tA += 20
            } else {
                val s = tG / 1000.0
                if (gyroGapAt == null || tG !in gyroGapAt)
                    m.feedGyro(tG, 1.2 * sin(2 * PI * 0.9 * s) + 0.01 * sin(7.0 * s), 0.6 * sin(2 * PI * 1.8 * s), 0.4 * kotlin.math.cos(2 * PI * 0.9 * s))
                tG += 50
            }
        }
    }

    @Test fun offTheWristAGapAndAStuckSensorAreUnavailableWithTheirReason() {
        val m = ActivityMonitor(model)
        val seen = mutableListOf<ActivityWindow>()
        m.onWindow = { seen += it }
        m.onWrist = true
        walk(m, 0, 21_000)
        assertEquals(2, seen.size)
        assertTrue(seen.none { it.label == ActivityLabels.UNAVAILABLE })
        seen.clear()
        walk(m, 21_000, 41_000, gyroGapAt = 25_000L..27_000L)
        assertTrue(seen.any { it.label == "unavailable" && it.reason == "sensor_gap" })
        seen.clear()
        walk(m, 41_000, 63_000, flat = true)
        val stuck = seen.last()                          // the first fully flat window
        assertEquals("unavailable", stuck.label); assertEquals("sensor_fault", stuck.reason); assertNull(stuck.confidence)
        seen.clear()
        m.onWrist = false
        walk(m, 63_000, 85_000)
        assertTrue(seen.isNotEmpty())
        assertTrue(seen.all { it.label == "unavailable" && it.reason == "off_wrist" })
        assertNull(m.latest)
    }

    // ── consent decides whether anything leaves ───────────────────────────────

    private class FakeServer(var sharing: Boolean = true, var outcome: (() -> ApiOutcome<ActivityAck>)? = null) {
        val received = mutableListOf<ActivityPeriod>()
        var calls = 0
        fun send(periods: List<ActivityPeriod>): ApiOutcome<ActivityAck> {
            calls++
            outcome?.let { return it() }
            if (sharing) received += periods
            return ApiOutcome.Ok(ActivityAck(periods.size, if (sharing) periods.size else 0, sharing))
        }
    }

    private fun sharing(server: FakeServer, q: ActivityQueue = queue()) =
        ActivitySharing(ActivitySummariser(id, ActivitySummariser.SOURCE_SIMULATED), q, server::send, ActivityToday { ZoneOffset.UTC })

    private fun feedHours(a: ActivitySharing, hours: Int) {
        for (h in 0 until hours) for (k in 0 until 6) a.onWindow(w(h * 3600L + k * 10, "walking"))
    }

    @Test fun withoutConsentNothingIsQueuedOrSent() {
        val server = FakeServer()
        val q = queue()
        val a = sharing(server, q)                       // the server has not said yes
        feedHours(a, 3)
        assertEquals(ActivitySharing.Result.NotShared, a.sync(4 * hour))
        assertEquals(0, server.calls)
        assertEquals(0, q.size())
        assertEquals(3, a.discarded)
        assertEquals("the wearer's own line still counts", 3, a.today.activeMinutes(4 * hour))
    }

    @Test fun withConsentClosedHoursAreSentOnceAndRemoved() {
        val server = FakeServer()
        val q = queue()
        val a = sharing(server, q)
        a.setSharing(true)
        feedHours(a, 3)
        assertEquals(2, q.size())                        // the third hour is still open
        assertEquals(ActivitySharing.Result.Sent, a.sync(3 * hour))
        assertEquals(3, server.received.size)
        assertEquals(0, q.size())
        assertTrue(server.received.all { it.source == "simulated" && it.seconds == mapOf("walking" to 60) })
        assertEquals(ActivitySharing.Result.NothingToSend, a.sync(3 * hour))
    }

    @Test fun consentWithdrawnOnTheServerClearsWhatIsWaitingAndStopsQueueing() {
        val server = FakeServer(sharing = false)
        val q = queue()
        val a = sharing(server, q)
        a.setSharing(true)                               // a stale yes on the watch
        feedHours(a, 3)
        assertEquals(ActivitySharing.Result.NotShared, a.sync(3 * hour))
        assertEquals(0, q.size())
        assertFalse(a.sharing)
        assertTrue(server.received.isEmpty())
        feedHours(a, 2)
        assertEquals(0, q.size())
    }

    @Test fun consentWithdrawnOnTheWatchClearsTheQueueAtOnce() {
        val q = queue()
        val a = sharing(FakeServer(), q)
        a.setSharing(true)
        feedHours(a, 3)
        assertEquals(2, q.size())
        a.setSharing(false)
        assertEquals(0, q.size())
    }

    @Test fun offlineKeepsEverythingAndARefusedBatchDoesNotBlockLaterHours() {
        val server = FakeServer(outcome = { ApiOutcome.Offline("no network") })
        val q = queue()
        val a = sharing(server, q)
        a.setSharing(true)
        feedHours(a, 3)
        assertEquals(ActivitySharing.Result.Offline, a.sync(3 * hour))
        assertEquals(3, q.size())
        server.outcome = { ApiOutcome.Failed(422, "bad") }
        a.sync(3 * hour)
        assertEquals(0, q.size())
    }

    @Test fun summariesWaitingAreSealedAndStayWithTheWearer() {
        val dir = Files.createTempDirectory("act").toFile()
        val key = javax.crypto.KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        var person: String? = "ada"
        val files = WearerFiles(dir, "activity") { person }
        val q = ActivityQueue({ files.current() }, sealer = AesGcmSealer { key })
        val a = sharing(FakeServer(), q)
        a.setSharing(true)
        feedHours(a, 2)
        assertTrue(File(dir, "activity-ada.jsonl").readLines().all { it.startsWith("s1:") })
        person = "bea"
        assertEquals(0, q.size())
        person = "ada"
        assertEquals(1, q.peek(10).size)
    }

    @Test fun onlyHourlySummariesCanLeaveTheWatch() {
        // No reading kind for activity; no urgent (outbox) path; one call site for the endpoint.
        assertFalse(File("src/main/java/care/ira/watch/sensors/SensorSource.kt").readText().contains("\"activity\""))
        assertFalse(File("src/main/java/care/ira/watch/outbox/Outbox.kt").readText().contains("activity", ignoreCase = true))
        val callers = File("src/main/java").walkTopDown().filter { it.isFile && it.extension == "kt" }
            .filter { "api.activity(" in it.readText() || "PATH_ACTIVITY" in it.readText() }
            .map { it.path.substringAfter("src/main/java/") }.toSortedSet()
        assertEquals(sortedSetOf("care/ira/watch/api/CareApi.kt", "care/ira/watch/service/CareDependencies.kt"), callers)
        // What is sent has no per-window field: no probabilities, no candidate class, no samples.
        val fields = ActivityPeriod.serializer().descriptor.let { d -> (0 until d.elementsCount).map(d::getElementName) }.toSet()
        assertEquals(setOf("period_start", "period_end", "model_version", "model_sha256", "source", "seconds", "reasons",
            "windows", "mean_confidence"), fields)
    }

    // ── the wearer's line ─────────────────────────────────────────────────────

    @Test fun theWearerSeesAModestEstimateOfMinutesOnTheMoveToday() {
        val today = ActivityToday { ZoneOffset.UTC }
        for (k in 0 until 18) today.add(w(36_000L + k * 10, "walking"))
        for (k in 0 until 30) today.add(w(40_000L + k * 10, "sitting"))
        for (k in 0 until 30) today.add(w(42_000L + k * 10, "eating_drinking"))
        assertEquals(3, today.activeMinutes(50_000_000))
        assertEquals("a new day starts at zero", 0, today.activeMinutes(50_000_000 + 86_400_000))
        assertNull(ActivityWearerText.line(0))
        val line = ActivityWearerText.line(3)!!
        assertTrue(line.contains("estimate"))
        for (word in listOf("ate", "eat", "meal", "health", "healthy", "goal", "well done", "should")) {
            assertFalse("'$word' in '$line'", Regex("\\b$word\\b", RegexOption.IGNORE_CASE).containsMatchIn(line))
        }
    }
}
