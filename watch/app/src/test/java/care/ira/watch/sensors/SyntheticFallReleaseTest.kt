package care.ira.watch.sensors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The synthetic fall trigger (SyntheticFallReceiver, `am broadcast`) is for the emulator journey
 * and lives in the debug source set alone. Gradle merges `src/debug` into debug variants only, so
 * no release build has the receiver or its manifest entry. These checks keep it there, the way
 * FallModelReleaseTest keeps the fall model where it belongs.
 */
class SyntheticFallReleaseTest {
    @Test
    fun `the debug source set holds the receiver and declares it`() {
        assertTrue("run from the app module", File("src/main/assets").isDirectory)
        val receiver = File("src/debug/java/care/ira/watch/debug/SyntheticFallReceiver.kt")
        assertTrue(receiver.exists())
        val manifest = File("src/debug/AndroidManifest.xml").readText()
        assertTrue(manifest.contains("care.ira.watch.debug.SyntheticFallReceiver"))
        assertTrue(manifest.contains("care.ira.watch.debug.SYNTHETIC_FALL"))
        // What the receiver injects is the detector's own event, through FallInjection in main.
        assertTrue(receiver.readText().contains("FallInjection.inject("))
    }

    @Test
    fun `no other source set names the receiver or its action`() {
        assertEquals(emptyList<String>(), syntheticFallReferences(File("src")).map { it.path })
    }

    @Test
    fun `main declares no receiver but the boot receiver`() {
        val main = File("src/main/AndroidManifest.xml").readText()
        val receivers = Regex("""<receiver[^>]*android:name="([^"]+)"""").findAll(main).map { it.groupValues[1] }.toList()
        assertEquals(listOf(".service.BootReceiver"), receivers)
    }

    @Test
    fun `the scan finds the receiver if it strays into another source set`() {
        val root = kotlin.io.path.createTempDirectory("ira-src").toFile()
        try {
            File(root, "debug/java").mkdirs()
            File(root, "debug/java/SyntheticFallReceiver.kt").writeText("class SyntheticFallReceiver")
            assertTrue(syntheticFallReferences(root).isEmpty())
            File(root, "release").mkdirs()
            File(root, "release/AndroidManifest.xml").writeText("""<receiver android:name="care.ira.watch.debug.SyntheticFallReceiver"/>""")
            File(root, "main/java").mkdirs()
            File(root, "main/java/Service.kt").writeText("""sendBroadcast(Intent("care.ira.watch.debug.SYNTHETIC_FALL"))""")
            assertEquals(listOf("main/java/Service.kt", "release/AndroidManifest.xml"),
                syntheticFallReferences(root).map { it.relativeTo(root).path }.sorted())
        } finally {
            root.deleteRecursively()
        }
    }

    /** Files outside `src/debug` and the tests that name the receiver or its broadcast action. */
    private fun syntheticFallReferences(src: File): List<File> =
        src.listFiles().orEmpty().filter { it.isDirectory && it.name != "debug" && it.name != "test" }
            .flatMap { set -> set.walkTopDown().filter { it.isFile }.toList() }
            .filter { f -> val t = f.readText(); MARKERS.any { t.contains(it) } }
            .sortedBy { it.path }

    companion object {
        val MARKERS = listOf("SyntheticFallReceiver", "SYNTHETIC_FALL")
    }
}
