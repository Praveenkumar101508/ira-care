package care.ira.watch.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element

/**
 * The watch app as IRA CARE OS's home screen (os/device/ira/ira_watch_cf/HOME_LAUNCHER.md).
 * No device here, so the manifests and the activity are checked as files, and the policy on
 * the JVM. A boot on Cuttlefish or a watch is still owed.
 */
class HomeScreenTest {
    private val ns = "http://schemas.android.com/apk/res/android"

    private fun mainActivity(path: String): Element {
        val f = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        val doc = f.newDocumentBuilder().parse(File(path))
        val acts = doc.getElementsByTagName("activity")
        for (i in 0 until acts.length) {
            val a = acts.item(i) as Element
            if (a.getAttributeNS(ns, "name") == ".ui.MainActivity") return a
        }
        error("no MainActivity in $path")
    }

    private fun categories(a: Element): Set<String> {
        val out = mutableSetOf<String>()
        val cs = a.getElementsByTagName("category")
        for (i in 0 until cs.length) out += (cs.item(i) as Element).getAttributeNS(ns, "name")
        return out
    }

    @Test
    fun `on IRA CARE OS the app is the home screen and Help shows over the lock screen`() {
        val aosp = mainActivity("src/aosp/AndroidManifest.xml")
        assertTrue(categories(aosp).containsAll(setOf("android.intent.category.HOME", "android.intent.category.DEFAULT")))
        assertEquals("true", aosp.getAttributeNS(ns, "showWhenLocked"))
        assertEquals("true", aosp.getAttributeNS(ns, "turnScreenOn"))
    }

    @Test
    fun `on a Wear OS watch from a shop the app never offers itself as the system home`() {
        val main = mainActivity("src/main/AndroidManifest.xml")
        assertFalse(categories(main).contains("android.intent.category.HOME"))
        assertFalse(File("src/wearos/AndroidManifest.xml").takeIf { it.exists() }?.readText()?.contains("category.HOME") ?: false)
        // A home activity must be single-task, or the home gesture stacks copies of it.
        assertEquals("singleTask", main.getAttributeNS(ns, "launchMode"))
    }

    @Test
    fun `back does nothing on the OS home screen and behaves normally on Wear OS`() {
        assertTrue(HomePolicy.backDoesNothing("aosp"))
        assertFalse(HomePolicy.backDoesNothing("wearos"))
        val src = File("src/main/java/care/ira/watch/ui/MainActivity.kt").readText()
        assertTrue(src.contains("if (HomePolicy.backDoesNothing(BuildConfig.PLATFORM))"))
    }

    @Test
    fun `nobody is sent into Settings for a permission they already have`() {
        assertFalse(HomePolicy.needsSettings(backgroundSensorsGranted = true))
        assertTrue(HomePolicy.needsSettings(backgroundSensorsGranted = false))
        val src = File("src/main/java/care/ira/watch/ui/MainActivity.kt").readText()
        val screen = src.substringAfter("private fun PermissionScreen(")
        assertEquals("Settings is opened in exactly one place, behind the check", 1,
            Regex("ACTION_APPLICATION_DETAILS_SETTINGS").findAll(screen).count())
        assertTrue(screen.contains("if (HomePolicy.needsSettings(granted))"))
    }

    @Test
    fun `a permission granted in Settings takes effect when the person comes back`() {
        val src = File("src/main/java/care/ira/watch/ui/MainActivity.kt").readText()
        val resume = src.substringAfter("override fun onResume()").substringBefore("@Composable")
        assertTrue(resume.contains("backgroundPermissionGranted()") && resume.contains("permissionGranted ="))
    }
}
