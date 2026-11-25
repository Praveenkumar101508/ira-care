package care.ira.watch.sensors

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * D-077: the fall model trained on SisFall (terms unconfirmed) never ships in a release build.
 * Gradle merges `src/main/assets` into every variant and `src/debug/assets` into debug variants
 * only, so the model's place on disk decides which builds carry it. Release builds then find no
 * model (`TfLiteInference.load` returns null) and detect falls with the rules.
 */
class FallModelReleaseTest {
    private val modelFiles = listOf(TfLiteInference.MODEL_ASSET, TfLiteInference.SPEC_ASSET)

    @Test
    fun `no release build carries the research fall model`() {
        for (set in listOf("main", "release", "wearosRelease", "aospRelease", "wearos", "aosp")) {
            for (f in modelFiles) assertFalse("src/$set/assets/$f would ship in a release build", File("src/$set/assets/$f").exists())
        }
    }

    @Test
    fun `engineering and demonstration builds keep it, so the comparison can still be shown`() {
        for (f in modelFiles) assertTrue("src/debug/assets/$f", File("src/debug/assets/$f").exists())
    }
}
