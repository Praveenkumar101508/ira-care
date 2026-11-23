package care.ira.watch.platform

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.CareApi
import care.ira.watch.api.SosStateDto

/**
 * Every platform dependency the app has, behind an interface, so the same app
 * runs on Google's Wear OS and on our own AOSP watch (phase 8):
 *
 *   sensors        [care.ira.watch.sensors.SensorSource]: Health Services (wearos flavour),
 *                  the Android sensor framework (aosp flavour), or the simulated driver
 *   location       [care.ira.watch.sensors.LocationSource]: the platform LocationManager, one fix per SOS
 *   speech         [care.ira.watch.voice.Speaker] and [care.ira.watch.voice.Listener], over whatever
 *                  engine the device has; [SpeechCapabilities] says whether one is there
 *   haptics        [Haptics]
 *   connectivity   [Connectivity]
 *   push           [ServerChannel]: our own held connection to our server. No Firebase, no
 *                  Google Cloud Messaging: the watch is told things by the server it trusts.
 */

interface Haptics {
    /** The pattern the watch uses for a question, a repeat, a fall or a carer's arrival. */
    fun pattern(timingsMs: LongArray = DEFAULT)

    companion object {
        val DEFAULT = longArrayOf(0, 400, 200, 400, 200, 600)
        val SHORT = longArrayOf(0, 250)
    }
}

class AndroidHaptics(private val context: Context) : Haptics {
    override fun pattern(timingsMs: LongArray) {
        val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(VibratorManager::class.java)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION") context.getSystemService(Vibrator::class.java)
        } ?: return
        if (!vibrator.hasVibrator()) return
        vibrator.vibrate(VibrationEffect.createWaveform(timingsMs, -1))
    }
}

interface Connectivity {
    /** The device believes it has a route to the internet right now. The server's answer is the truth. */
    fun isOnline(): Boolean
}

class AndroidConnectivity(private val context: Context) : Connectivity {
    override fun isOnline(): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return true
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }
}

/** What speech the device offers. Both are optional on AOSP: an engine ships with the image or not. */
data class SpeechCapabilities(val canSpeak: Boolean, val canListen: Boolean) {
    companion object {
        fun probe(context: Context): SpeechCapabilities {
            val engines = runCatching { TextToSpeech(context, null).engines.isNotEmpty() }.getOrDefault(false)
            val listen = runCatching { SpeechRecognizer.isRecognitionAvailable(context) }.getOrDefault(false)
            return SpeechCapabilities(canSpeak = engines, canListen = listen)
        }
    }
}

/**
 * Push, our way: the server holds a connection open to the watch and writes a
 * JSON line whenever something changes (the SOS state, a carer's arrival). No
 * third party sits between the server and the wrist. `run` returns when the
 * server closes the connection or the link drops; the caller reconnects.
 */
interface ServerChannel {
    fun run(onMessage: (SosStateDto) -> Unit): ApiOutcome<Unit>
}

class SosStreamChannel(private val api: CareApi) : ServerChannel {
    override fun run(onMessage: (SosStateDto) -> Unit): ApiOutcome<Unit> = api.sosStream(onMessage)
}
