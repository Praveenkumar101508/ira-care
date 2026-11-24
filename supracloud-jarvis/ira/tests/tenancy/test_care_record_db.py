"""The live overview and the care record (phase 11). Real HTTP stack, real PostgreSQL."""
from __future__ import annotations

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
from tests.tenancy.test_care_follow_up_db import _setup  # noqa: E402

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def test_the_overview_names_who_needs_attention_and_why(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    ov = (await client.get(f"{C}/overview")).json()
    assert ov["people"] >= 1 and ov["alerts"] == [] and ov["visits"] == []
    # A late visit, an SOS nobody has answered, and a watch that has not synced.
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=30)
    r = await client.post(f"{C}/people/{p['id']}/visits", json={"planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=1)).isoformat(),
                                                                "carer_name": "Sravani"})
    assert r.status_code == 201
    await escalation.run_workspace(p["jp"], dt.datetime.now(UTC), provider=providers.LogOnlyProvider())
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    assert r.status_code == 201
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_devices SET last_sync_at = $1, battery_percent = 9 WHERE person_id = $2",
                           dt.datetime.now(UTC) - dt.timedelta(hours=3), uuid.UUID(p["id"]))
    ov = (await client.get(f"{C}/overview")).json()
    me = next(x for x in ov["attention"] if x["person_id"] == p["id"])
    assert "an alert is waiting for someone" in me["reasons"] and "a visit is late or did not happen" in me["reasons"]
    v = next(x for x in ov["visits"] if x["person_id"] == p["id"])
    assert v["state"] == "overdue" and v["status"] == "late"
    sos = [a for a in ov["alerts"] if a["person_id"] == p["id"] and a["kind"] == "help_requested"][0]
    assert sos["awaiting_response"] and sos["responder"] is None
    w = next(x for x in ov["stale_watches"] if x["person_id"] == p["id"])
    assert any("not synced" in r for r in w["reasons"]) and "battery 9 %" in w["reasons"]
    # A responder on the way: the alert is no longer awaiting a response; past the deadline it is overdue.
    client.act_as(fam, "member", "member")
    assert (await client.post(f"{C}/alerts/{sos['id']}/acknowledge", json={"kind": "on_my_way"})).status_code == 200
    client.act_as(fam, "admin", "workspace_admin")
    sos2 = [a for a in (await client.get(f"{C}/overview")).json()["alerts"] if a["id"] == sos["id"]][0]
    assert not sos2["awaiting_response"] and sos2["responder"] == "Sravani" and not sos2["follow_up_overdue"]
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_alerts SET follow_up_due_at = $1 WHERE id = $2", dt.datetime.now(UTC) - dt.timedelta(minutes=1), uuid.UUID(sos["id"]))
    ov = (await client.get(f"{C}/overview")).json()
    assert [a for a in ov["alerts"] if a["id"] == sos["id"]][0]["follow_up_overdue"]
    assert "a responder said they were coming and has not arrived" in next(x for x in ov["attention"] if x["person_id"] == p["id"])["reasons"]
    # Another family's overview knows nothing of it.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert all(x["person_id"] != p["id"] for x in (await client.get(f"{C}/overview")).json()["attention"])


async def test_entry_instructions_on_the_profile_are_for_managers_and_every_reading_is_logged(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.patch(f"{C}/people/{p['id']}", json={"home_address": "1 Example Street, Exampletown", "access_notes": "Key safe by the door, code with the family. Small dog."})
    if r.status_code == 503:
        pytest.skip("no sealing key configured in this environment")
    assert r.status_code == 200, r.text
    r = await client.get(f"{C}/people/{p['id']}/access-notes")
    assert r.status_code == 200 and "Key safe" in r.json()["access_notes"]
    # A family member is refused on the profile (during an alert, the alert's own route applies).
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{p['id']}/access-notes")).status_code == 403
    client.act_as(fam, "admin", "workspace_admin")
    log = (await client.get(f"{C}/people/{p['id']}/access-log")).json()["entries"]
    assert any(e["resource_kind"] == "access_notes" and e["detail"].get("from") == "profile" for e in log)


async def test_the_daily_report_carries_visits_reminders_and_what_ira_remembers(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    now = dt.datetime.now(UTC)
    # Everything the report must carry happens today, whatever the wall clock says (the test person's day is UTC).
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(now - dt.timedelta(hours=2), midnight + dt.timedelta(minutes=1))
    reminder_due = max(now - dt.timedelta(minutes=30), midnight + dt.timedelta(minutes=2))
    v = (await client.post(f"{C}/people/{p['id']}/visits", json={"planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=1)).isoformat(),
                                                                 "carer_name": "Sravani", "tasks": ["Breakfast", "Medication"]})).json()
    client.act_as(fam, "member", "member")
    await client.post(f"{C}/visits/{v['id']}/arrive")
    r = await client.post(f"{C}/visits/{v['id']}/depart", json={"outcome": "all_well", "handover": "Ate well, in good spirits.", "tasks_done": [v["tasks"][0]["id"]]})
    assert r.status_code == 200, r.text
    r = await client.post(f"{C}/people/{p['id']}/reminders", json={"text": "ring Sravani", "due_at": reminder_due.isoformat()})
    assert r.status_code == 201
    await client.post(f"{C}/reminders/{r.json()['id']}/act", json={"action": "confirm"})
    await client.post(f"{C}/people/{p['id']}/talk", json={"text": "I had a lovely morning in the garden"})
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/reports/generate", json={"kind": "daily_carer", "date": now.date().isoformat()})
    assert r.status_code in (200, 201), r.text
    rep = r.json()
    text = rep["body_text"]
    assert "Visits: 1 planned, 1 done" in text and "1 of 2 planned tasks done" in text and "Handover from Sravani: Ate well" in text
    assert "Reminders: 1 due, 1 confirmed" in text
    assert "What IRA remembers (summary by IRA" in text
    assert "lovely morning" not in text, "the person's words never appear in a report"
    assert rep["body"]["visits"]["done"] == 1 and rep["body"]["reminders"]["confirmed"] == 1
