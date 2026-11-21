package care.ira.watch.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
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

@Serializable
data class HelpRequest(val via: String = "button", val note: String = "")

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

    fun pair(code: String, appVersion: String): ApiOutcome<PairedDevice> =
        post("/api/v1/care/devices/pair", json.encodeToString(PairRequest(code, appVersion)), auth = false)
            .map { json.decodeFromString<PairedDevice>(it) }

    fun report(report: DeviceReport): ApiOutcome<ReportAck> =
        post("/api/v1/care/devices/me/report", json.encodeToString(report))
            .map { json.decodeFromString<ReportAck>(it) }

    fun help(request: HelpRequest): ApiOutcome<JsonObject> =
        post("/api/v1/care/devices/me/help", json.encodeToString(request)).map { json.parseToJsonElement(it).jsonObject }

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

    fun missed(checkinId: String): ApiOutcome<Unit> =
        post("/api/v1/care/devices/me/checkins/$checkinId/missed", "{}").map { }

    private fun get(path: String): ApiOutcome<String> = execute(Request.Builder().url(baseUrl + path).get(), auth = true)

    private fun post(path: String, body: String, auth: Boolean = true): ApiOutcome<String> =
        execute(Request.Builder().url(baseUrl + path).post(body.toRequestBody(mediaType)), auth)

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
    }
}

inline fun <T, R> ApiOutcome<T>.map(transform: (T) -> R): ApiOutcome<R> = when (this) {
    is ApiOutcome.Ok -> ApiOutcome.Ok(transform(value))
    is ApiOutcome.Offline -> this
    is ApiOutcome.Unauthorised -> this
    is ApiOutcome.Failed -> this
}
