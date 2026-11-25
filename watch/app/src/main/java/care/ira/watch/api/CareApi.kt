package care.ira.watch.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/** One reading as the backend's `/care/devices/me/report` takes it. */
@Serializable
data class Reading(
    val kind: String,
    val value: Double,
    @SerialName("recorded_at") val recordedAt: String,
    val unit: String = "",
)

@Serializable
data class DeviceReport(
    val readings: List<Reading> = emptyList(),
    @SerialName("battery_percent") val batteryPercent: Int? = null,
    @SerialName("on_wrist") val onWrist: Boolean? = null,
    @SerialName("background_sensor_permission") val backgroundSensorPermission: Boolean? = null,
    @SerialName("app_version") val appVersion: String? = null,
)

@Serializable
data class ReportAck(val accepted: Int, val stored: Int)

@Serializable
data class PairRequest(
    @SerialName("pairing_code") val pairingCode: String,
    @SerialName("app_version") val appVersion: String = "",
)

@Serializable
data class PairedDevice(
    val id: String,
    @SerialName("person_id") val personId: String,
    val status: String,
    @SerialName("device_token") val deviceToken: String,
)

@Serializable
data class DueCheckin(
    val id: String,
    val status: String,
    @SerialName("question_kind") val questionKind: String,
    @SerialName("question_text") val questionText: String,
    /** A safety question ("Are you OK?"): vibrate first, speak at once, shorter wait. */
    val safety: Boolean = false,
)

@Serializable
data class CheckinAnswer(val text: String = "", val via: String = "voice")

/** Where the watch was when the SOS started. Sent once with the request, never streamed. */
@Serializable
data class LocationFix(
    val latitude: Double,
    val longitude: Double,
    @SerialName("accuracy_m") val accuracyM: Double? = null,
    @SerialName("recorded_at") val recordedAt: String? = null,
)

@Serializable
data class HelpRequest(val via: String = "button", val note: String = "", val location: LocationFix? = null)

/** The carer's fall settings, as `GET /devices/me` returns them. Thresholds, not medical claims. */
@Serializable
data class FallSettingsDto(
    @SerialName("fall_detection_enabled") val enabled: Boolean = true,
    @SerialName("fall_countdown_s") val countdownS: Int = 30,
    @SerialName("fall_impact_g") val impactG: Double = 2.5,
    @SerialName("fall_still_seconds") val stillSeconds: Int = 20,
    @SerialName("fall_still_threshold_g") val stillThresholdG: Double = 0.3,
)

/** The carer's heart-rate range and how often ordinary readings go up, as `GET /devices/me` returns them. */
@Serializable
data class ReadingSettingsDto(
    @SerialName("heart_rate_min") val heartRateMin: Int? = null,
    @SerialName("heart_rate_max") val heartRateMax: Int? = null,
    @SerialName("reading_upload_interval_s") val uploadIntervalS: Int = 60,
) {
    fun outOfRange(bpm: Double): Boolean =
        (heartRateMin != null && bpm < heartRateMin) || (heartRateMax != null && bpm > heartRateMax)
}

/** The true state of the person's SOS, from the server. */
@Serializable
data class SosStateDto(
    val state: String = "idle",
    @SerialName("alert_id") val alertId: String? = null,
    @SerialName("responder_name") val responderName: String? = null,
    val kind: String? = null,
    @SerialName("updated_at") val updatedAt: String? = null,
    /** Carers' visits, from the server (109): none, expected, late, or here. */
    val visit: VisitStateDto? = null,
)

@Serializable
data class VisitStateDto(
    val state: String = "none",
    @SerialName("carer_name") val carerName: String? = null,
    @SerialName("visit_id") val visitId: String? = null,
    @SerialName("planned_start") val plannedStart: String? = null,
    val since: String? = null,
)

@Serializable
data class DeviceMe(
    val id: String,
    val status: String,
    val fall: FallSettingsDto = FallSettingsDto(),
    val readings: ReadingSettingsDto = ReadingSettingsDto(),
    val sos: SosStateDto = SosStateDto(),
    val profile: ProfileDto = ProfileDto(),
)

/** How IRA speaks to this person (110): set by the person or their representative, applied here. */
@Serializable
data class ProfileDto(
    val pace: String = "normal",
    @SerialName("prompting_level") val promptingLevel: String = "standard",
    @SerialName("speech_rate") val speechRate: Float = 0.9f,
    @SerialName("repeat_after_s") val repeatAfterS: Int = 60,
    val repeats: Int = 1,
    val voice: Boolean = true,
    val touch: Boolean = true,
    val vibration: Boolean = true,
)

@Serializable
data class TalkRequest(val text: String)

/** What the server said back, sentence by sentence, and what to do at the end. */
data class TalkOutcome(val sentences: List<String>, val action: JsonObject?, val needsHelp: Boolean)

@Serializable
data class FallStart(@SerialName("peak_g") val peakG: Double, @SerialName("still_seconds") val stillSeconds: Int)

/** What went wrong talking to the backend, in a way the caller can act on. */
sealed class ApiOutcome<out T> {
    data class Ok<T>(val value: T) : ApiOutcome<T>()
    /** No network, timeout: keep the data and try later. */
    data class Offline(val reason: String) : ApiOutcome<Nothing>()
    /** The token is no longer accepted: the watch must be paired again. */
    object Unauthorised : ApiOutcome<Nothing>()
    /** The server answered with something other than success or 401. */
    data class Failed(val status: Int, val body: String) : ApiOutcome<Nothing>()
}

/**
 * The watch's side of the care API. Every call is synchronous and meant to be
 * run off the main thread (the foreground service and the sync worker do).
 */
class CareApi(
    private val baseUrl: String,
    private val tokenProvider: () -> String?,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build(),
) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val mediaType = "application/json; charset=utf-8".toMediaType()
    /** The SOS stream is held open; it must not be cut by the ordinary read timeout. */
    private val streamClient: OkHttpClient = client.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).build()

    fun pair(code: String, appVersion: String): ApiOutcome<PairedDevice> =
        post("/api/v1/care/devices/pair", json.encodeToString(PairRequest(code, appVersion)), auth = false)
            .map { json.decodeFromString<PairedDevice>(it) }

    fun report(report: DeviceReport): ApiOutcome<ReportAck> =
        post("/api/v1/care/devices/me/report", json.encodeToString(report))
            .map { json.decodeFromString<ReportAck>(it) }

    /** The watch's own record and the carer's fall settings; asked after each sync. */
    fun me(): ApiOutcome<DeviceMe> =
        get("/api/v1/care/devices/me").map { json.decodeFromString<DeviceMe>(it) }

    /** The fall countdown has started: the server opens an asking alert and keeps its own deadline. */
    fun fallStarted(start: FallStart): ApiOutcome<JsonObject> =
        post("/api/v1/care/devices/me/fall", json.encodeToString(start)).map { json.parseToJsonElement(it).jsonObject }

    /** The wearer confirmed unpairing: the server ends this token and tells the care circle. */
    fun unpair(): ApiOutcome<JsonObject> =
        post("/api/v1/care/devices/me/unpair", "{}").map { json.parseToJsonElement(it).jsonObject }

    /** The person tapped I'm OK during the countdown. */
    fun fallOk(): ApiOutcome<JsonObject> =
        post("/api/v1/care/devices/me/fall/ok", "{}").map { json.parseToJsonElement(it).jsonObject }

    /**
     * Talk to IRA. The server streams one JSON object per line; `onSentence` is
     * called for each sentence as it arrives so the watch can speak it at once.
     */
    fun talk(text: String, onSentence: (String) -> Unit): ApiOutcome<TalkOutcome> {
        val token = tokenProvider() ?: return ApiOutcome.Unauthorised
        val request = Request.Builder().url("$baseUrl/api/v1/care/devices/me/talk")
            .header(TOKEN_HEADER, token).post(json.encodeToString(TalkRequest(text)).toRequestBody(mediaType)).build()
        return try {
            client.newCall(request).execute().use { response ->
                if (response.code == 401) return ApiOutcome.Unauthorised
                if (!response.isSuccessful) return ApiOutcome.Failed(response.code, response.body?.string()?.take(300) ?: "")
                val sentences = mutableListOf<String>()
                var action: JsonObject? = null
                var needsHelp = false
                val source = response.body?.source() ?: return ApiOutcome.Failed(response.code, "empty")
                while (true) {
                    val line = source.readUtf8Line() ?: break
                    if (line.isBlank()) continue
                    val obj = json.parseToJsonElement(line).jsonObject
                    obj["sentence"]?.let { el ->
                        val s = (el as? JsonPrimitive)?.content ?: return@let
                        sentences += s; onSentence(s)
                    }
                    if ((obj["done"] as? JsonPrimitive)?.content == "true") {
                        action = obj["action"] as? JsonObject
                        needsHelp = (obj["needs_help"] as? JsonPrimitive)?.content == "true"
                    }
                }
                ApiOutcome.Ok(TalkOutcome(sentences, action, needsHelp))
            }
        } catch (e: IOException) {
            ApiOutcome.Offline(e.javaClass.simpleName)
        }
    }

    fun help(request: HelpRequest, idempotencyKey: String? = null): ApiOutcome<JsonObject> =
        post("/api/v1/care/devices/me/help", json.encodeToString(request), idempotencyKey = idempotencyKey)
            .map { json.parseToJsonElement(it).jsonObject }

    /**
     * An urgent event from the outbox: the path and body exactly as queued, with
     * the event's idempotency key, so a re-send is recognised by the server.
     */
    fun sendUrgent(path: String, body: String, idempotencyKey: String): ApiOutcome<String> =
        post(path, body, idempotencyKey = idempotencyKey)

    /** The state the wrist must show for the current SOS, asked once. */
    fun sosState(): ApiOutcome<SosStateDto> =
        get("/api/v1/care/devices/me/sos").map { json.decodeFromString<SosStateDto>(it) }

    /**
     * The same state, pushed by the server: one JSON line now and one on every
     * change, the connection held open. `onState` is called for each. Returns
     * when the server closes the stream (the caller reconnects) or the link drops.
     */
    fun sosStream(onState: (SosStateDto) -> Unit): ApiOutcome<Unit> {
        val token = tokenProvider() ?: return ApiOutcome.Unauthorised
        val request = Request.Builder().url("$baseUrl/api/v1/care/devices/me/sos/stream").header(TOKEN_HEADER, token).get().build()
        return try {
            streamClient.newCall(request).execute().use { response ->
                if (response.code == 401) return ApiOutcome.Unauthorised
                if (!response.isSuccessful) return ApiOutcome.Failed(response.code, response.body?.string()?.take(300) ?: "")
                val source = response.body?.source() ?: return ApiOutcome.Failed(response.code, "empty")
                while (true) {
                    val line = source.readUtf8Line() ?: break
                    if (line.isBlank()) continue
                    val obj = json.parseToJsonElement(line).jsonObject
                    if (obj.containsKey("state")) onState(json.decodeFromJsonElement(SosStateDto.serializer(), obj))
                }
                ApiOutcome.Ok(Unit)
            }
        } catch (e: IOException) {
            ApiOutcome.Offline(e.javaClass.simpleName)
        }
    }

    /** The due check-in, or null when there is none (or it is quiet hours). */
    fun dueCheckin(): ApiOutcome<DueCheckin?> =
        get("/api/v1/care/devices/me/checkins/due").map { body ->
            val element: JsonElement = json.parseToJsonElement(body).jsonObject["checkin"] ?: JsonNull
            if (element is JsonNull) null else json.decodeFromJsonElement(DueCheckin.serializer(), element)
        }

    fun repeated(checkinId: String): ApiOutcome<Unit> =
        post("/api/v1/care/devices/me/checkins/$checkinId/repeated", "{}").map { }

    fun answer(checkinId: String, answer: CheckinAnswer): ApiOutcome<Unit> =
        post("/api/v1/care/devices/me/checkins/$checkinId/answer", json.encodeToString(answer)).map { }

    /** The wrist showed "[Name] is here" (109): the person saw it. */
    fun acknowledgeVisit(visitId: String): ApiOutcome<Unit> =
        post("/api/v1/care/devices/me/visits/$visitId/acknowledge", "{}").map { }

    fun missed(checkinId: String): ApiOutcome<Unit> =
        post("/api/v1/care/devices/me/checkins/$checkinId/missed", "{}").map { }

    private fun get(path: String): ApiOutcome<String> = execute(Request.Builder().url(baseUrl + path).get(), auth = true)

    private fun post(path: String, body: String, auth: Boolean = true, idempotencyKey: String? = null): ApiOutcome<String> {
        val builder = Request.Builder().url(baseUrl + path).post(body.toRequestBody(mediaType))
        if (idempotencyKey != null) builder.header(IDEMPOTENCY_HEADER, idempotencyKey)
        return execute(builder, auth)
    }

    private fun execute(builder: Request.Builder, auth: Boolean): ApiOutcome<String> {
        if (auth) {
            val token = tokenProvider() ?: return ApiOutcome.Unauthorised
            builder.header(TOKEN_HEADER, token)
        }
        return try {
            client.newCall(builder.build()).execute().use { response ->
                val text = response.body?.string() ?: ""
                when {
                    response.isSuccessful -> ApiOutcome.Ok(text)
                    response.code == 401 -> ApiOutcome.Unauthorised
                    else -> ApiOutcome.Failed(response.code, text.take(300))
                }
            }
        } catch (e: IOException) {
            ApiOutcome.Offline(e.javaClass.simpleName)
        }
    }

    companion object {
        const val TOKEN_HEADER = "X-Device-Token"
        const val IDEMPOTENCY_HEADER = "X-Idempotency-Key"
        const val PATH_HELP = "/api/v1/care/devices/me/help"
        const val PATH_FALL = "/api/v1/care/devices/me/fall"
        const val PATH_FALL_OK = "/api/v1/care/devices/me/fall/ok"
        fun pathAnswer(checkinId: String) = "/api/v1/care/devices/me/checkins/$checkinId/answer"
        fun pathMissed(checkinId: String) = "/api/v1/care/devices/me/checkins/$checkinId/missed"
    }
}

inline fun <T, R> ApiOutcome<T>.map(transform: (T) -> R): ApiOutcome<R> = when (this) {
    is ApiOutcome.Ok -> ApiOutcome.Ok(transform(value))
    is ApiOutcome.Offline -> this
    is ApiOutcome.Unauthorised -> this
    is ApiOutcome.Failed -> this
}
