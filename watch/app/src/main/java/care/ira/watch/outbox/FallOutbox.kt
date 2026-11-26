package care.ira.watch.outbox

import care.ira.watch.api.CareApi
import care.ira.watch.api.FallOk
import care.ira.watch.api.FallStart
import care.ira.watch.api.HelpRequest
import care.ira.watch.api.LocationFix
import care.ira.watch.checkin.FallCountdown
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * A fall countdown's events, written to the outbox under keys derived from the incident's id
 * (D-092): `fall-<id>-d<n>` for its n-th detection, `fall-<id>-ok` and `fall-<id>-help`. The same
 * event queued twice is held once, a retry re-sends the same key, and the server answers a key
 * it has seen with its first answer, so a flaky link or a double tap never opens a second alert.
 * The outbox sends in order, so an incident's "I'm OK" always reaches the server before the start
 * of the next incident. Pure Kotlin: tested without a watch.
 */
object FallOutbox {
    private val json = Json { explicitNulls = false }

    fun startedKey(incidentId: String, detection: Int) = "fall-$incidentId-d$detection"
    fun okKey(incidentId: String) = "fall-$incidentId-ok"
    fun helpKey(incidentId: String) = "fall-$incidentId-help"

    fun started(outbox: Outbox, a: FallCountdown.Action.ReportFallStarted): UrgentEvent =
        outbox.enqueue(Outbox.KIND_FALL_STARTED, CareApi.PATH_FALL,
            json.encodeToString(FallStart(a.peakG, a.stillSeconds, a.incidentId, a.detection)),
            startedKey(a.incidentId, a.detection))

    fun ok(outbox: Outbox, a: FallCountdown.Action.ReportFallOk): UrgentEvent =
        outbox.enqueue(Outbox.KIND_FALL_OK, CareApi.PATH_FALL_OK, json.encodeToString(FallOk(a.incidentId)), okKey(a.incidentId))

    /** Help for a fall incident: the countdown ended or Help was pressed during it. */
    fun help(outbox: Outbox, incidentId: String, via: String, location: LocationFix?): UrgentEvent =
        outbox.enqueue(Outbox.KIND_HELP, CareApi.PATH_HELP,
            json.encodeToString(HelpRequest(via = via, location = location, incidentId = incidentId)), helpKey(incidentId))
}
