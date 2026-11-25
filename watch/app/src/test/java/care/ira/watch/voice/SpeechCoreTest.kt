package care.ira.watch.voice

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class SpeechCoreTest {

    /** A recogniser that records which thread-context touched it and answers when told. */
    private class FakeRecognizer(val log: MutableList<String>) : Recognizer {
        var onResult: ((String?) -> Unit)? = null
        var onError: ((Int) -> Unit)? = null
        override fun start(onResult: (String?) -> Unit, onError: (Int) -> Unit) {
            log += "start on ${Thread.currentThread().name.substringBefore(" @")}"; this.onResult = onResult; this.onError = onError
        }
        override fun cancel() { log += "cancel" }
        override fun destroy() { log += "destroy" }
    }

    private class FakeFactory(val available: Boolean = true) : RecognizerFactory {
        val log = mutableListOf<String>()
        val made = mutableListOf<FakeRecognizer>()
        override fun available() = available
        override fun create(): Recognizer = FakeRecognizer(log).also { made += it }
    }

    @Test
    fun `an answer comes back and the recogniser is always destroyed`() = runTest {
        val f = FakeFactory()
        val l = RecognizerListener(f, StandardTestDispatcher(testScheduler))
        val heard = async { l.listen(10_000) }
        runCurrent()
        f.made.single().onResult!!("I'm fine")
        assertEquals("I'm fine", heard.await())
        assertEquals(listOf("cancel", "destroy"), f.log.filter { it != f.log.first() })
    }

    @Test
    fun `a timeout gives up, destroys the recogniser, and a late result is ignored`() = runTest {
        val f = FakeFactory()
        val l = RecognizerListener(f, StandardTestDispatcher(testScheduler))
        val heard = async { l.listen(5_000) }
        advanceTimeBy(6_000)
        advanceUntilIdle()
        assertNull(heard.await())
        assertTrue("destroyed on timeout", "destroy" in f.log)
        f.made.single().onResult!!("too late") // must not throw or resume anything
    }

    @Test
    fun `cancelling the listen destroys the recogniser`() = runTest {
        val f = FakeFactory()
        val l = RecognizerListener(f, StandardTestDispatcher(testScheduler))
        val job = launch { l.listen(60_000) }
        runCurrent()
        job.cancel()
        advanceUntilIdle()
        assertTrue("destroy" in f.log)
    }

    @Test
    fun `two listens never run two recognisers at once`() = runTest {
        val f = FakeFactory()
        val l = RecognizerListener(f, StandardTestDispatcher(testScheduler))
        val a = async { l.listen(10_000) }
        val b = async { l.listen(10_000) }
        runCurrent()
        assertEquals("the second waits for the first", 1, f.made.size)
        f.made[0].onResult!!("one")
        runCurrent()
        assertEquals(2, f.made.size)
        f.made[1].onResult!!("two")
        assertEquals("one", a.await()); assertEquals("two", b.await())
    }

    @Test
    fun `no recogniser on this watch means nothing heard, not a crash`() = runTest {
        assertNull(RecognizerListener(FakeFactory(available = false), StandardTestDispatcher(testScheduler)).listen(1_000))
    }

    @Test
    fun `an error and an empty transcript are both nothing heard`() = runTest {
        val f = FakeFactory()
        val l = RecognizerListener(f, StandardTestDispatcher(testScheduler))
        val e = async { l.listen(10_000) }
        runCurrent(); f.made[0].onError!!(7)
        assertNull(e.await())
        val blank = async { l.listen(10_000) }
        runCurrent(); f.made[1].onResult!!("   ")
        assertNull(blank.await())
    }

    // ── Speaking ─────────────────────────────────────────────────────────────

    private class FakeEngine(var answersReady: Boolean? = true, var finishes: Boolean = true) : SpeechEngine {
        val spoken = mutableListOf<String>()
        var readyCallback: ((Boolean) -> Unit)? = null
        var stops = 0
        val pending = mutableListOf<Pair<String, (String) -> Unit>>()
        override fun init(onReady: (Boolean) -> Unit) { readyCallback = onReady; answersReady?.let { onReady(it) } }
        override fun setRate(rate: Float) {}
        override fun speak(text: String, id: String, volume: Float, onDone: (String) -> Unit): Boolean {
            spoken += text
            if (finishes) onDone(id) else pending += id to onDone
            return true
        }
        override fun stop() { stops++ }
        override fun shutdown() {}
    }

    private class FakeVolume(var level: Int = 4) : VolumeControl {
        val history = mutableListOf<Int>()
        override fun current() = level
        override fun max() = 10
        override fun set(level: Int) { this.level = level; history += level }
    }

    private fun TestScope.speaker(e: SpeechEngine, v: VolumeControl? = null) = EngineSpeaker(e, v, StandardTestDispatcher(testScheduler))

    @Test
    fun `louder raises the volume for that sentence only and puts it back`() = runTest {
        val v = FakeVolume(level = 4)
        speaker(FakeEngine(), v).speak("Sending your request for help.", louder = true)
        assertEquals(listOf(10, 4), v.history)
        assertEquals(4, v.level)
    }

    @Test
    fun `a stuck engine cannot hold the caller, the sentence has a time limit and is stopped`() = runTest {
        val e = FakeEngine(finishes = false)
        val v = FakeVolume(level = 3)
        val s = speaker(e, v)
        val done = async { s.speak("Stay where you are.", louder = true) }
        advanceTimeBy(EngineSpeaker.limitFor("Stay where you are.") + 100)
        advanceUntilIdle()
        assertTrue(done.isCompleted)
        assertEquals(1, e.stops)
        assertEquals("the volume is put back even then", 3, v.level)
    }

    @Test
    fun `an engine that never becomes ready is given up on, silently and in time`() = runTest {
        val e = FakeEngine(answersReady = null)
        val s = speaker(e)
        val done = async { s.speak("Hello.", louder = false) }
        advanceTimeBy(EngineSpeaker.READY_TIMEOUT_MS + 100)
        advanceUntilIdle()
        assertTrue(done.isCompleted)
        assertTrue(e.spoken.isEmpty())
    }

    @Test
    fun `the first sentence waits for the engine to be ready instead of being lost`() = runTest {
        val e = FakeEngine(answersReady = null)
        val s = speaker(e)
        val done = async { s.speak("Good morning.", louder = false) }
        advanceTimeBy(500)
        e.readyCallback!!(true)
        advanceUntilIdle()
        assertTrue(done.isCompleted)
        assertEquals(listOf("Good morning."), e.spoken)
    }

    @Test
    fun `sentences never overlap, and each waits for its own end`() = runTest {
        val e = FakeEngine(finishes = false)
        val s = speaker(e)
        val first = async { s.speak("One.", louder = false) }
        val second = async { s.speak("Two.", louder = false) }
        advanceTimeBy(100)
        assertEquals("the second waits", listOf("One."), e.spoken)
        e.pending.removeAt(0).let { (id, done) -> done(id) }
        advanceTimeBy(100)
        assertTrue(first.isCompleted)
        assertEquals(listOf("One.", "Two."), e.spoken)
        e.pending.removeAt(0).let { (id, done) -> done(id) }
        advanceUntilIdle()
        assertTrue(second.isCompleted)
    }
}
