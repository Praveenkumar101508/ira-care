package care.ira.watch.service

import android.content.Context
import android.os.BatteryManager
import care.ira.watch.BuildConfig
import care.ira.watch.api.ApiOutcome
import care.ira.watch.api.CareApi
import care.ira.watch.core.Clock
import care.ira.watch.pairing.PairingStore
import care.ira.watch.queue.OfflineQueue
import care.ira.watch.storage.KeystoreKey
import care.ira.watch.storage.WearerFiles
import care.ira.watch.api.FallSettingsDto
import care.ira.watch.api.ProfileDto
import care.ira.watch.api.ReadingSettingsDto
import care.ira.watch.api.SosStateDto
import care.ira.watch.outbox.Outbox
import care.ira.watch.outbox.SosState
import care.ira.watch.outbox.VisitState
import care.ira.watch.outbox.UrgentEvent
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import care.ira.watch.sensors.AccelerometerSource
import care.ira.watch.sensors.AndroidAccelerometerSource
import care.ira.watch.sensors.AndroidLocationSource
import care.ira.watch.sensors.SimulatedSensorSource
import care.ira.watch.sensors.LocationSource
import care.ira.watch.sensors.SensorSource
import care.ira.watch.platform.AndroidConnectivity
import care.ira.watch.platform.AndroidHaptics
import care.ira.watch.platform.Connectivity
import care.ira.watch.platform.Haptics
import care.ira.watch.platform.PlatformDrivers
import care.ira.watch.platform.ServerChannel
import care.ira.watch.platform.SosStreamChannel
import care.ira.watch.platform.SpeechCapabilities
import care.ira.watch.voice.AndroidListener
import care.ira.watch.voice.AndroidSpeaker
import care.ira.watch.voice.Listener
import care.ira.watch.voice.Speaker
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class DeviceState(val battery: Int?, val onWrist: Boolean?, val permission: Boolean?, val appVersion: String)

/** What the watch is showing right now, for the screen. */
data class WatchStatus(
    val paired: Boolean,
    val permissionGranted: Boolean,
    val question: String? = null,
    val listening: Boolean = false,
    val lastAnswer: String? = null,
    /** The true state of a Help request or a fall, as the server reports it (never guessed). */
    val sos: SosState = SosState.Idle,
    /** A carer's visit, as the server reports it (109). */
    val visit: VisitState = VisitState.None,
    val queued: Int = 0,
    /** Urgent events still waiting for the server's answer. */
    val urgentPending: Int = 0,
    val online: Boolean = true,
    /** Seconds left in a fall countdown, or null when none is running. */
    val fallCountdown: Int? = null,
    /** A "Talk to IRA" exchange is in progress. */
    val talking: Boolean = false,
)

/**
 * The app's wiring, built once. Real Android implementations here; tests
 * build the pieces they need directly with fakes.
 */
class CareDependencies private constructor(context: Context) {
    val clock: Clock = Clock.SYSTEM
    /** Sealing for everything the watch keeps (Keystore key); see care/ira/watch/storage. */
    private val sealer = KeystoreKey.sealer()
    val pairing = PairingStore(context, sealer)
    /** Each wearer's readings and urgent events in that wearer's own files: never sent in another's name. */
    private val readingFiles = WearerFiles(context.filesDir, "readings") { pairing.personId }
    private val outboxFiles = WearerFiles(context.filesDir, "outbox") { pairing.personId }
    val queue = OfflineQueue({ readingFiles.current() }, sealer = sealer)
    /** Help, falls and answers, on disk until the server has answered. */
    val outbox = Outbox({ outboxFiles.current() }, clock, sealer = sealer)
    val api = CareApi(BuildConfig.CARE_API_BASE_URL, { pairing.token })
    /** The platform's readings, or the scripted driver when the build asks for it (phase 8). */
    val sensors: SensorSource = if (BuildConfig.SIMULATED_SENSORS) SimulatedSensorSource(clock) else PlatformDrivers.sensors(context, clock)
    val platform: String = PlatformDrivers.NAME
    val haptics: Haptics = AndroidHaptics(context)
    val connectivity: Connectivity = AndroidConnectivity(context)
    /** Our own push channel: the server's held connection, nothing in between. */
    val channel: ServerChannel by lazy { SosStreamChannel(api) }
    val speech: SpeechCapabilities by lazy { SpeechCapabilities.probe(context) }
    /** Asked once per SOS; never polled. */
    val location: LocationSource = AndroidLocationSource(context)
    val accelerometer: AccelerometerSource = AndroidAccelerometerSource(context)
    val gyroscope: care.ira.watch.sensors.GyroscopeSource = care.ira.watch.sensors.AndroidGyroscopeSource(context)
    /**
     * IRA Care Activity (WISDM, CC BY 4.0): what the wrist is doing, kept on the watch and never
     * sent. Null when the model asset is missing or malformed: then there is no estimate at all.
     */
    val activity: care.ira.watch.activity.ActivityMonitor? = runCatching {
        val text = context.assets.open(care.ira.watch.activity.ActivityModel.ASSET).bufferedReader().use { it.readText() }
        care.ira.watch.activity.ActivityMonitor(care.ira.watch.activity.ActivityModel.parse(text))
    }.getOrNull()
    /** The trained fall model and its normalisation, when the build carries them; null means rules only. */
    val fallModel: Pair<care.ira.watch.sensors.Inference, care.ira.watch.sensors.FallModelSpec>? =
        care.ira.watch.sensors.TfLiteInference.load(context)
    /** The carer's fall settings, refreshed from the server after each sync. */
    @Volatile var fallSettings: FallSettingsDto = FallSettingsDto()
    /** The carer's heart-rate range and the upload interval, refreshed the same way. */
    @Volatile var readingSettings: ReadingSettingsDto = ReadingSettingsDto()
    /** How IRA speaks to this person (pace, prompting, channels), from the server. */
    @Volatile var profile: ProfileDto = ProfileDto()
    private val json = Json { ignoreUnknownKeys = true }
    val speaker: Speaker = AndroidSpeaker(context)
    val listener: Listener = AndroidListener(context)
    val sync = Syncer(this)
    val status = MutableStateFlow(WatchStatus(pairing.isPaired, sensors.backgroundPermissionGranted()))
    private val appContext = context.applicationContext

    @Volatile var lastOnWrist: Boolean? = null

    fun deviceState(): DeviceState {
        val bm = appContext.getSystemService(BatteryManager::class.java)
        val battery = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)?.takeIf { it in 0..100 }
        return DeviceState(battery, lastOnWrist, sensors.backgroundPermissionGranted(), BuildConfig.VERSION_NAME)
    }

    /** The wearer confirmed unpairing (see pairing/Unpairing): the server first, then the watch. */
    fun unpairWatch(): care.ira.watch.pairing.Unpairing.Result =
        care.ira.watch.pairing.Unpairer({ api.unpair() }) { onUnauthorised() }.unpair()

    fun onUnauthorised() {
        pairing.clear()
        status.value = status.value.copy(paired = false)
    }

    /** The server's word on the SOS: the only source of "received", "on the way", "arrived". */
    fun applyServerSos(dto: SosStateDto) {
        val fromServer = SosState.fromServer(dto.state, dto.responderName)
        val current = status.value.sos
        val next = when {
            fromServer != null -> fromServer
            // Nothing open on the server: a request still in the outbox keeps its own state.
            current is SosState.Sending || current is SosState.WaitingForConnection -> current
            else -> SosState.Idle
        }
        val visit = dto.visit?.let { VisitState.fromServer(it.state, it.carerName, it.plannedStart) } ?: status.value.visit
        val arrivedNow = visit is VisitState.Here && status.value.visit !is VisitState.Here
        if (next != current || visit != status.value.visit) status.value = status.value.copy(sos = next, visit = visit)
        if (arrivedNow) onCarerArrived(dto.visit?.visitId)
    }

    /** A carer has just arrived: the service vibrates the wrist, then the server is told the person saw it. */
    var onCarerArrived: (visitId: String?) -> Unit = { id -> if (id != null) api.acknowledgeVisit(id) }

    /** An urgent event reached the server. A Help or fall answer carries the SOS state. */
    fun onUrgentDelivered(event: UrgentEvent, responseBody: String) {
        status.value = status.value.copy(urgentPending = outbox.size())
        if (event.kind == Outbox.KIND_HELP) {
            val state = runCatching { json.parseToJsonElement(responseBody).jsonObject["sos"]?.jsonObject }.getOrNull()
            val name = (state?.get("responder_name") as? JsonPrimitive)?.content
            applyServerSos(SosStateDto(state = (state?.get("state") as? JsonPrimitive)?.content ?: "received", responderName = name))
        }
    }

    /** The server refused the event (a definite answer): it is not retried. */
    fun onUrgentRefused(event: UrgentEvent, httpStatus: Int) {
        status.value = status.value.copy(urgentPending = outbox.size())
        if (event.kind == Outbox.KIND_HELP && status.value.sos is SosState.Sending) {
            // A refused Help request must never read as help coming: ask the server what it holds.
            when (val s = api.sosState()) {
                is ApiOutcome.Ok -> applyServerSos(s.value)
                else -> status.value = status.value.copy(sos = SosState.WaitingForConnection)
            }
        }
    }

    /** The server could not be reached; the outbox will retry. */
    fun onUrgentWaiting(event: UrgentEvent) {
        status.value = status.value.copy(urgentPending = outbox.size())
        if (event.kind == Outbox.KIND_HELP || event.kind == Outbox.KIND_FALL_STARTED) {
            status.value = status.value.copy(sos = SosState.WaitingForConnection)
        }
    }

    fun statusFlow(): StateFlow<WatchStatus> = status

    companion object {
        @Volatile private var instance: CareDependencies? = null

        fun get(context: Context): CareDependencies =
            instance ?: synchronized(this) { instance ?: CareDependencies(context.applicationContext).also { instance = it } }
    }
}
