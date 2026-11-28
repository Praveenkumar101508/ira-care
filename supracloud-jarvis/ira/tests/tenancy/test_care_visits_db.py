"""Scheduled care visits (109, phase 6; care/visits.py).

Real HTTP stack, real PostgreSQL as `ira_app`, a fake push provider, the
server's clock moved by hand. Covers: planning, what the watch hears, late and
missed on the monitoring pass, the extra check-in, arrival pausing check-ins
and closing the visit alert, the record of the visit, tenancy, and the
rollback's refusal to drop records of care given.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401
from tests.tenancy.test_care_follow_up_db import _setup  # noqa: E402
from tests.tenancy.test_care_push_db import TOKEN_A, FakePush  # noqa: E402

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


def _window(start: dt.datetime, hours: int = 1) -> dict:
    return {"planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=hours)).isoformat()}


async def _plan(client, fam, p, start, **extra) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    body = {**_window(start), "carer_member_id": p["member"]["id"], "purpose": "Morning visit",
            "tasks": ["Breakfast", "Medication"], **extra}
    r = await client.post(f"{C}/people/{p['id']}/visits", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _pass(p, at, fake=None):
    fake = fake or FakePush()
    out = await escalation.run_workspace(p["jp"], at, provider=providers.LogOnlyProvider(), push_provider=fake)
    return out, fake


async def test_a_planned_visit_is_what_the_watch_hears(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    start = dt.datetime.now(UTC) + dt.timedelta(hours=2)
    v = await _plan(client, fam, p, start)
    assert v["status"] == "planned" and v["carer_name"] == "Sravani" and [t["title"] for t in v["tasks"]] == ["Breakfast", "Medication"]
    sos = (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()
    assert sos["state"] == "idle"
    assert (sos["visit"]["state"], sos["visit"]["carer_name"], sos["visit"]["visit_id"]) == ("expected", "Sravani", v["id"])
    # A window that ends before it starts, or a window longer than a day, is refused.
    r = await client.post(f"{C}/people/{p['id']}/visits", json={"planned_start": start.isoformat(), "planned_end": start.isoformat()})
    assert r.status_code == 422
    r = await client.post(f"{C}/people/{p['id']}/visits", json={"planned_start": start.isoformat(),
                                                                "planned_end": (start + dt.timedelta(hours=30)).isoformat()})
    assert r.status_code == 422


async def test_no_arrival_means_late_then_missed_with_an_extra_checkin_each_time(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    # The coordinator sets lateness at 15 minutes explicitly (the default), through the settings route,
    # so the test also proves the setting is saved and read.
    client.act_as(fam, "admin", "workspace_admin")
    s = (await client.get(f"{C}/people/{p['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["visit_late_minutes"] = 20
    r = await client.put(f"{C}/people/{p['id']}/settings", json=body)
    assert r.status_code == 200 and r.json()["visit_late_minutes"] == 20, r.text
    body["visit_late_minutes"] = 15
    assert (await client.put(f"{C}/people/{p['id']}/settings", json=body)).json()["visit_late_minutes"] == 15
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=5)   # the window opened five minutes ago
    v = await _plan(client, fam, p, start)
    # 14 minutes in: trusted. The setting is 15 minutes.
    out, fake = await _pass(p, start + dt.timedelta(minutes=14))
    assert (out.visits_late, out.visits_missed) == (0, 0)
    # 15 minutes in: late. A notice reaches the coordinator's phone; IRA asks the person how they are.
    out, fake = await _pass(p, start + dt.timedelta(minutes=15))
    assert (out.visits_late, out.visits_missed) == (1, 0)
    assert fake.tokens == [TOKEN_A] and "late" in fake.messages[0]["title"]
    alerts = (await client.get(f"{C}/alerts", params={"person_id": p["id"], "open_only": "true"})).json()["alerts"]
    assert [a["kind"] for a in alerts] == ["visit_late"] and alerts[0]["severity"] == "notice"
    assert (await client.get(f"{C}/visits/{v['id']}")).json()["status"] == "late"
    async with workspace_connection(p["jp"]) as conn:
        due = await conn.fetch("SELECT question_text, status FROM care_checkins WHERE person_id = $1 AND status = 'scheduled'",
                               uuid.UUID(p["id"]))
    assert len(due) == 1 and "carer has not arrived" in due[0]["question_text"]
    # The watch still says the carer is coming: lateness is the coordinator's problem, not the person's.
    assert (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()["visit"]["state"] == "late"
    # A second pass changes nothing.
    out, fake = await _pass(p, start + dt.timedelta(minutes=20))
    assert (out.visits_late, out.visits_missed, fake.tokens) == (0, 0, [])
    # The window ends with nobody there: missed, urgent, and the chain starts at tier 1.
    out, fake = await _pass(p, start + dt.timedelta(hours=1))
    assert (out.visits_late, out.visits_missed) == (0, 1)
    # (An hour on, the watch has also not synced: that notice is the ordinary one, not this test's.)
    missed = [m for m in fake.messages if "did not happen" in m["body"]]
    assert len(missed) == 1 and "alert for Test Person" in missed[0]["title"] and "999" not in re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", missed[0]["body"])
    alerts = (await client.get(f"{C}/alerts", params={"person_id": p["id"], "open_only": "true"})).json()["alerts"]
    kinds = [a["kind"] for a in alerts]
    assert "visit_missed" in kinds and "visit_late" not in kinds
    missed_alert = next(a for a in alerts if a["kind"] == "visit_missed")
    assert missed_alert["severity"] == "urgent" and "did not happen" in missed_alert["details"]["summary"]
    assert (await client.get(f"{C}/visits/{v['id']}")).json()["status"] == "missed"
    assert (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()["visit"]["state"] == "none"


async def test_an_arrival_closes_the_visit_alert_pauses_checkins_and_the_departure_is_the_record(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=20)
    v = await _plan(client, fam, p, start)
    out, _ = await _pass(p, dt.datetime.now(UTC))
    assert out.visits_late == 1
    # The carer arrives (from the phone, as the circle member).
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/visits/{v['id']}/arrive")
    assert r.status_code == 200 and r.json()["status"] == "arrived" and r.json()["arrived_at"] is not None
    assert (await client.post(f"{C}/visits/{v['id']}/arrive")).json()["status"] == "arrived", "idempotent"
    alerts = (await client.get(f"{C}/alerts", params={"person_id": p["id"], "open_only": "false"})).json()["alerts"]
    late = [a for a in alerts if a["kind"] == "visit_late"]
    assert late and late[0]["status"] == "resolved" and late[0]["outcome"] == "all_well"
    assert (await client.get(f"{C}/devices/me/sos", headers=p["h"])).json()["visit"]["state"] == "here"
    # The watch vibrated and showed "Sravani is here"; the person saw it. Once.
    r = await client.post(f"{C}/devices/me/visits/{v['id']}/acknowledge", headers=p["h"])
    assert r.status_code == 200 and r.json()["acknowledged_at"] is not None
    first = r.json()["acknowledged_at"]
    assert (await client.post(f"{C}/devices/me/visits/{v['id']}/acknowledge", headers=p["h"])).json()["acknowledged_at"] == first
    assert (await client.post(f"{C}/devices/me/visits/{uuid.uuid4()}/acknowledge", headers=p["h"])).status_code == 404
    # A due check-in waits while the carer is in the room.
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", uuid.UUID(p["id"]))
        await conn.execute("INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind) VALUES ($1, $2, 'scheduled', 'wellbeing')",
                           uuid.UUID(p["id"]), dt.datetime.now(UTC) - dt.timedelta(minutes=1))
    now = dt.datetime.now(UTC)
    co = await cycle.run_workspace(p["jp"], now)
    assert co.paused_for_visit == 1
    async with workspace_connection(p["jp"]) as conn:
        row = await conn.fetchrow("SELECT scheduled_at, status FROM care_checkins WHERE person_id = $1 AND status = 'scheduled'", uuid.UUID(p["id"]))
    assert row["scheduled_at"] >= now + dt.timedelta(minutes=9)
    due = (await client.get(f"{C}/devices/me/checkins/due", headers=p["h"])).json()
    assert not due.get("checkin") and not due.get("checkins"), due
    # Leaving: the outcome, the handover and the tasks done are the record.
    tasks = (await client.get(f"{C}/visits/{v['id']}")).json()["tasks"]
    r = await client.post(f"{C}/visits/{v['id']}/depart", json={"outcome": "concern_noted", "notes": "Seemed tired.",
                                                                "handover": "Please check the evening dose.", "tasks_done": [tasks[0]["id"]]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "departed" and d["outcome"] == "concern_noted" and d["handover"] == "Please check the evening dose."
    assert [t["done"] for t in d["tasks"]] == [True, False]
    # Nothing moves it afterwards; a departure needs an arrival first.
    r = await client.patch(f"{C}/visits/{v['id']}", json={"purpose": "changed"})
    assert r.json()["purpose"] == "Morning visit"
    v2 = await _plan(client, fam, p, dt.datetime.now(UTC) + dt.timedelta(hours=3))
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/visits/{v2['id']}/depart", json={"outcome": "all_well"})
    assert r.status_code == 409
    listed = (await client.get(f"{C}/people/{p['id']}/visits")).json()["visits"]
    assert [x["status"] for x in listed] == ["planned", "departed"]
    # And the next scheduled check-in is asked again once the carer has left.
    co = await cycle.run_workspace(p["jp"], dt.datetime.now(UTC) + dt.timedelta(minutes=11))
    assert co.paused_for_visit == 0


async def test_visits_stay_inside_their_workspace_and_the_rollback_keeps_the_record(client, families):
    fam, other = families["a"], families["b"]
    p = await _setup(client, fam)
    v = await _plan(client, fam, p, dt.datetime.now(UTC) + dt.timedelta(hours=1))
    client.act_as(other, "admin", "workspace_admin")
    assert (await client.get(f"{C}/visits/{v['id']}")).status_code == 404
    assert (await client.post(f"{C}/visits/{v['id']}/arrive")).status_code == 404
    assert (await client.get(f"{C}/people/{p['id']}/visits")).status_code == 404
    # The rollback refuses while a visit records an arrival or an outcome.
    client.act_as(fam, "member", "member")
    assert (await client.post(f"{C}/visits/{v['id']}/arrive")).status_code == 200
    down = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "postgres", "rollback", "109_care_visits_down.sql")).read()
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        with pytest.raises(asyncpg.PostgresError, match="Refusing rollback"):
            await admin.execute(down)
        await admin.execute("ROLLBACK")  # the script's own transaction, left aborted by the refusal
        assert await admin.fetchval("SELECT to_regclass('care_visits') IS NOT NULL")
    finally:
        await admin.close()
