"""A trustworthy SOS (round 3, phase 1): the watch's urgent events are
idempotent, and the state the wrist shows comes from the server.

Real HTTP stack, real PostgreSQL as `ira_app`, the care fixtures.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402
from care import sos  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam, *, hr=(45, 130), fall=True) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Sravani", "role": "family", "user_id": str(fam["member"]), "phone": "+440000000001"})
    person["member"] = r.json()
    s = (await client.get(f"{C}/people/{person['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["heart_rate_min"], body["heart_rate_max"] = hr
    body["fall_detection_enabled"] = fall
    # No quiet hours for this test person: an ordinary check-in is asked whatever the wall clock says.
    body["quiet_start"], body["quiet_end"] = "00:00", "00:00"
    r = await client.put(f"{C}/people/{person['id']}/settings", json=body)
    assert r.status_code == 200, r.text
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    device = dict(paired); device["person_id"] = uuid.UUID(person["id"]); device["id"] = uuid.UUID(paired["id"])
    person["device"] = device; person["token"] = paired["device_token"]
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def _alert_count(client, person_id) -> int:
    return len((await client.get(f"{C}/alerts", params={"person_id": person_id, "open_only": "false"})).json()["alerts"])


# ── Idempotency: the outbox may send the same event many times ───────────────

async def test_a_help_request_sent_twice_with_one_key_opens_one_alert(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    key = str(uuid.uuid4())
    h = dict(p["h"], **{sos.IDEMPOTENCY_HEADER: key})
    r1 = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=h)
    assert r1.status_code == 201, r1.text
    assert r1.json()["sos"]["state"] == "received", "the first answer already says the server has it"
    r2 = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=h)
    assert r2.status_code == 201 and r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["id"] == r1.json()["id"]
    client.act_as(fam, "member", "member")
    assert await _alert_count(client, p["id"]) == 1
    # A different key is a new press: with the first alert still open the chain already runs,
    # and the server records a second request against the same person without a second alert.
    r3 = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=dict(p["h"], **{sos.IDEMPOTENCY_HEADER: str(uuid.uuid4())}))
    assert r3.status_code == 201
    async with workspace_connection(p["jp"]) as conn:
        n = await conn.fetchval("SELECT count(*) FROM care_device_events WHERE device_id = $1", p["device"]["id"])
    assert n == 2, "every distinct key is remembered"


async def test_a_fall_start_sent_twice_does_not_open_two_alerts_and_the_replay_is_not_a_409(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    key = str(uuid.uuid4())
    h = dict(p["h"], **{sos.IDEMPOTENCY_HEADER: key})
    r1 = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=h)
    assert r1.status_code == 201, r1.text
    r2 = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=h)
    assert r2.status_code == 201 and r2.json()["id"] == r1.json()["id"]
    # Without the key a repeat is what it always was: refused, because one is already asking.
    r3 = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=p["h"])
    assert r3.status_code == 409
    client.act_as(fam, "member", "member")
    assert await _alert_count(client, p["id"]) == 1


async def test_a_server_refusal_is_remembered_so_the_outbox_stops_retrying_it(client, families):
    fam = families["a"]
    p = await _setup(client, fam, fall=False)
    key = str(uuid.uuid4())
    h = dict(p["h"], **{sos.IDEMPOTENCY_HEADER: key})
    r1 = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=h)
    assert r1.status_code == 409
    r2 = await client.post(f"{C}/devices/me/fall", json={"peak_g": 3.4, "still_seconds": 20}, headers=h)
    assert r2.status_code == 409 and r2.headers.get("X-Idempotent-Replay") == "true"
    client.act_as(fam, "member", "member")
    assert await _alert_count(client, p["id"]) == 0


async def test_an_answer_sent_twice_is_recorded_once(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{p['id']}/checkins/ask-now", json={"kind": "wellbeing"})
    assert r.status_code == 201
    due = (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()["checkin"]
    assert due is not None
    key = str(uuid.uuid4())
    h = dict(p["h"], **{sos.IDEMPOTENCY_HEADER: key})
    r1 = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", json={"text": "I am fine thanks", "via": "voice"}, headers=h)
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", json={"text": "I am fine thanks", "via": "voice"}, headers=h)
    assert r2.status_code == 200 and r2.headers.get("X-Idempotent-Replay") == "true"
    # Without a key, the second answer finds no open check-in: that is the old behaviour, unchanged.
    r3 = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", json={"text": "again"}, headers=p["h"])
    assert r3.status_code == 404
    async with workspace_connection(p["jp"]) as conn:
        n = await conn.fetchval("SELECT count(*) FROM care_transcripts WHERE checkin_id = $1 AND speaker = 'person'", uuid.UUID(due["id"]))
    assert n == 1


async def test_keys_are_scoped_to_the_device(client, families):
    """The same key from another watch is another event, never a replay of the first."""
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/devices", json={"platform": "wearos"})
    second = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    key = str(uuid.uuid4())
    r1 = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": p["token"], sos.IDEMPOTENCY_HEADER: key})
    r2 = await client.post(f"{C}/devices/me/help", json={"via": "voice"}, headers={"X-Device-Token": second["device_token"], sos.IDEMPOTENCY_HEADER: key})
    assert r1.status_code == 201 and r2.status_code == 201 and r2.headers.get("X-Idempotent-Replay") is None


# ── The state the wrist shows ────────────────────────────────────────────────

async def test_the_wrist_sees_received_then_the_responders_name_then_arrival_then_closed(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    assert (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()["state"] == "idle"
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    alert_id = r.json()["id"]
    state = (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()
    assert state["state"] == "received" and state["alert_id"] == alert_id and state["responder_name"] is None
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["sos"]["state"] == "received", "/devices/me carries the same state for the sync path"
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/alerts/{alert_id}/acknowledge", json={"kind": "on_my_way"})
    assert r.status_code == 200, r.text
    state = (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()
    assert (state["state"], state["responder_name"]) == ("on_the_way", "Sravani")
    r = await client.post(f"{C}/alerts/{alert_id}/acknowledge", json={"kind": "arrived"})
    assert r.status_code == 200, r.text
    state = (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()
    assert (state["state"], state["responder_name"]) == ("arrived", "Sravani")
    r = await client.post(f"{C}/alerts/{alert_id}/resolve", json={"resolution": "Visited; all well.", "outcome": "all_well"})
    assert r.status_code == 200
    state = (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()
    assert state["state"] == "closed" and state["alert_id"] == alert_id


async def test_the_stream_pushes_each_change_to_the_watch(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    seen: list[dict] = []

    async def consume():
        async for item in sos.stream(p["jp"], p["device"], poll_s=0.05, max_seconds=6):
            if "state" in item:
                seen.append(item)
            if item.get("state") == "on_the_way":
                break
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    alert_id = r.json()["id"]
    await asyncio.sleep(0.3)
    client.act_as(fam, "member", "member")
    await client.post(f"{C}/alerts/{alert_id}/acknowledge", json={"kind": "on_my_way"})
    await asyncio.wait_for(task, timeout=8)
    assert [s["state"] for s in seen] == ["idle", "received", "on_the_way"], seen
    assert seen[-1]["responder_name"] == "Sravani"


async def test_the_stream_route_answers_with_the_current_state_first(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    import json as _json
    async with client.http.stream("GET", f"{C}/devices/me/sos/stream", headers=p["h"]) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        async for line in r.aiter_lines():
            if line.strip():
                assert _json.loads(line)["state"] == "received"
                break


async def test_a_notice_is_not_an_sos_for_the_wrist(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute(
            "INSERT INTO care_alerts (person_id, kind, severity, status, details) VALUES ($1, 'low_battery', 'notice', 'open', '{}')",
            p["device"]["person_id"])
    assert (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()["state"] == "idle"


# ── The watch holds the range and the upload interval ────────────────────────

async def test_the_watch_is_told_the_carers_range_and_the_upload_interval(client, families):
    fam = families["a"]
    p = await _setup(client, fam, hr=(50, 120))
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["readings"] == {"heart_rate_min": 50, "heart_rate_max": 120, "reading_upload_interval_s": 60}
    client.act_as(fam, "admin", "workspace_admin")
    s = (await client.get(f"{C}/people/{p['id']}/settings")).json()
    assert s["reading_upload_interval_s"] == 60
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["reading_upload_interval_s"] = 30
    r = await client.put(f"{C}/people/{p['id']}/settings", json=body)
    assert r.status_code == 200, r.text
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["readings"]["reading_upload_interval_s"] == 30
    r = await client.put(f"{C}/people/{p['id']}/settings", json=dict(body, reading_upload_interval_s=5))
    assert r.status_code == 422, "the floor is 15 seconds"


async def test_every_sample_keeps_its_own_time_and_arrival_is_stamped(client, families):
    """Many samples a few seconds apart are all stored with their sample time,
    and the server stamps when each arrived."""
    fam = families["a"]
    p = await _setup(client, fam)
    t0 = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    readings = [{"kind": "heart_rate", "value": 70 + i, "unit": "bpm", "recorded_at": (t0 + dt.timedelta(seconds=5 * i)).isoformat()} for i in range(12)]
    r = await client.post(f"{C}/devices/me/report", json={"readings": readings, "on_wrist": True}, headers=p["h"])
    assert r.status_code == 200 and r.json()["stored"] == 12
    client.act_as(fam, "member", "member")
    v = (await client.get(f"{C}/people/{p['id']}/vitals", params={"kind": "heart_rate", "limit": 50})).json()
    assert v["count"] == 12
    times = sorted(x["recorded_at"] for x in v["vitals"])
    assert len(set(times)) == 12
    assert all(x["received_at"] is not None for x in v["vitals"])


async def test_a_question_already_out_stops_a_second_alert_of_the_same_kind(client, families):
    """Found by the sample-to-alert measurement: with "Are you OK?" asking for a
    heart rate outside the range, a missed ordinary check-in that saw the same
    reading opened a second SOS of the same kind. One event, one alert."""
    import datetime as _dt
    from care import escalation, providers
    from care import repository as repo
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    now = _dt.datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
    report = {"readings": [{"kind": "heart_rate", "value": v, "unit": "bpm", "recorded_at": now + _dt.timedelta(seconds=60 * i)}
                           for i, v in enumerate((160, 165, 170))], "battery_percent": 70, "on_wrist": True,
              "background_sensor_permission": True, "now": now + _dt.timedelta(seconds=120)}
    out = await repo.record_report(jp, device, report)
    assert out["safety_check"]["kind"] == "heart_rate_outside_range"
    # An ordinary check-in is missed while the question is out, with the same readings on record.
    async with workspace_connection(jp) as conn:
        await conn.execute(
            "INSERT INTO care_checkins (person_id, scheduled_at, asked_at, status, question_kind, question_text, answer_summary) "
            "VALUES ($1, $2, $2, 'missed', 'wellbeing', 'How are you?', 'No answer.')",
            device["person_id"], now + _dt.timedelta(seconds=125))
    outcome = await escalation.run_workspace(jp, now + _dt.timedelta(seconds=130), provider=providers.LogOnlyProvider())
    assert outcome.alerts_opened == 0, "the asking alert already covers this kind"
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"person_id": p["id"], "open_only": "false"})).json()["alerts"]
    assert [a["kind"] for a in alerts] == ["heart_rate_outside_range"]


async def test_pressing_help_while_asked_settles_the_question(client, families):
    """Found by the staging proof (round 3, phase 14): after Help during "Are you OK?",
    the watch was offered the same question again. Help is the answer: the alert opens,
    the safety check-in is recorded as answered by the button, and nothing is due."""
    import datetime as _dt
    from care import repository as repo
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    now = _dt.datetime(2026, 11, 23, 14, 0, tzinfo=UTC)
    report = {"readings": [{"kind": "heart_rate", "value": v, "unit": "bpm", "recorded_at": now + _dt.timedelta(seconds=60 * i)}
                           for i, v in enumerate((160, 165, 170))], "battery_percent": 70, "on_wrist": True,
              "background_sensor_permission": True, "now": now + _dt.timedelta(seconds=120)}
    out = await repo.record_report(jp, device, report)
    assert out["safety_check"]["kind"] == "heart_rate_outside_range"
    assert (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()["checkin"] is not None
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={**p["h"], sos.IDEMPOTENCY_HEADER: "help-while-asked"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "open" and r.json()["sos"]["state"] == "received"
    assert (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()["checkin"] is None
    client.act_as(fam, "member", "member")
    checkins = (await client.get(f"{C}/people/{p['id']}/checkins", params={"limit": 10})).json()["checkins"]
    safety = [c for c in checkins if c["question_kind"] == "safety"]
    assert safety and safety[0]["status"] == "answered"
    async with workspace_connection(jp) as conn:
        via = await conn.fetchval("SELECT answered_via FROM care_checkins WHERE id = $1", uuid.UUID(safety[0]["id"]))
    assert via == "button"
