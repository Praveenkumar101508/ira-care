package care.ira.watch.api

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CareApiTest {
    private val server = MockWebServer()
    private var token: String? = "cd1.scope.secret"
    private lateinit var api: CareApi

    @Before fun start() { server.start(); api = CareApi(server.url("/").toString().trimEnd('/'), { token }) }
    @After fun stop() { server.shutdown() }

    @Test
    fun `pairing sends the code without a token and keeps the device token`() {
        server.enqueue(MockResponse().setBody("""{"id":"d1","person_id":"p1","status":"paired","device_token":"cd1.s.t","extra":1}"""))
        val out = api.pair("cp1.scope.ABCDEFGH", "0.1-test") as ApiOutcome.Ok
        assertEquals("cd1.s.t", out.value.deviceToken)
        val req = server.takeRequest()
        assertEquals("/api/v1/care/devices/pair", req.path)
        assertNull(req.getHeader(CareApi.TOKEN_HEADER))
        assertTrue(req.body.readUtf8().contains("\"pairing_code\":\"cp1.scope.ABCDEFGH\""))
    }

    @Test
    fun `a report carries the token and the readings in the backend's shape`() {
        server.enqueue(MockResponse().setBody("""{"accepted":2,"stored":1}"""))
        val out = api.report(DeviceReport(listOf(Reading("heart_rate", 71.0, "2026-11-21T10:00:00Z", "bpm")), 64, true, true, "0.1"))
        assertEquals(ReportAck(2, 1), (out as ApiOutcome.Ok).value)
        val req = server.takeRequest()
        assertEquals("cd1.scope.secret", req.getHeader(CareApi.TOKEN_HEADER))
        val body = req.body.readUtf8()
        assertTrue(body.contains("\"recorded_at\":\"2026-11-21T10:00:00Z\""))
        assertTrue(body.contains("\"battery_percent\":64"))
        assertTrue(body.contains("\"on_wrist\":true"))
    }

    @Test
    fun `a due check-in may be null`() {
        server.enqueue(MockResponse().setBody("""{"checkin":null}"""))
        assertNull((api.dueCheckin() as ApiOutcome.Ok).value)
        server.enqueue(MockResponse().setBody("""{"checkin":{"id":"c1","status":"asked","question_kind":"meal","question_text":"Hi Tess. Have you eaten?","person_id":"p"}}"""))
        val due = (api.dueCheckin() as ApiOutcome.Ok).value!!
        assertEquals("c1", due.id); assertEquals("Hi Tess. Have you eaten?", due.questionText)
        assertEquals(false, due.safety)
        server.enqueue(MockResponse().setBody("""{"checkin":{"id":"s1","status":"asked","question_kind":"safety","question_text":"Tess, are you OK?","safety":true}}"""))
        assertEquals(true, (api.dueCheckin() as ApiOutcome.Ok).value!!.safety)
    }

    @Test
    fun `401 means pair again and a dead server means offline`() {
        server.enqueue(MockResponse().setResponseCode(401))
        assertTrue(api.dueCheckin() is ApiOutcome.Unauthorised)
        token = null
        assertTrue(api.repeated("c1") is ApiOutcome.Unauthorised)
        token = "t"
        server.shutdown()
        assertTrue(api.missed("c1") is ApiOutcome.Offline)
    }

    @Test
    fun `other errors are reported with their status`() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"No open check-in"}"""))
        val out = api.answer("c9", CheckinAnswer("yes", "voice"))
        assertEquals(404, (out as ApiOutcome.Failed).status)
    }

    @Test
    fun `help carries the one-off location when there is one and nothing when there is not`() {
        server.enqueue(MockResponse().setResponseCode(201).setBody("""{"id":"a1","kind":"help_requested","status":"open","at_home":false}"""))
        api.help(HelpRequest(via = "button", location = LocationFix(51.518, -0.12, 12.0, "2026-11-21T10:00:00Z")))
        val withFix = server.takeRequest().body.readUtf8()
        assertTrue(withFix.contains("\"latitude\":51.518") && withFix.contains("\"accuracy_m\":12.0"))
        server.enqueue(MockResponse().setResponseCode(201).setBody("""{"id":"a2","kind":"help_requested","status":"open","at_home":null}"""))
        api.help(HelpRequest(via = "voice"))
        val withoutFix = server.takeRequest().body.readUtf8()
        assertTrue(!withoutFix.contains("latitude"))
    }

    @Test
    fun `talk speaks each streamed sentence as it arrives and reads the final action`() {
        server.enqueue(MockResponse().setBody(
            """{"sentence": "Here is Family Member."}
{"sentence": "Tap to call."}
{"done": true, "action": {"kind": "call", "name": "Family Member", "phone": "+440000000001"}, "source": "rules", "needs_help": false}
"""))
        val spoken = mutableListOf<String>()
        val out = api.talk("call my daughter") { spoken += it } as ApiOutcome.Ok
        assertEquals(listOf("Here is Family Member.", "Tap to call."), spoken)
        assertEquals(spoken, out.value.sentences)
        assertEquals("call", (out.value.action!!["kind"] as kotlinx.serialization.json.JsonPrimitive).content)
        assertTrue(!out.value.needsHelp)
        val req = server.takeRequest()
        assertEquals("/api/v1/care/devices/me/talk", req.path)
        assertTrue(req.body.readUtf8().contains("\"text\":\"call my daughter\""))
        server.enqueue(MockResponse().setBody("""{"sentence": "I'm getting help for you now."}
{"done": true, "action": {"kind": "safety_check"}, "needs_help": true}
"""))
        val help = api.talk("I've fallen") { } as ApiOutcome.Ok
        assertTrue(help.value.needsHelp)
    }
}

class CareApiSosTest {
    private val server = MockWebServer()
    private var token: String? = "cd1.scope.secret"
    private lateinit var api: CareApi

    @Before fun start() { server.start(); api = CareApi(server.url("/").toString().trimEnd('/'), { token }) }
    @After fun stop() { server.shutdown() }

    @Test
    fun `an urgent event carries its idempotency key and the stored answer is accepted on a replay`() {
        server.enqueue(MockResponse().setResponseCode(201).setBody("""{"id":"a1","sos":{"state":"received"}}"""))
        val out = api.sendUrgent(CareApi.PATH_HELP, """{"via":"button"}""", "key-1")
        assertTrue(out is ApiOutcome.Ok)
        val req = server.takeRequest()
        assertEquals("key-1", req.getHeader(CareApi.IDEMPOTENCY_HEADER))
        assertEquals(CareApi.PATH_HELP, req.path)
        server.enqueue(MockResponse().setResponseCode(201).setHeader("X-Idempotent-Replay", "true").setBody("""{"id":"a1"}"""))
        assertTrue(api.sendUrgent(CareApi.PATH_HELP, """{"via":"button"}""", "key-1") is ApiOutcome.Ok)
    }

    @Test
    fun `network loss is offline and a refusal is a definite answer`() {
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"detail":"already asking"}"""))
        val refused = api.sendUrgent(CareApi.PATH_FALL, "{}", "k")
        assertEquals(409, (refused as ApiOutcome.Failed).status)
        server.shutdown()
        assertTrue(api.sendUrgent(CareApi.PATH_HELP, "{}", "k2") is ApiOutcome.Offline)
    }

    @Test
    fun `a server that cannot answer now is not a refusal, so an urgent event is kept and retried`() {
        // (408 is retried by OkHttp itself before we see it; the rule is checked directly below.)
        for (code in listOf(500, 502, 503, 504, 425, 429)) {
            server.enqueue(MockResponse().setResponseCode(code).setBody("""{"error_code":"dependency_unavailable"}"""))
            val out = api.sendUrgent(CareApi.PATH_HELP, """{"via":"button"}""", "key-$code")
            assertTrue("$code must be retried, not dropped", out is ApiOutcome.Offline)
        }
        // A real refusal is still a definite answer.
        for (code in listOf(400, 403, 404, 409, 422)) {
            server.enqueue(MockResponse().setResponseCode(code))
            val got = api.sendUrgent(CareApi.PATH_HELP, "{}", "r-$code")
            assertTrue("$code is a definite refusal, got $got", got is ApiOutcome.Failed)
        }
        assertTrue(CareApi.isRetryable(408))
        assertTrue(!CareApi.isRetryable(404) && !CareApi.isRetryable(409))
    }

    @Test
    fun `the device record carries the range the upload interval and the sos state`() {
        server.enqueue(MockResponse().setBody("""{"id":"d","status":"paired","fall":{"fall_countdown_s":45},"readings":{"heart_rate_min":45,"heart_rate_max":130,"reading_upload_interval_s":30},"sos":{"state":"on_the_way","responder_name":"Sravani","alert_id":"a1"}}"""))
        val me = (api.me() as ApiOutcome.Ok).value
        assertEquals(30, me.readings.uploadIntervalS)
        assertTrue(me.readings.outOfRange(160.0) && me.readings.outOfRange(40.0) && !me.readings.outOfRange(72.0))
        assertEquals("on_the_way", me.sos.state); assertEquals("Sravani", me.sos.responderName)
        assertTrue(!ReadingSettingsDto().outOfRange(200.0))   // no range set: no rule
    }

    @Test
    fun `the sos stream hands each state line on and skips heartbeats`() {
        server.enqueue(MockResponse().setBody("""{"state":"received","alert_id":"a1","responder_name":null}
{"heartbeat":"2026-11-23T02:00:00Z"}
{"state":"on_the_way","alert_id":"a1","responder_name":"Sravani"}
"""))
        val seen = mutableListOf<SosStateDto>()
        val out = api.sosStream { seen += it }
        assertTrue(out is ApiOutcome.Ok)
        assertEquals(listOf("received", "on_the_way"), seen.map { it.state })
        assertEquals("Sravani", seen[1].responderName)
        assertEquals("/api/v1/care/devices/me/sos/stream", server.takeRequest().path)
        server.enqueue(MockResponse().setResponseCode(401))
        assertTrue(api.sosStream { } is ApiOutcome.Unauthorised)
    }
}
