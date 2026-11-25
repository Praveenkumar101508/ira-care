package care.ira.watch.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import kotlinx.coroutines.Dispatchers

/** Something that can say a sentence aloud. The real one wraps Android's TextToSpeech. */
interface Speaker {
    suspend fun speak(text: String, louder: Boolean)
    /** The person's pace (care/personal.py): 0.8 slow, 0.9 normal. */
    fun setRate(rate: Float) {}
    fun shutdown() {}
}

/**
 * Android's TextToSpeech. The rules (wait until ready, one sentence at a time, a time limit per
 * sentence, the volume put back after "louder") are in [EngineSpeaker]; this only adapts the
 * platform engine to them.
 */
class AndroidSpeaker(context: Context) : Speaker by EngineSpeaker(
    AndroidSpeechEngine(context), AndroidVolume(context.getSystemService(AudioManager::class.java)), Dispatchers.Main,
)

private class AndroidSpeechEngine(private val context: Context) : SpeechEngine {
    private var tts: TextToSpeech? = null
    private val waiting = mutableMapOf<String, (String) -> Unit>()

    override fun init(onReady: (Boolean) -> Unit) {
        tts = TextToSpeech(context) { status -> configure(status, onReady) }
    }

    private fun configure(status: Int, onReady: (Boolean) -> Unit) {
        val engine = tts
        if (status != TextToSpeech.SUCCESS || engine == null) {
            onReady(false)
        } else {
            engine.setAudioAttributes(
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
            )
            engine.language = Locale.UK
            // One listener for the engine's life: each utterance finds its own waiter by id.
            engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {}
                override fun onDone(utteranceId: String?) = finished(utteranceId)
                @Deprecated("Deprecated in Java")
                override fun onError(utteranceId: String?) = finished(utteranceId)
                override fun onError(utteranceId: String?, errorCode: Int) = finished(utteranceId)
                override fun onStop(utteranceId: String?, interrupted: Boolean) = finished(utteranceId)
            })
            onReady(true)
        }
    }

    private fun finished(id: String?) {
        if (id == null) return
        synchronized(waiting) { waiting.remove(id) }?.invoke(id)
    }

    override fun setRate(rate: Float) { tts?.setSpeechRate(rate) }

    override fun speak(text: String, id: String, volume: Float, onDone: (String) -> Unit): Boolean {
        val engine = tts ?: return false
        synchronized(waiting) { waiting[id] = onDone }
        val params = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, volume) }
        val ok = engine.speak(text, TextToSpeech.QUEUE_FLUSH, params, id) == TextToSpeech.SUCCESS
        if (!ok) synchronized(waiting) { waiting.remove(id) }
        return ok
    }

    override fun stop() { tts?.stop() }

    override fun shutdown() {
        tts?.shutdown()
        tts = null
        synchronized(waiting) { waiting.clear() }
    }
}

private class AndroidVolume(private val audio: AudioManager) : VolumeControl {
    override fun current(): Int = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
    override fun max(): Int = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
    override fun set(level: Int) = audio.setStreamVolume(AudioManager.STREAM_MUSIC, level, 0)
}
