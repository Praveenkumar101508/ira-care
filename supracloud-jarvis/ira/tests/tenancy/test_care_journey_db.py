"""The complete care journey (directive section 16), as one test, end to end.

Twenty-two steps, in order, against the real care API over HTTP (ASGI) and a real
PostgreSQL as `ira_app`, with the background jobs (check-in cycle, escalation, reports)
called the way the worker calls them. Each step asserts something a person would see.

Fictional people only. The watch is driven over its own routes (the device token);
its readings are SIMULATED (labelled below). Messages go to a recording provider and a
fake push service: nothing leaves this machine, and no real emergency number is used.
The network failure is simulated with an HTTP transport that either fails before the
server sees a request, or loses the answer after the server has acted on it.

docs/demo/CARE_JOURNEY_22.md lists each step and the assertion that proves it.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
httpx = pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import _principal, client, pool  # noqa: E402,F401
from tests.tenancy.test_care_delivery_db import Recording  # noqa: E402
from tests.tenancy.test_care_push_db import FakePush  # noqa: E402

from care import checkin_cycle as cycle  # noqa: E402
from care import conversation as conv  # noqa: E402
from care import escalation, reports, sos  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
FAMILY_TOKEN = "ExponentPushToken[journey-daughter-phone]"
FAMILY_PHONE = "+440000000101"   # a test number: never an emergency number, never dialled for real


# ── The organisation and its people, created and removed by this test ─────────

@pytest.fixture
async def org(pool):
    """A founder's verified account and four more fictional accounts (daughter, carer, an outsider, Edith herself). The organisation
    itself is created by step 1 through the real route; everything is removed afterwards."""
    admin = await asyncpg.connect(ADMIN_DSN)
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("founder", "daughter", "carer", "outsider", "wearer")}
    emails = {k: f"journey-{k}-{tag}@example.invalid" for k in users}
    for k, uid in users.items():
        await admin.execute(
            "INSERT INTO users (id, email, email_verified_at, display_name, auth_provider, status) "
            "VALUES ($1, $2, NOW(), $3, 'password', 'active')", uid, emails[k], f"Journey {k}")
    state = {"users": users, "emails": emails, "tenant_id": None, "admin": admin}
    try:
        yield state
    finally:
        if state["tenant_id"]:
            await admin.execute("DELETE FROM care_access_log WHERE tenant_id = $1", state["tenant_id"])
            await admin.execute("DELETE FROM tenants WHERE id = $1", state["tenant_id"])
        await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", list(users.values()))
        await admin.close()


class Watch(httpx.AsyncBaseTransport):
    """The watch's network. `down`: nothing reaches the server. `lose_answers`: the next
    requests reach the server and are acted on, but the answer never comes back."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.down = False
        self.lose_answers = 0
        self.reached = 0

    async def handle_async_request(self, request):
        if self.down:
            raise httpx.ConnectError("no network", request=request)
        response = await self.inner.handle_async_request(request)
        self.reached += 1
        if self.lose_answers:
            self.lose_answers -= 1
            await response.aread()
            raise httpx.ReadError("connection lost before the answer arrived", request=request)
        return response


def _settings_body(s: dict) -> dict:
    return {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}


async def _alerts(client, person_id, open_only=False) -> list[dict]:
    r = await client.get(f"{C}/alerts", params={"person_id": person_id, "open_only": str(open_only).lower()})
    assert r.status_code == 200, r.text
    return r.json()["alerts"]


async def test_the_complete_care_journey_in_twenty_two_steps(client, org):
    from api.middleware.auth import require_auth

    model = conv.FakeModel()
    conv.use_model(model)
    try:
        await _journey(client, org, model, require_auth)
    finally:
        conv.use_model(None)


async def _journey(client, org, model, require_auth):  # noqa: C901 - one story, told in order
    users = org["users"]

    # ── 1. Create a care organisation and wearer ──────────────────────────────
    client.app.dependency_overrides[require_auth] = lambda: org["emails"]["founder"]
    r = await client.post(f"{C}/organisations", json={"name": "Journey Home Care", "plan_id": "care_organisation"})
    assert r.status_code == 201, r.text
    made = r.json()
    org["tenant_id"] = uuid.UUID(made["tenant_id"])
    fam = {"tenant_id": org["tenant_id"], "workspace_id": uuid.UUID(made["workspace_id"]), **users}
    async with org["admin"].transaction():
        for k in ("daughter", "carer", "outsider", "wearer"):
            await org["admin"].execute(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at) VALUES ($1, $2, 'member', 'active', NOW())",
                fam["tenant_id"], users[k])
    client.act_as(fam, "founder", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Edith Example", "preferred_name": "Edith", "timezone": "UTC",
                                               "home_address": "1 Example Street"})
    assert r.status_code == 201, r.text
    person = r.json()
    pid = person["id"]
    assert [p["id"] for p in (await client.get(f"{C}/people")).json()["people"]] == [pid], "step 1: the wearer is on the organisation's list"
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])

    # ── 2. Consent and care-circle access ─────────────────────────────────────
    r = await client.post(f"{C}/people/{pid}/circle", json={
        "display_name": "Daughter Dee", "role": "family", "relationship": "daughter", "tier": 1,
        "user_id": str(users["daughter"]), "phone": FAMILY_PHONE})
    assert r.status_code == 201, r.text
    daughter = r.json()
    r = await client.post(f"{C}/people/{pid}/circle", json={
        "display_name": "Carer Cal", "role": "carer", "tier": 2, "user_id": str(users["carer"])})
    assert r.status_code == 201, r.text
    carer = r.json()
    r = await client.post(f"{C}/people/{pid}/circle", json={
        "display_name": "Edith", "role": "family", "relationship": "self", "user_id": str(users["wearer"])})
    assert r.status_code == 201, r.text
    for kind in ("health_data_family", "topics"):
        r = await client.post(f"{C}/people/{pid}/consents", json={"kind": kind, "granted": True, "granted_by": "person", "granted_by_name": "Edith"})
        assert r.status_code == 201, r.text
    consents = (await client.get(f"{C}/people/{pid}/consents")).json()
    granted = {c["kind"] for c in consents["consents"] if c["granted"]}
    assert {"health_data_family", "topics"} <= granted, f"step 2: consents recorded: {consents}"
    client.act_as(fam, "daughter", "member")
    assert (await client.get(f"{C}/people/{pid}/status")).status_code == 200, "step 2: the daughter in the circle sees Edith"
    client.act_as(fam, "outsider", "member")
    assert (await client.get(f"{C}/people/{pid}/status")).status_code == 404, "step 2: an account outside the circle does not"
    client.act_as(fam, "daughter", "member")
    r = await client.post(f"{C}/push/register", json={"token": FAMILY_TOKEN, "platform": "ios", "person_id": pid})
    assert r.status_code == 201, r.text

    # ── 3. Pair the watch ─────────────────────────────────────────────────────
    client.act_as(fam, "founder", "workspace_admin")
    s = (await client.get(f"{C}/people/{pid}/settings")).json()
    body = _settings_body(s)
    body["quiet_start"], body["quiet_end"] = "00:00", "00:00"   # no quiet hours: the wall clock does not decide this test
    body["fall_detection_enabled"] = True
    assert (await client.put(f"{C}/people/{pid}/settings", json=body)).status_code == 200
    r = await client.post(f"{C}/people/{pid}/devices", json={"platform": "wearos"})
    assert r.status_code == 201, r.text
    net = Watch(client.app)
    watch = httpx.AsyncClient(transport=net, base_url="http://care.invalid")
    try:
        paired = (await watch.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
        h = {"X-Device-Token": paired["device_token"]}
        device = dict(paired, person_id=uuid.UUID(pid), id=uuid.UUID(paired["id"]))
        me = (await watch.get(f"{C}/devices/me", headers=h)).json()
        assert me["person_id"] == pid and me["status"] == "paired", "step 3: the watch knows whose it is"
        assert (await watch.get(f"{C}/devices/me/sos", headers=h)).json()["state"] == "idle", "step 3: the paired watch is told all is quiet"
        devices = (await client.get(f"{C}/people/{pid}/devices")).json()
        devices = devices["devices"]
        assert any(d["id"] == paired["id"] and d["status"] == "paired" for d in devices), "step 3: the dashboard shows it paired"

        await _steps_4_to_22(client, watch, net, fam, pid, h, device, jp, daughter, carer, model)
    finally:
        await watch.aclose()


async def _steps_4_to_22(client, watch, net, fam, pid, h, device, jp, daughter, carer, model):  # noqa: C901
    now = dt.datetime.now(UTC)

    # ── 4. Routines and care schedules ────────────────────────────────────────
    client.act_as(fam, "founder", "workspace_admin")
    r = await client.post(f"{C}/people/{pid}/routines", json={"name": "Morning walk", "kind": "activity", "time_of_day": "10:00"})
    assert r.status_code == 201, r.text
    r = await client.post(f"{C}/people/{pid}/medications", json={"name": "Blue tablet", "dose": "1", "times": ["08:00"]})
    assert r.status_code == 201, r.text
    med = r.json()
    later = now + dt.timedelta(hours=3)
    r = await client.post(f"{C}/people/{pid}/visits", json={
        "planned_start": later.isoformat(), "planned_end": (later + dt.timedelta(hours=1)).isoformat(),
        "carer_member_id": carer["id"], "purpose": "Lunch visit", "tasks": ["Lunch", "Medication"]})
    assert r.status_code == 201, r.text
    routines = (await client.get(f"{C}/people/{pid}/routines")).json()
    assert [x["name"] for x in routines["routines"]] == ["Morning walk"], "step 4: the routine is saved"
    assert [m["name"] for m in (await client.get(f"{C}/people/{pid}/medications")).json()["medications"]] == ["Blue tablet"]
    assert [v["purpose"] for v in (await client.get(f"{C}/people/{pid}/visits")).json()["visits"]] == ["Lunch visit"], "step 4: the visit is scheduled"

    # ── 5. IRA initiates a conversation ───────────────────────────────────────
    out = await cycle.run_workspace(jp, now)
    assert out is not None
    due = await cycle.due_checkin(jp, device, now + dt.timedelta(minutes=25))
    assert due is not None and "Edith" in due["question_text"] and due["question_text"].count("?") == 1, f"step 5: IRA asks Edith by name: {due}"
    assert due["question_source"] == "model", "step 5: composed by IRA's model (the deterministic test model)"
    answered = await cycle.record_answer(jp, device, due["id"], "I am fine thanks, a quiet morning", via="voice", now=now + dt.timedelta(minutes=26))
    assert answered is not None
    client.act_as(fam, "daughter", "member")
    checkins = (await client.get(f"{C}/people/{pid}/checkins", params={"limit": 10})).json()["checkins"]
    assert any(c["id"] == str(due["id"]) and c["status"] == "answered" for c in checkins), "step 5: the answer is on the record"

    # ── 6. IRA remembers an authorised preference ─────────────────────────────
    profile = {"pace": "slow", "prompting_level": "standard", "voice_on": True, "touch_on": True, "vibration_on": True,
               "topics_avoid": [], "familiar_topics": ["her garden roses"]}
    r = await client.put(f"{C}/people/{pid}/profile", json=profile)
    assert r.status_code == 200, r.text
    assert r.json()["familiar_topics"] == ["her garden roses"] and r.json()["topics_consented"], "step 6: kept because Edith consented (step 2)"
    client.act_as(fam, "wearer", "member")
    model.calls.clear()
    r = await client.post(f"{C}/people/{pid}/talk", json={"text": "Tell me something cheerful"})
    assert r.status_code == 200, r.text
    prompts = [m["content"] for call in model.calls for m in call if m["role"] == "system"]
    assert any("Enjoys: her garden roses" in p for p in prompts), "step 6: IRA's next conversation uses the preference"
    client.act_as(fam, "daughter", "member")
    assert len((await client.get(f"{C}/people/{pid}/memory")).json()["memory"]) >= 1, "step 6: the day is remembered, for the family to see and correct"

    # ── 7. Medication reminder ────────────────────────────────────────────────
    async with workspace_connection(jp) as conn:
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", uuid.UUID(pid))
    r = await client.post(f"{C}/people/{pid}/reminders", json={
        "text": "Blue tablet", "kind": "medication", "medication_id": med["id"],
        "due_at": (dt.datetime.now(UTC) - dt.timedelta(seconds=1)).isoformat()})
    assert r.status_code == 201, r.text
    await cycle.run_workspace(jp, dt.datetime.now(UTC))
    due = (await watch.get(f"{C}/devices/me/checkins/due", headers=h)).json()["checkin"]
    assert due and due["question_kind"] == "medication" and "Blue tablet" in due["question_text"], f"step 7: the watch asks about the tablet: {due}"

    # ── 8. An ambiguous answer is handled safely ──────────────────────────────
    r = await watch.post(f"{C}/devices/me/checkins/{due['id']}/answer", json={"text": "maybe, I can't remember", "via": "voice"},
                         headers={**h, sos.IDEMPOTENCY_HEADER: str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    said = r.json()
    assert said["said"] == "unclear" and said["structured"]["medication_taken"] == "unknown", f"step 8: 'maybe' is read as unclear: {said}"
    assert said["reminder"]["status"] == "snoozed", "step 8: the tablet is asked about again later, not ticked off"
    events = (await client.get(f"{C}/people/{pid}/medication-events")).json()["events"]
    assert not any(e["status"] == "taken" for e in events), "step 8: no dose is written from an unclear answer"

    # ── 9. A care visit is scheduled and completed ────────────────────────────
    client.act_as(fam, "founder", "workspace_admin")
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=5)
    r = await client.post(f"{C}/people/{pid}/visits", json={
        "planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=1)).isoformat(),
        "carer_member_id": carer["id"], "purpose": "Morning visit", "tasks": ["Breakfast"]})
    assert r.status_code == 201, r.text
    visit = r.json()
    client.act_as(fam, "carer", "member")
    r = await client.post(f"{C}/visits/{visit['id']}/arrive")
    assert r.status_code == 200 and r.json()["status"] == "arrived", r.text
    assert (await watch.get(f"{C}/devices/me/sos", headers=h)).json()["visit"]["state"] == "here", "step 9: the watch says the carer is here"
    task_id = r.json()["tasks"][0]["id"]
    r = await client.post(f"{C}/visits/{visit['id']}/depart", json={"outcome": "all_well", "notes": "Breakfast eaten.",
                                                                   "handover": "Evening tablet due.", "tasks_done": [task_id]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "departed" and r.json()["outcome"] == "all_well" and r.json()["tasks"][0]["done"], "step 9: the visit is the record"

    # ── 10. A missed visit is identified ──────────────────────────────────────
    client.act_as(fam, "founder", "workspace_admin")
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=70)
    r = await client.post(f"{C}/people/{pid}/visits", json={
        "planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=1)).isoformat(),
        "carer_member_id": carer["id"], "purpose": "Tea visit"})
    assert r.status_code == 201, r.text
    missed_visit = r.json()
    out = await escalation.run_workspace(jp, dt.datetime.now(UTC), provider=Recording(), push_provider=FakePush())
    assert out.visits_missed == 1, f"step 10: the job finds the visit that never happened: {out}"
    assert (await client.get(f"{C}/visits/{missed_visit['id']}")).json()["status"] == "missed"
    missed = [a for a in await _alerts(client, pid) if a["kind"] == "visit_missed"]
    assert missed and missed[0]["severity"] == "urgent", "step 10: an urgent alert for the coordinator"
    r = await client.post(f"{C}/alerts/{missed[0]['id']}/resolve", json={"resolution": "Rebooked for tomorrow.", "outcome": "all_well"})
    assert r.status_code == 200, r.text
    async with workspace_connection(jp) as conn:   # the missed visit's extra check-in is not this story's
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status IN ('scheduled', 'asked')", uuid.UUID(pid))

    # ── 11. A simulated fall occurs ───────────────────────────────────────────
    # SIMULATED reading: no person fell. A hard impact and stillness, as the watch reports it.
    r = await watch.post(f"{C}/devices/me/fall", json={"peak_g": 3.6, "still_seconds": 20},
                         headers={**h, sos.IDEMPOTENCY_HEADER: "journey-fall-1"})
    assert r.status_code == 201, r.text
    fall = r.json()
    fell_at = dt.datetime.now(UTC)
    assert fall["kind"] == "suspected_fall" and fall["status"] == "asking", f"step 11: a suspected fall, asked about first: {fall}"

    # ── 12. IRA checks the person's wellbeing ─────────────────────────────────
    a = next(x for x in await _alerts(client, pid) if x["id"] == fall["id"])
    assert a["status"] == "asking" and a["next_action_at"], "step 12: the alert waits for Edith's answer, with a deadline"
    wrist = (await watch.get(f"{C}/devices/me/sos", headers=h)).json()
    assert (wrist["state"], wrist["alert_id"], wrist["responder_name"]) == ("asking", fall["id"], None), \
        f"step 12: the wrist is asking Edith whether she is OK; nobody is sent yet: {wrist}"
    quiet = await escalation.run_workspace(jp, fell_at + dt.timedelta(seconds=20), provider=Recording(), push_provider=FakePush())
    assert quiet.promoted == 0, "step 12: inside the countdown nothing escalates"

    # ── 13. The wearer does not respond ───────────────────────────────────────
    # ── 14. An SOS is created ─────────────────────────────────────────────────
    # ── 15. A test notification reaches the family ────────────────────────────
    provider, pusher = Recording(), FakePush()
    out = await escalation.run_workspace(jp, fell_at + dt.timedelta(seconds=45), provider=provider, push_provider=pusher)
    assert out.promoted == 1 and out.tiers_run >= 1, f"step 13: silence past the deadline promotes the fall: {out}"
    a = next(x for x in await _alerts(client, pid) if x["id"] == fall["id"])
    assert a["status"] == "escalating", f"step 14: the SOS is live: {a}"
    assert (await watch.get(f"{C}/devices/me/sos", headers=h)).json()["state"] == "received", "step 14: the wrist says help is called"
    assert ("sms", FAMILY_PHONE) in provider.sent, f"step 15: a text to the daughter, by the test provider: {provider.sent}"
    assert FAMILY_TOKEN in pusher.tokens, "step 15: a push to the daughter's registered phone (fake push service)"
    assert all("999" not in str(m) for m in pusher.messages), "step 15: no emergency number is involved"

    # ── 16. The dashboard receives the incident ───────────────────────────────
    client.act_as(fam, "founder", "workspace_admin")
    open_now = await _alerts(client, pid, open_only=True)
    assert open_now and open_now[0]["id"] == fall["id"], "step 16: the fall heads the open alerts"
    status = (await client.get(f"{C}/people/{pid}/status")).json()
    assert [(x["id"], x["severity"], x["status"]) for x in status["open_alerts"]] == [(fall["id"], "sos", "escalating")], \
        "step 16: Edith's record shows the live SOS"
    overview = (await client.get(f"{C}/overview")).json()
    assert [a["id"] for a in overview["alerts"] if a["severity"] == "sos"] == [fall["id"]], "step 16: the organisation's overview shows it"
    assert any(x["person_name"] == "Edith" and "an alert is waiting for someone" in x["reasons"] for x in overview["attention"])

    # ── 17. A caregiver acknowledges it ───────────────────────────────────────
    client.act_as(fam, "daughter", "member")
    r = await client.post(f"{C}/alerts/{fall['id']}/acknowledge", json={"kind": "on_my_way"})
    assert r.status_code == 200, r.text
    wrist = (await watch.get(f"{C}/devices/me/sos", headers=h)).json()
    assert (wrist["state"], wrist["responder_name"]) == ("on_the_way", "Daughter Dee"), f"step 17: the wrist names who is coming: {wrist}"
    async def fall_attempts() -> int:
        async with workspace_connection(jp) as conn:
            return await conn.fetchval("SELECT count(*) FROM care_alert_attempts WHERE alert_id = $1", uuid.UUID(fall["id"]))
    before = await fall_attempts()
    acked_at = dt.datetime.now(UTC)
    # Twenty minutes on: past the 5-minute wait after which an unanswered tier calls the next one,
    # inside the 30-minute follow-up. An acknowledged fall calls nobody else.
    await escalation.run_workspace(jp, acked_at + dt.timedelta(minutes=20), provider=Recording(), push_provider=FakePush())
    assert await fall_attempts() == before, "step 17: once someone is on the way, nobody else is called for this fall"
    # Thirty-one minutes on and still no arrival: IRA asks the responder, and only the responder.
    follow = FakePush()
    out = await escalation.run_workspace(jp, acked_at + dt.timedelta(minutes=31), provider=Recording(), push_provider=follow)
    about_fall = [m for m in follow.messages if m.get("data", {}).get("care_alert_id") == fall["id"]]
    assert out.follow_ups_asked == 1 and [m["to"] for m in about_fall] == [FAMILY_TOKEN], \
        f"step 17: 'have you reached Edith?' to Dee, and to nobody else: {about_fall}"

    # ── 18. Arrival and outcome are recorded ──────────────────────────────────
    r = await client.post(f"{C}/alerts/{fall['id']}/acknowledge", json={"kind": "arrived"})
    assert r.status_code == 200, r.text
    assert (await watch.get(f"{C}/devices/me/sos", headers=h)).json()["state"] == "arrived"
    r = await client.post(f"{C}/alerts/{fall['id']}/resolve", json={"resolution": "Found sitting; no injury. Helped up.", "outcome": "all_well"})
    assert r.status_code == 200, r.text
    done = (await client.get(f"{C}/alerts/{fall['id']}")).json()
    assert done["status"] == "resolved" and done["outcome"] == "all_well" and done["resolution"].startswith("Found sitting"), "step 18"
    assert (await watch.get(f"{C}/devices/me/sos", headers=h)).json()["state"] == "closed", "step 18: the wrist hears it is over"

    # ── 19. Daily and weekly reports are generated ────────────────────────────
    today = dt.datetime.now(UTC).date().isoformat()
    r = await client.post(f"{C}/people/{pid}/reports/generate", json={"kind": "daily_carer", "date": today})
    assert r.status_code == 201, r.text
    daily = r.json()
    assert daily["period_start"] == today and "check-in" in daily["body_text"].lower(), f"step 19: {daily['body_text'][:300]}"
    assert "garden roses" not in daily["body_text"] and "can't remember" not in daily["body_text"], "step 19: summaries, never her words"
    r = await client.post(f"{C}/people/{pid}/reports/generate", json={"kind": "weekly_family", "date": today})
    assert r.status_code == 201, r.text
    assert len(r.json()["body"]["days"]) == 7, "step 19: the weekly report covers seven days"

    # ── 20. Network failure is simulated ──────────────────────────────────────
    alerts_before = len(await _alerts(client, pid))
    reached_before = net.reached
    net.down = True
    offline_t0 = dt.datetime.now(UTC) - dt.timedelta(minutes=3)
    # SIMULATED readings taken while offline, kept on the watch.
    held = [{"kind": "heart_rate", "value": 71 + i, "unit": "bpm", "recorded_at": (offline_t0 + dt.timedelta(seconds=30 * i)).isoformat()}
            for i in range(4)]
    help_key = "journey-help-while-offline"
    with pytest.raises(httpx.ConnectError):
        await watch.post(f"{C}/devices/me/help", json={"via": "button"}, headers={**h, sos.IDEMPOTENCY_HEADER: help_key})
    with pytest.raises(httpx.ConnectError):
        await watch.post(f"{C}/devices/me/report", json={"readings": held, "on_wrist": True}, headers=h)
    assert net.reached == reached_before, "step 20: nothing reached the server"
    assert len(await _alerts(client, pid)) == alerts_before

    # ── 21. Offline functionality: the outbox keeps the events and re-sends the same keys ──
    # The connection comes back for one request, the server acts on the Help, and the answer is lost:
    # the watch cannot tell whether it went, so its outbox keeps the event and sends it again.
    net.down, net.lose_answers = False, 1
    with pytest.raises(httpx.ReadError):
        await watch.post(f"{C}/devices/me/help", json={"via": "button"}, headers={**h, sos.IDEMPOTENCY_HEADER: help_key})
    assert len(await _alerts(client, pid)) == alerts_before + 1, "step 21: the server acted on it although the watch never heard"

    # ── 22. Connectivity recovers without duplicate actions ───────────────────
    again = await watch.post(f"{C}/devices/me/help", json={"via": "button"}, headers={**h, sos.IDEMPOTENCY_HEADER: help_key})
    assert again.status_code == 201 and again.headers.get("X-Idempotent-Replay") == "true", "step 22: the same key is a replay"
    again2 = await watch.post(f"{C}/devices/me/help", json={"via": "button"}, headers={**h, sos.IDEMPOTENCY_HEADER: help_key})
    assert again2.json()["id"] == again.json()["id"]
    help_alerts = [a for a in await _alerts(client, pid) if a["id"] == again.json()["id"]]
    assert len(await _alerts(client, pid)) == alerts_before + 1 and len(help_alerts) == 1, "step 22: one Help, one alert"
    first = await watch.post(f"{C}/devices/me/report", json={"readings": held, "on_wrist": True}, headers=h)
    resent = await watch.post(f"{C}/devices/me/report", json={"readings": held, "on_wrist": True}, headers=h)
    assert first.status_code == 200 and resent.status_code == 200
    vitals = (await client.get(f"{C}/people/{pid}/vitals", params={"kind": "heart_rate", "limit": 50})).json()
    offline_times = {r["recorded_at"] for r in held}
    stored = [v for v in vitals["vitals"] if dt.datetime.fromisoformat(v["recorded_at"]).isoformat() in offline_times]
    assert len(stored) == len(held), f"step 22: the readings held offline arrive once each, with their own times ({len(stored)})"
    client.act_as(fam, "daughter", "member")
    await client.post(f"{C}/alerts/{again.json()['id']}/resolve", json={"resolution": "Rang Edith; she pressed it by mistake.", "outcome": "false_alarm"})
