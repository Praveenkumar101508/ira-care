package care.ira.watch.testserver

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The test server screen (typing a care server address on the watch) is in `src/debug` alone,
 * and release keeps its build-time address and Android's refusal of cleartext. Gradle merges
 * `src/debug` into debug variants only and `src/release` into release variants only. These checks
 * read the sources, so they hold whichever variant runs them; ReleaseServerAddressTest runs inside
 * the release variant itself, and scripts/ci/check_watch_release_server.py reads the built APKs.
 */
class TestServerSourceSetTest {
    @Test
    fun `the screen and the stored address are in the debug source set`() {
        assertTrue("run from the app module", File("src/main/assets").isDirectory)
        val debug = File("src/debug/java/care/ira/watch/testserver/TestServer.kt").readText()
        for (marker in SCREEN_MARKERS) assertTrue(marker, debug.contains(marker))
        assertTrue(debug.contains("ServerAddress.check("))
    }

    @Test
    fun `the release TestServer has no screen, no storage and only the build's address`() {
        val release = File("src/release/java/care/ira/watch/testserver/TestServer.kt").readText()
        assertEquals(emptyList<String>(), SCREEN_MARKERS.filter { release.contains(it) })
        assertTrue(release.contains("fun baseUrl(context: Context): String = BuildConfig.CARE_API_BASE_URL"))
        assertTrue(release.contains("addressScreen: (@Composable (onDone: () -> Unit) -> Unit)? = null"))
    }

    @Test
    fun `only the debug source set carries the screen`() {
        assertEquals(emptyList<String>(), screenReferences(File("src")).map { it.path })
    }

    @Test
    fun `the release build's address and cleartext policy are as they were`() {
        val gradle = File("build.gradle.kts").readText()
        assertTrue(gradle.contains(
            "buildConfigField(\"String\", \"CARE_API_BASE_URL\", \"\\\"\${secret(\"IRA_CARE_API_BASE_URL\") ?: \"https://care.example.invalid\"}\\\"\")"))
        // No manifest outside debug turns cleartext on or names a network security config, and only
        // debug has one: release keeps Android's default for targetSdk 28 and later, no cleartext.
        assertTrue(Regex("""targetSdk\s*=\s*(\d+)""").find(gradle)!!.groupValues[1].toInt() >= 28)
        for (set in File("src").listFiles().orEmpty().filter { it.isDirectory && it.name != "debug" && it.name != "test" && !it.name.startsWith("test") }) {
            val manifest = File(set, "AndroidManifest.xml")
            if (manifest.exists()) {
                val text = manifest.readText()
                assertFalse(manifest.path, text.contains("usesCleartextTraffic"))
                assertFalse(manifest.path, text.contains("networkSecurityConfig"))
            }
            assertFalse(set.path, File(set, "res/xml/network_security_config.xml").exists())
        }
        val config = File("src/debug/res/xml/network_security_config.xml").readText()
        assertTrue(config.contains("<base-config cleartextTrafficPermitted=\"false\" />"))
        val cleartextHosts = Regex("""<domain[^>]*>([^<]+)</domain>""").findAll(config).map { it.groupValues[1] }.toList()
        assertEquals(listOf("10.0.2.2", "localhost", "127.0.0.1"), cleartextHosts)
    }

    @Test
    fun `the scan finds the screen if it strays into another source set`() {
        val root = kotlin.io.path.createTempDirectory("ira-src").toFile()
        try {
            File(root, "debug/java").mkdirs()
            File(root, "debug/java/TestServer.kt").writeText("@Composable private fun TestServerScreen() {}")
            assertTrue(screenReferences(root).isEmpty())
            File(root, "release/java").mkdirs()
            File(root, "release/java/TestServer.kt").writeText("val p = context.getSharedPreferences(\"care_test_server\", 0)")
            File(root, "wearos/java").mkdirs()
            File(root, "wearos/java/Screen.kt").writeText("TestServerScreen(onDone)")
            assertEquals(listOf("release/java/TestServer.kt", "wearos/java/Screen.kt"),
                screenReferences(root).map { it.relativeTo(root).path }.sorted())
        } finally {
            root.deleteRecursively()
        }
    }

    /** Files outside `src/debug` and the tests that hold the screen or the stored address. */
    private fun screenReferences(src: File): List<File> =
        src.listFiles().orEmpty().filter { it.isDirectory && it.name != "debug" && !it.name.startsWith("test") }
            .flatMap { set -> set.walkTopDown().filter { it.isFile }.toList() }
            .filter { f -> val t = f.readText(); SCREEN_MARKERS.any { t.contains(it) } }
            .sortedBy { it.path }

    companion object {
        val SCREEN_MARKERS = listOf("TestServerScreen", "care_test_server")
    }
}
