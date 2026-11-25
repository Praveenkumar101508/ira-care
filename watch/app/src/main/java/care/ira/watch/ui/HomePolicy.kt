package care.ira.watch.ui

/**
 * How the main screen behaves as a home screen, kept apart from Android so it is tested on the
 * JVM. Pure Kotlin.
 */
object HomePolicy {
    /** On IRA CARE OS the app is the home screen: back has nowhere to go, so it does nothing.
     *  (A home activity that finishes leaves an empty screen until the system restarts it.)
     *  On a Wear OS watch from a shop, back behaves as Wear OS expects. */
    fun backDoesNothing(platform: String): Boolean = platform == "aosp"

    /** After the permission dialog: send the person to Settings only when background sensor
     *  access is still missing. Where it was granted at first boot, or in the dialog, nobody is
     *  sent into Settings. */
    fun needsSettings(backgroundSensorsGranted: Boolean): Boolean = !backgroundSensorsGranted
}
