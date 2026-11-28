package care.ira.watch.ui

/**
 * The watch face's main line, most pressing first. Pure Kotlin, tested on the JVM.
 *
 * "All well" is a promise that someone would hear about trouble. With no connection to the
 * server the watch cannot keep it, so it says so instead: Help still works (it is saved and sent
 * when the connection is back), and nothing reassures the person that the circle is watching.
 */
object FaceText {
    const val ALL_WELL = "All well"
    const val NO_CONNECTION = "No connection.\nHelp is saved and sent when it is back."

    fun main(fallCountdown: Int?, sosText: String?, question: String?, lastAnswer: String?, visitText: String?, online: Boolean): String = when {
        fallCountdown != null -> "Did you fall? Are you OK?\nHelp in $fallCountdown s"
        sosText != null -> sosText
        question != null -> question
        // The person must know the watch cannot reach anyone: never hidden behind a thank-you or a visit.
        !online -> NO_CONNECTION
        lastAnswer != null -> "Thank you."
        visitText != null -> visitText
        else -> ALL_WELL
    }
}
