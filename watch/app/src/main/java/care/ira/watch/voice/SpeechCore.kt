package care.ira.watch.voice

import kotlin.coroutines.resume
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/*
 * The rules for listening and speaking, with no Android in them, so they are tested on the JVM.
 * The Android adapters (AndroidListener, AndroidSpeaker) only translate to and from the platform.
 *
 * Listening:
 *   - every recogniser call happens on `main` (Android requires its main thread);
 *   - one recogniser at a time, created for one answer and destroyed after it, always, also on
 *     timeout and cancellation;
 *   - a result that arrives after the answer was given up on is ignored;
 *   - no recogniser on this watch: null at once ("not heard"), never a crash.
 *
 * Speaking:
 *   - waits for the engine to be ready, for at most READY_TIMEOUT_MS, then gives up silently;
 *   - one utterance at a time, each with its own id, so a finished sentence never releases the
 *     wrong waiter;
 *   - every utterance has a time limit from its length, so a stuck engine cannot hold up the
 *     caller (and the caller may be on the way to sending help);
 *   - "louder" raises the volume for that utterance only and puts it back afterwards.
 */

/** One platform speech recogniser, used for a single answer. */
interface Recognizer {
    fun start(onResult: (String?) -> Unit, onError: (Int) -> Unit)
    fun cancel()
    fun destroy()
}

interface RecognizerFactory {
    fun available(): Boolean
    fun create(): Recognizer
}

class RecognizerListener(
    private val factory: RecognizerFactory,
    private val main: CoroutineDispatcher,
) : Listener {
    private val one = Mutex()

    override suspend fun listen(timeoutMs: Long): String? = one.withLock {
        withContext(main) {
            if (!factory.available()) return@withContext null
            val rec = factory.create()
            try {
                withTimeoutOrNull(timeoutMs) {
                    suspendCancellableCoroutine<String?> { cont ->
                        rec.start(
                            onResult = { text -> if (cont.isActive) cont.resume(text?.takeIf { it.isNotBlank() }) },
                            onError = { if (cont.isActive) cont.resume(null) },
                        )
                    }
                }
            } finally {
                rec.cancel()
                rec.destroy()
            }
        }
    }
}

/** One platform text-to-speech engine. `speak` returns false when the engine refused at once. */
interface SpeechEngine {
    /** Starts the engine; `onReady(true)` once it can speak, `onReady(false)` if it never will. */
    fun init(onReady: (Boolean) -> Unit)
    fun setRate(rate: Float)
    fun speak(text: String, id: String, volume: Float, onDone: (String) -> Unit): Boolean
    fun stop()
    fun shutdown()
}

/** The stream volume, so "louder" can be undone. */
interface VolumeControl {
    fun current(): Int
    fun max(): Int
    fun set(level: Int)
}

class EngineSpeaker(
    private val engine: SpeechEngine,
    private val volume: VolumeControl?,
    private val main: CoroutineDispatcher,
    private val newId: () -> String = { java.util.UUID.randomUUID().toString() },
) : Speaker {
    private val one = Mutex()
    private var state = 0 // 0 not started, 1 starting, 2 ready, 3 failed
    private val readyWaiters = mutableListOf<(Boolean) -> Unit>()
    private var rate = 0.9f

    companion object {
        const val READY_TIMEOUT_MS = 4_000L
        /** A sentence may take this long plus PER_CHAR_MS per character, and never more than MAX_UTTERANCE_MS. */
        const val BASE_UTTERANCE_MS = 3_000L
        const val PER_CHAR_MS = 90L
        const val MAX_UTTERANCE_MS = 30_000L
        fun limitFor(text: String): Long = (BASE_UTTERANCE_MS + PER_CHAR_MS * text.length).coerceAtMost(MAX_UTTERANCE_MS)
    }

    override fun setRate(rate: Float) {
        this.rate = rate.coerceIn(0.6f, 1.2f)
        if (state == 2) engine.setRate(this.rate)
    }

    private suspend fun ready(): Boolean = withContext(main) {
        when (state) {
            2 -> true
            3 -> false
            else -> withTimeoutOrNull(READY_TIMEOUT_MS) {
                suspendCancellableCoroutine<Boolean> { cont ->
                    readyWaiters += { ok -> if (cont.isActive) cont.resume(ok) }
                    if (state == 0) {
                        state = 1
                        engine.init { ok ->
                            state = if (ok) 2 else 3
                            if (ok) engine.setRate(rate)
                            val waiting = readyWaiters.toList()
                            readyWaiters.clear()
                            waiting.forEach { it(ok) }
                        }
                    }
                }
            } ?: false
        }
    }

    override suspend fun speak(text: String, louder: Boolean) {
        if (text.isBlank()) return
        one.withLock {
            if (!ready()) return@withLock
            withContext(main) {
                val before = if (louder) volume?.current() else null
                try {
                    if (louder) volume?.let { it.set(it.max()) }
                    val id = newId()
                    withTimeoutOrNull(limitFor(text)) {
                        suspendCancellableCoroutine<Unit> { cont ->
                            val accepted = engine.speak(text, id, if (louder) 1.0f else 0.8f) { finished ->
                                if (finished == id && cont.isActive) cont.resume(Unit)
                            }
                            if (!accepted && cont.isActive) cont.resume(Unit)
                        }
                    } ?: engine.stop() // over time: stop it, never wait on a stuck engine
                } finally {
                    if (before != null) volume?.set(before)
                }
            }
        }
    }

    override fun shutdown() {
        engine.shutdown()
        state = 0
    }
}
