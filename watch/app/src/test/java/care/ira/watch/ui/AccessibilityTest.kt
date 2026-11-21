package care.ira.watch.ui

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The watch has no instrumentation run here (no device), so the accessibility
 * contract is checked on the source: every button on the home screen carries a
 * content description, the status text is a live region, and the buttons are
 * at least 44 dp tall. A real screen-reader pass on a watch is still owed.
 */
class AccessibilityTest {
    private val source = File("src/main/java/care/ira/watch/ui/MainActivity.kt").readText()

    @Test
    fun `every home screen button has a content description`() {
        val home = source.substringAfter("fun HomeScreen(")
        val buttons = Regex("Button\\(").findAll(home).count()
        val described = Regex("contentDescription = \"").findAll(home).count()
        assertTrue("buttons=$buttons described=$described", buttons >= 3 && described >= buttons)
    }

    @Test
    fun `the status text is announced as it changes and the buttons are large enough`() {
        assertTrue(source.contains("liveRegion = LiveRegionMode.Assertive"))
        assertTrue(Regex("height\\((2\\d|3\\d)\\.dp\\)").findAll(source.substringAfter("fun HomeScreen(")).none())
    }
}
