package care.ira.watch.activity

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import kotlin.math.PI
import kotlin.math.sin

/**
 * Preparation for the battery measurement (a device item): how long one window costs on the JVM,
 * from raw samples at the watch's rates to a closed hourly summary. The numbers are written to
 * `build/activity-cost.txt` and quoted in docs/ai/activity/MODEL_CARD.md with the machine they
 * came from. This is NOT the watch's cost: a watch CPU is several times slower and the energy
 * also depends on the sensors, which only a watch can measure. The bound asserted here is loose
 * on purpose; it catches an accidental order-of-magnitude regression, not a few per cent.
 */
class ActivityCostTest {
    private val modelText = File("src/main/assets/${ActivityModel.ASSET}").readText()
    private val g = 9.80665

    @Test fun oneWindowFromSamplesToSummaryCostsMillisecondsOnTheJvm() {
        val model = ActivityModel.parse(modelText)
        val monitor = ActivityMonitor(model)
        monitor.onWrist = true
        val sharing = ActivitySharing(ActivitySummariser(ActivityModelId.of(modelText), ActivitySummariser.SOURCE_SIMULATED),
            ActivityQueue({ File.createTempFile("cost", ".jsonl") }), { error("nothing is sent here") })
        val windowNanos = mutableListOf<Long>()
        var feedNanos = 0L
        monitor.onWindow = { w -> sharing.onWindow(w) }
        fun feed(fromMs: Long, toMs: Long) {
            var tA = fromMs; var tG = fromMs
            while (tA < toMs || tG < toMs) {
                val t0 = System.nanoTime()
                val est: ActivityEstimate?
                if (tA <= tG) {
                    val s = tA / 1000.0
                    est = monitor.feedAccelG(tA, (0.3 + 2.5 * sin(2 * PI * 1.8 * s)) / g, (-2.0 + 1.5 * sin(2 * PI * 0.9 * s + 0.4)) / g,
                        (9.5 + 3.0 * sin(2 * PI * 1.8 * s + 1.1)) / g)
                    tA += 20                               // accelerometer about 50 Hz
                } else {
                    val s = tG / 1000.0
                    est = monitor.feedGyro(tG, 1.2 * sin(2 * PI * 0.9 * s), 0.6 * sin(2 * PI * 1.8 * s), 0.4 * kotlin.math.cos(2 * PI * 0.9 * s))
                    tG += 50                               // gyroscope 20 Hz
                }
                val dt = System.nanoTime() - t0
                if (est != null) windowNanos += dt else feedNanos += dt
            }
        }
        feed(0, 120_000)                                   // warm-up: two minutes, not counted
        windowNanos.clear(); feedNanos = 0
        val minutes = 20
        feed(120_000, 120_000 + minutes * 60_000L)
        val sorted = windowNanos.sorted()
        val median = sorted[sorted.size / 2] / 1e6
        val p95 = sorted[(sorted.size * 95) / 100] / 1e6
        val windows = sorted.size
        val perWindowFeed = feedNanos / 1e6 / windows
        val busyPerHourS = (median + perWindowFeed) * 360 / 1000
        val report = buildString {
            appendLine("activity cost on the JVM (${System.getProperty("java.vm.name")} ${System.getProperty("java.version")}, " +
                "${System.getProperty("os.arch")}, ${Runtime.getRuntime().availableProcessors()} CPUs)")
            appendLine("windows measured: $windows (20 min of 50 Hz accelerometer and 20 Hz gyroscope)")
            appendLine("classify + summarise, per window: median %.2f ms, p95 %.2f ms".format(median, p95))
            appendLine("sample intake between windows, per window: %.2f ms".format(perWindowFeed))
            appendLine("CPU busy per hour (360 windows): %.2f s, a duty cycle of %.3f %%".format(busyPerHourS, busyPerHourS / 3600 * 100))
        }
        File("build").mkdirs()
        File("build/activity-cost.txt").writeText(report)
        println(report)
        assertTrue("about one window per 10 s of samples", windows in 115..121)
        assertTrue("median per window $median ms", median < 250.0)
    }
}
