package care.ira.watch.sensors

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/**
 * Which fall model a release build may carry (D-077, D-080).
 *
 * Gradle merges `src/main/assets` into every variant and `src/debug/assets` into debug variants
 * only (where it wins over main). D-077 kept the SisFall-trained v1 model (terms unconfirmed) out
 * of every release build. D-080 lets ONE model into release builds: v3, trained only on BITS-2 and
 * UCI HAR (both CC BY 4.0), which passed its held-out gate against the rules (MODEL_CARD_V3.md).
 * So `src/main/assets` may hold a model only if it is that exact file: its spec names the licensed
 * provenance and the "recover" mode, and its SHA-256 is the one pinned here and in the spec. A
 * different model in main, including v1, fails this test.
 */
class FallModelReleaseTest {
    private val modelFiles = listOf(TfLiteInference.MODEL_ASSET, TfLiteInference.SPEC_ASSET)

    private fun sha256(f: File) = MessageDigest.getInstance("SHA-256").digest(f.readBytes()).joinToString("") { "%02x".format(it) }

    @Test
    fun `release-only source sets carry no model of their own`() {
        for (set in listOf("release", "wearosRelease", "aospRelease", "wearos", "aosp")) {
            for (f in modelFiles) assertFalse("src/$set/assets/$f", File("src/$set/assets/$f").exists())
        }
    }

    @Test
    fun `the model every release build carries is the licensed v3 and nothing else`() {
        val tflite = File("src/main/assets/${TfLiteInference.MODEL_ASSET}")
        val specFile = File("src/main/assets/${TfLiteInference.SPEC_ASSET}")
        assertTrue("v3 ships in src/main/assets", tflite.exists() && specFile.exists())
        val spec = FallModelSpec.parse(specFile.readText())
        assertEquals(FallModelSpec.PROVENANCE_LICENSED_V3, spec.provenance)
        assertEquals(FallModelSpec.MODE_RECOVER, spec.mode)
        val pinned = Json.parseToJsonElement(specFile.readText()).jsonObject["tfliteSha256"]?.jsonPrimitive?.content
        assertEquals("the spec names the file it belongs to", sha256(tflite), pinned)
        assertEquals("the reviewed v3 file, byte for byte", LICENSED_V3_SHA256, sha256(tflite))
    }

    @Test
    fun `the SisFall-trained v1 stays in debug builds only and never in main`() {
        val v1 = File("src/debug/assets/${TfLiteInference.MODEL_ASSET}")
        assertTrue("debug builds keep v1 for the comparison", v1.exists())
        val v1spec = FallModelSpec.parse(File("src/debug/assets/${TfLiteInference.SPEC_ASSET}").readText())
        assertNotEquals(FallModelSpec.PROVENANCE_LICENSED_V3, v1spec.provenance)
        assertNotEquals(sha256(v1), sha256(File("src/main/assets/${TfLiteInference.MODEL_ASSET}")))
    }

    companion object {
        /** scripts/training/fall_model_v3.py, reproduced byte for byte in two runs (MODEL_CARD_V3.md). */
        const val LICENSED_V3_SHA256 = "2cab7c9bcbf60de51d7c42f5690ad549bcbf4f13eb6266c35bf5d0a59b063278"
    }
}
