package care.ira.watch.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.UUID
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

/** Something that can say a sentence aloud. The real one wraps Android's TextToSpeech. */
interface Speaker {
    suspend fun speak(text: String, louder: Boolean)
    /** The person's pace (care/personal.py): 0.8 slow, 0.9 normal. */
    fun setRate(rate: Float) {}
    fun shutdown() {}
}

class AndroidSpeaker(private val context: Context) : Speaker {
    private var tts: TextToSpeech? = null
    private var ready = false
    private var rate = 0.9f

    override fun setRate(rate: Float) {
        this.rate = rate.coerceIn(0.6f, 1.2f)
        tts?.setSpeechRate(this.rate)
    }

    private fun engine(): TextToSpeech {
        tts?.let { return it }
        val created = TextToSpeech(context) { status -> ready = status == TextToSpeech.SUCCESS }
        created.setAudioAttributes(
            AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
        )
        created.language = Locale.UK
        created.setSpeechRate(rate)   // the person's pace; a little slower than default by default
        tts = created
        return created
    }

    override suspend fun speak(text: String, louder: Boolean) {
        val engine = engine()
        if (louder) {
            val audio = context.getSystemService(AudioManager::class.java)
            audio.setStreamVolume(AudioManager.STREAM_MUSIC, audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC), 0)
        }
        suspendCancellableCoroutine { cont ->
            val id = UUID.randomUUID().toString()
            engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {}
                override fun onDone(utteranceId: String?) { if (utteranceId == id && cont.isActive) cont.resume(Unit) }
                @Deprecated("Deprecated in Java")
                override fun onError(utteranceId: String?) { if (utteranceId == id && cont.isActive) cont.resume(Unit) }
            })
            val params = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, if (louder) 1.0f else 0.8f) }
            if (engine.speak(text, TextToSpeech.QUEUE_FLUSH, params, id) != TextToSpeech.SUCCESS && cont.isActive) cont.resume(Unit)
        }
    }

    override fun shutdown() {
        tts?.shutdown()
        tts = null
    }
}
