package care.ira.watch.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull

/** Something that listens for a short spoken answer. Returns null when nothing was heard. */
interface Listener {
    suspend fun listen(timeoutMs: Long): String?
    fun shutdown() {}
}

/** Android's own speech recogniser. Audio is never stored: only the text comes back. */
class AndroidListener(private val context: Context) : Listener {
    private var recognizer: SpeechRecognizer? = null

    override suspend fun listen(timeoutMs: Long): String? {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) return null
        return withTimeoutOrNull(timeoutMs) {
            suspendCancellableCoroutine { cont ->
                val rec = SpeechRecognizer.createSpeechRecognizer(context).also { recognizer = it }
                rec.setRecognitionListener(object : RecognitionListener {
                    override fun onResults(results: Bundle?) {
                        val best = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
                        if (cont.isActive) cont.resume(best)
                    }
                    override fun onError(error: Int) { if (cont.isActive) cont.resume(null) }
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
                cont.invokeOnCancellation { rec.cancel() }
            }
        }
    }

    override fun shutdown() {
        recognizer?.destroy()
        recognizer = null
    }
}
