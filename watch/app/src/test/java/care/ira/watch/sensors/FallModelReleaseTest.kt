package care.ira.watch.sensors

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/**
 * Which fall model a build of ANY kind may carry (D-077, D-080, D-082).
 *
 * Gradle merges `src/main/assets` into every variant and each other source set (`src/debug`,
 * `src/release`, `src/wearos`, `src/aospDebug`, ...) into the variants it names. D-077 kept the
 * SisFall-trained v1 model (terms unconfirmed) out of release builds; D-082 takes it out of every
 * build, debug and demonstration included. D-080 lets one model in: v3, trained only on BITS-2
 * and UCI HAR (both CC BY 4.0). So:
 *   - `src/main/assets` holds a fall model only if it is that exact v3 file;
 *   - no other source set holds a fall model of its own (it would override main in its variants);
 *   - no file in ANY source set's assets is SisFall-derived: not the v1 weights or spec by hash,
 *     and nothing that names SisFall as its training data.
 */
class FallModelReleaseTest {
    private val modelFiles = listOf(TfLiteInference.MODEL_ASSET, TfLiteInference.SPEC_ASSET)

    private fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    private fun sha256(f: File) = sha256(f.readBytes())

    /** Every `assets` directory of every source set under [src], whatever variant it belongs to. */
    private fun assetDirs(src: File = File("src")): List<File> =
        src.listFiles().orEmpty().filter { it.isDirectory }.map { File(it, "assets") }.filter { it.isDirectory }

    @Test
    fun `no source set other than main carries a fall model of its own`() {
        assertTrue("run from the app module", File("src/main/assets").isDirectory)
        for (dir in assetDirs()) {
            if (dir.parentFile?.name == "main") continue
            for (f in modelFiles) assertFalse("${dir.path}/$f overrides v3 in its variants", File(dir, f).exists())
        }
    }

    @Test
    fun `the model every build carries is the licensed v3 and nothing else`() {
        val tflite = File("src/main/assets/${TfLiteInference.MODEL_ASSET}")
        val specFile = File("src/main/assets/${TfLiteInference.SPEC_ASSET}")
        assertTrue("v3 ships in src/main/assets", tflite.exists() && specFile.exists())
        val spec = FallModelSpec.parse(specFile.readText())
        assertEquals(FallModelSpec.PROVENANCE_LICENSED_V3, spec.provenance)
        assertEquals(FallModelSpec.MODE_RECOVER, spec.mode)
        val pinned = Json.parseToJsonElement(specFile.readText()).jsonObject["tfliteSha256"]?.jsonPrimitive?.content
        assertEquals("the spec names the file it belongs to", sha256(tflite), pinned)
        assertEquals("the reviewed v3 file, byte for byte", LICENSED_V3_SHA256, sha256(tflite))
        assertTrue("the watch itself would run it", spec.mayRun(sha256(tflite)))
    }

    @Test
    fun `no SisFall-derived file is in the assets of any variant`() {
        val found = sisfallDerived(assetDirs())
        assertTrue("SisFall-derived files in a build source set: $found", found.isEmpty())
    }

    @Test
    fun `the scan finds the v1 provenance marker and the v1 weights in a debug source set`() {
        val root = kotlin.io.path.createTempDirectory("ira-src").toFile()
        try {
            val debugAssets = File(root, "debug/assets").apply { mkdirs() }
            File(debugAssets, "fall_model.json").writeText(V1_SPEC_FIXTURE)
            assertEquals(listOf("debug/assets/fall_model.json"), sisfallDerived(assetDirs(root)).map { it.relativeTo(root).path })
            // The weights by hash alone, under a name that hides them (stand-in bytes, their hash added to the known set).
            File(debugAssets, "fall_model.json").delete()
            val disguised = File(debugAssets, "something.bin").apply { writeBytes(byteArrayOf(1, 2, 3)) }
            assertTrue(sisfallDerived(assetDirs(root)).isEmpty())
            assertTrue(sisfallDerived(assetDirs(root), knownHashes = SISFALL_DERIVED_SHA256 + sha256(disguised)).isNotEmpty())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `the watch refuses to run v1 or any unreviewed model even if one reappears`() {
        val v1 = FallModelSpec.parse(V1_SPEC_FIXTURE)
        assertFalse(v1.mayRun(V1_TFLITE_SHA256))
        val relabelled = v1.copy(provenance = FallModelSpec.PROVENANCE_LICENSED_V3, tfliteSha256 = V1_TFLITE_SHA256)
        assertFalse("a v1 file labelled as v3 is still refused", relabelled.mayRun(V1_TFLITE_SHA256))
        val v3 = FallModelSpec.parse(File("src/main/assets/${TfLiteInference.SPEC_ASSET}").readText())
        assertFalse("the v3 spec beside a different file is refused", v3.mayRun(V1_TFLITE_SHA256))
        assertTrue(v3.mayRun(LICENSED_V3_SHA256))
    }

    /** Files in [dirs] that are SisFall-derived: a known hash, or bytes naming SisFall. */
    private fun sisfallDerived(dirs: List<File>, knownHashes: Set<String> = SISFALL_DERIVED_SHA256): List<File> =
        dirs.flatMap { d -> d.walkTopDown().filter { it.isFile }.toList() }.filter { f ->
            val bytes = f.readBytes()
            sha256(bytes) in knownHashes || String(bytes, Charsets.ISO_8859_1).contains("sisfall", ignoreCase = true)
        }.sortedBy { it.path }

    companion object {
        /** scripts/training/fall_model_v3.py, reproduced byte for byte in two runs (MODEL_CARD_V3.md). */
        const val LICENSED_V3_SHA256 = "2cab7c9bcbf60de51d7c42f5690ad549bcbf4f13eb6266c35bf5d0a59b063278"
        /** v1 (scripts/training/kaggle_fall_model.py): removed from the tree (D-082); its hash is kept so no build can carry it. */
        const val V1_TFLITE_SHA256 = "8182ea37154ab52e1d9383b3de42add2a2bb902cafb38daa6056f767d84ae6cb"
        /** The v1 spec that debug builds carried until D-082. */
        const val V1_SPEC_SHA256 = "5bb67e334955f3c4ae07da6b4d0dad93a9df666f13d80ada65eec9a03981eff6"
        val SISFALL_DERIVED_SHA256 = setOf(V1_TFLITE_SHA256, V1_SPEC_SHA256)
        /** The provenance of the v1 spec, as it was in `src/debug/assets/fall_model.json`. */
        val V1_SPEC_FIXTURE = """
            {"threshold": 0.9, "mean": [0.0842, -0.6421, -0.0668, 1.0339], "std": [0.4803, 0.5781, 0.4554, 0.3523],
             "windowSamples": 200, "rateHz": 50,
             "trained_on": "SisFall (Zenodo 22212281, CC BY 4.0) and UCI HAR (CC BY 4.0), subject split; see docs/evidence/training/fall.json"}
        """.trimIndent()
    }
}
