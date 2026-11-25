package care.ira.watch.activity

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import kotlin.math.abs
import kotlin.math.max

/**
 * The watch's IRA Care Activity gives the backend's answers, to 1e-6, on the fixture the
 * Python reference wrote (scripts/training/activity_watch_fixture.py): the same refusals,
 * the same 121 features, the same probabilities and the same class.
 */
class ActivityParityTest {
    private val model = ActivityModel.parse(File("src/main/assets/${ActivityModel.ASSET}").readText())
    private val fixture = Json.parseToJsonElement(File("src/test/resources/activity_parity.json").readText()).jsonObject

    private fun matrix(o: JsonObject, key: String): Array<DoubleArray> = o.getValue(key).jsonArray.map { row ->
        row.jsonArray.map { if (it is JsonNull) Double.NaN else it.jsonPrimitive.double }.toDoubleArray()
    }.toTypedArray()

    private fun close(a: Double, b: Double) = abs(a - b) <= 1e-6 * max(1.0, abs(b))

    @Test fun theAssetIsTheBackendsModelByteForByte() {
        assertArrayEquals(File("../../supracloud-jarvis/ira/care/models/activity_model.json").readBytes(),
            File("src/main/assets/${ActivityModel.ASSET}").readBytes())
    }

    @Test fun featureNamesMatchTheReference() {
        assertEquals(fixture.getValue("feature_names").jsonArray.map { it.jsonPrimitive.content }, ActivityFeatures.FEATURE_NAMES)
    }

    @Test fun everyFixtureCaseGivesTheReferenceAnswer() {
        val cases = fixture.getValue("cases").jsonArray
        assertTrue(cases.size >= 10)
        for (c in cases.map { it.jsonObject }) {
            val name = c.getValue("name").jsonPrimitive.content
            val acc = matrix(c, "acc"); val gyro = matrix(c, "gyro")
            val fs = c.getValue("fs_hz").jsonPrimitive.double
            val gfs = c.getValue("gyro_fs_hz").jsonPrimitive.double
            val est = model.estimate(acc, gyro, fs, gfs)
            val want = c["activity"]?.let { if (it is JsonNull) null else it.jsonPrimitive.content }
            val reason = c["reason"]?.let { if (it is JsonNull) null else it.jsonPrimitive.content }
            assertEquals("$name: activity", want, est.activity)
            assertEquals("$name: reason", reason, est.reason)
            if (c.containsKey("features")) {
                val checked = ActivityFeatures.checkWindow(acc, gyro, fs, gfs)
                val got = ActivityFeatures.windowFeatures(checked.acc!!, checked.gyro!!)
                val ref = c.getValue("features").jsonArray.map { it.jsonPrimitive.double }
                assertEquals("$name: feature count", ref.size, got.size)
                for (i in ref.indices) if (!close(got[i], ref[i]))
                    fail("$name: feature ${ActivityFeatures.FEATURE_NAMES[i]} = ${got[i]}, reference ${ref[i]}")
                val p = model.predictProba(got)
                val refP = c.getValue("probabilities").jsonArray.map { it.jsonPrimitive.double }
                for (i in refP.indices) assertTrue("$name: p[$i] ${p[i]} vs ${refP[i]}", abs(p[i] - refP[i]) <= 1e-6)
                assertTrue("$name: confidence", abs(est.confidence - c.getValue("confidence").jsonPrimitive.double) <= 1e-6)
            } else {
                assertNull("$name: a refused window has no estimate", est.activity)
            }
        }
    }

    @Test fun belowTheConfidenceThresholdTheAnswerIsUnknownNeverAGuess() {
        val c = fixture.getValue("cases").jsonArray.first { it.jsonObject.getValue("name").jsonPrimitive.content == "eat_20hz" }.jsonObject
        val strict = ActivityModel.parse(File("src/main/assets/${ActivityModel.ASSET}").readText()
            .replace("\"min_confidence\":0.5", "\"min_confidence\":0.999"))
        val est = strict.estimate(matrix(c, "acc"), matrix(c, "gyro"))
        assertEquals(ActivityModel.UNKNOWN, est.activity)
        assertEquals("low_confidence", est.reason)
        assertEquals("sitting", est.candidate)
    }

    @Test fun aMalformedModelIsRefusedNotHalfUsed() {
        val good = File("src/main/assets/${ActivityModel.ASSET}").readText()
        for ((label, bad) in listOf(
            "format" to good.replace(ActivityModel.FORMAT, "something-else"),
            "features" to good.replaceFirst("\"acc_x_mean\"", "\"acc_x_avg\""),
            "not json" to "{",
        )) {
            try { ActivityModel.parse(bad); fail("$label: accepted a malformed model") } catch (e: Exception) { /* refused */ }
        }
    }
}
