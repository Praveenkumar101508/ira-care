package care.ira.watch.checkin

import care.ira.watch.api.DueCheckin

/**
 * A reminder answered on the wrist (D1.h). The server asks a due reminder as a check-in and
 * says which one it is (`reminder_id`, care/personal.py); the watch then shows these buttons
 * instead of "I'm OK" and sends the one pressed as `choice` on the answer.
 *
 * A button is clear, but a dose still takes two: "Done" on a medication makes the server ask
 * once more before anything is written down (hazard H14). "Not today" records a skipped dose,
 * said plainly; "Later" asks again in a while. Pure Kotlin: tested on the JVM.
 */
enum class ReminderChoice(val wire: String, val label: String, val description: String) {
    DONE("done", "Done", "Done. Says you have done it."),
    LATER("later", "Later", "Later. Reminds you again in a while."),
    SKIP("skip", "Not today", "Not today. Says you will not do it today."),
}

object ReminderButtons {
    /** The due check-in's reminder kind, or null when it is not a reminder (no `reminder_id`). */
    fun reminderKindOf(due: DueCheckin): String? = if (due.reminderId != null) due.questionKind else null

    /** The buttons for a reminder of this kind; none when the question is not a reminder. */
    fun choices(reminderKind: String?): List<ReminderChoice> = when (reminderKind) {
        null -> emptyList()
        "medication", "hydration" -> listOf(ReminderChoice.DONE, ReminderChoice.LATER, ReminderChoice.SKIP)
        else -> listOf(ReminderChoice.DONE, ReminderChoice.LATER)
    }

    /**
     * What the screen shows. A fall countdown or a live SOS outranks a reminder, as on the face
     * itself (FaceText): its buttons give way to "I'm OK" and Help, and come back afterwards.
     */
    fun visible(reminderKind: String?, fallCountdown: Int?, sosActive: Boolean): List<ReminderChoice> =
        if (fallCountdown != null || sosActive) emptyList() else choices(reminderKind)
}
