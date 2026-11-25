package care.ira.watch.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import kotlinx.coroutines.Dispatchers

/** Something that listens for a short spoken answer. Returns null when nothing was heard. */
interface Listener {
    suspend fun listen(timeoutMs: Long): String?
    fun shutdown() {}
}

/**
 * Android's own speech recogniser. Audio is never stored: only the text comes back. The rules
 * (main thread only, one at a time, always destroyed, late results ignored) are in
 * [RecognizerListener]; this only adapts the platform's recogniser to them.
 */
class AndroidListener(context: Context) : Listener by RecognizerListener(AndroidRecognizerFactory(context), Dispatchers.Main)

class AndroidRecognizerFactory(private val context: Context) : RecognizerFactory {
    override fun available(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    override fun create(): Recognizer = AndroidRecognizer(SpeechRecognizer.createSpeechRecognizer(context))
}

private class AndroidRecognizer(private val rec: SpeechRecognizer) : Recognizer {
    override fun start(onResult: (String?) -> Unit, onError: (Int) -> Unit) {
        // Named apart from the listener's own methods: inside the object, `onError(error)` would
        // call the object's onError again, forever.
        val deliverResult = onResult
        val deliverError = onError
        rec.setRecognitionListener(object : RecognitionListener {
            override fun onResults(results: Bundle?) {
                deliverResult(results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull())
            }
            override fun onError(error: Int) = deliverError(error)
            override fun onReadyForSpeech(params: Bundle?) {}
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onPartialResults(partialResults: Bundle?) {}
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
        rec.startListening(intent)
    }

    override fun cancel() = rec.cancel()
    override fun destroy() = rec.destroy()
}
