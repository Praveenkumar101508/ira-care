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
}
