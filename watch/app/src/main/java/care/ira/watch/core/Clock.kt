package care.ira.watch.core

/** A clock the app reads time from, so tests can move it. */
fun interface Clock {
    fun nowMillis(): Long

    companion object {
        val SYSTEM: Clock = Clock { System.currentTimeMillis() }
    }
}
