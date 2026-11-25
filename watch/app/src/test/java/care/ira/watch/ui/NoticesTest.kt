package care.ira.watch.ui

import java.io.ByteArrayInputStream
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Each watch build carries its own third-party notices and shows them under About.
 *
 * Gradle merges a flavour's source set (src/wearos, src/aosp) into that flavour's APK only, so
 * each flavour's assets hold the notices file of that build: docs/legal/notices/watch-<flavour>.txt
 * as `scripts/supply_chain.py render` writes it. The two builds differ (the Wear OS build has
 * Google's Health Services, the AOSP build has not), which the checks below use to show each
 * file is its own build's. Run from the app module, like the other source checks.
 */
class NoticesTest {
    private val flavours = mapOf(
        "wearos" to "IRA CARE watch app, Wear OS build (release APK)",
        "aosp" to "IRA CARE watch app, plain Android (AOSP) build (release APK)",
    )
    private fun asset(flavour: String) = File("src/$flavour/assets/${Notices.ASSET}")
    private fun read(dir: String) = Notices.read { File(dir, it).inputStream() }

    @Test
    fun `each flavour carries the notices of its own build, unedited`() {
        for ((flavour, title) in flavours) {
            assertTrue("${asset(flavour)} is missing", asset(flavour).isFile)
            val text = read("src/$flavour/assets")
            assertEquals(File("../../docs/legal/notices/watch-$flavour.txt").readText(), text)
            assertEquals(title, Notices.title(text))
            assertTrue(text.contains("\norg.tensorflow:tensorflow-lite 2.16.1\n"))
        }
        assertTrue(read("src/wearos/assets").contains("\nandroidx.health:health-services-client "))
        assertFalse(read("src/aosp/assets").contains("health-services-client"))
    }

    @Test
    fun `no notices file in main, where it would be the same for both builds`() {
        assertFalse(File("src/main/assets/${Notices.ASSET}").exists())
    }

    @Test
    fun `a file that is not a notices file is refused, never shown as an empty list`() {
        assertThrows(IllegalArgumentException::class.java) {
            Notices.read { ByteArrayInputStream("hello\n".toByteArray()) }
        }
        // Negative control for the flavour check: the other build's file names the other title.
        assertEquals(flavours["aosp"], Notices.title(read("src/aosp/assets")))
        assertFalse(Notices.title(read("src/aosp/assets")) == flavours["wearos"])
    }

    @Test
    fun `the list shows every paragraph, licence texts included`() {
        val text = read("src/wearos/assets")
        val paragraphs = Notices.paragraphs(text)
        fun lines(t: String) = t.lines().filter { it.isNotBlank() }.map { it.trimEnd() }
        assertEquals(lines(text), lines(paragraphs.joinToString("\n")))
        assertTrue(paragraphs.first().startsWith(Notices.HEADING))
        assertTrue(paragraphs.any { it.startsWith("----- Apache-2.0 -----") })
        assertEquals(listOf("a", "b"), Notices.paragraphs("a\n\n\nb\n"))
    }

    @Test
    fun `home has an About button that opens the licences, and the screen reads the asset`() {
        val source = File("src/main/java/care/ira/watch/ui/MainActivity.kt").readText()
        val home = source.substringAfter("fun HomeScreen(")
        assertTrue(Regex("onClick = onAbout,\\s+modifier = [^\\n]*contentDescription = \"About\\.").containsMatchIn(home))
        assertTrue(source.contains("about -> AboutScreen(onBack = { about = false })"))
        assertTrue(source.contains("onAbout = { about = true }"))
        assertTrue(source.contains("Notices.read { assets.open(it) }"))
        assertTrue(source.contains("BackHandler(enabled = about)"))
    }
}
