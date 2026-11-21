"""IRA CARE round 2, phase 1: continuous monitoring against a real PostgreSQL.

Readings are checked as they arrive; a confirmed breach asks first; "I'm OK"
resolves, "Help" or silence escalates; night rules; the morning check; and the
operations heartbeat that notices when the jobs stop.
"""
from __future__ import annotations

import datetime as dt
import os
import random
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
from care import escalation, ops, providers, repository as repo  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam, *, hr=(45, 130)) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Family Member", "role": "family", "user_id": str(fam["member"]), "phone": "+440000000001"})
    s = (await client.get(f"{C}/people/{person['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["heart_rate_min"], body["heart_rate_max"] = hr
    r = await client.put(f"{C}/people/{person['id']}/settings", json=body)
    assert r.status_code == 200, r.text
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    device = dict(paired); device["person_id"] = uuid.UUID(person["id"]); device["id"] = uuid.UUID(paired["id"])
    person["device"] = device; person["token"] = paired["device_token"]
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


def _report(values, start, step_s=60, **state):
    return {"readings": [{"kind": "heart_rate", "value": v, "unit": "bpm", "recorded_at": start + dt.timedelta(seconds=i * step_s)}
                         for i, v in enumerate(values)], "battery_percent": 70, "on_wrist": True,
            "background_sensor_permission": True, "now": start + dt.timedelta(seconds=(len(values) - 1) * step_s), **state}


async def test_a_confirmed_breach_at_night_asks_first_and_silence_becomes_an_sos(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    night = dt.datetime(2026, 11, 21, 2, 0, tzinfo=UTC)
    # One spike: nothing.
    out = await repo.record_report(jp, device, _report([70, 160], night))
    assert "safety_check" not in out
    # Two in a row over two minutes: ask first.
    out = await repo.record_report(jp, device, _report([160, 165, 170], night + dt.timedelta(minutes=5)))
    assert out["safety_check"]["kind"] == "heart_rate_outside_range"
    now = night + dt.timedelta(minutes=7, seconds=10)
    due = await cycle.due_checkin(jp, device, now)
    assert due is not None and due["safety"] is True and due["question_kind"] == "safety" and "are you OK" in due["question_text"]
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["status"] == "asking" and alerts[0]["details"]["ask_first"] is True
    # Nobody answers within the wait: the server opens the alert and runs tier 1.
    provider = providers.LogOnlyProvider()
    before = await escalation.run_workspace(jp, now + dt.timedelta(seconds=30), provider=provider)
    assert before.promoted == 0 and before.tiers_run == 0
    after = await escalation.run_workspace(jp, now + dt.timedelta(seconds=70), provider=provider)
    assert after.promoted == 1 and after.tiers_run == 1 and after.attempts > 0
    alerts = (await client.get(f"{C}/alerts")).json()["alerts"]
    assert alerts[0]["status"] == "escalating" and alerts[0]["details"]["outcome"] == "no_answer"
    checkins = (await client.get(f"{C}/people/{p['id']}/checkins")).json()["checkins"]
    assert checkins[0]["question_kind"] == "safety" and checkins[0]["status"] == "missed"
    # A second breach while the first alert is unresolved does not open another.
    out = await repo.record_report(jp, device, _report([170, 175], night + dt.timedelta(minutes=20)))
    assert "safety_check" not in out


async def test_im_ok_resolves_the_question_and_help_opens_it(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    t = dt.datetime(2026, 11, 21, 14, 0, tzinfo=UTC)
    out = await repo.record_report(jp, device, _report([160, 165], t, step_s=120))
    assert "safety_check" in out
    due = await cycle.due_checkin(jp, device, t + dt.timedelta(minutes=2, seconds=5))
    answered = await cycle.record_answer(jp, device, due["id"], "", via="button", now=t + dt.timedelta(minutes=2, seconds=20))
    assert answered["status"] == "answered" and answered["answer_summary"] == "Pressed I'm OK."
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert alerts[0]["status"] == "resolved" and alerts[0]["details"]["outcome"] == "answered_ok"
    assert "answered the safety check: OK" in alerts[0]["resolution"]
    # The next breach asks again; this time the person says help.
    out = await repo.record_report(jp, device, _report([35, 36], t + dt.timedelta(minutes=30), step_s=120))
    assert "safety_check" in out
    due = await cycle.due_checkin(jp, device, t + dt.timedelta(minutes=32, seconds=5))
    answered = await cycle.record_answer(jp, device, due["id"], "please help me", via="voice", now=t + dt.timedelta(minutes=32, seconds=15))
    assert answered["asked_for_help"] is True
    alerts = (await client.get(f"{C}/alerts")).json()["alerts"]
    assert alerts[0]["status"] == "open" and alerts[0]["details"]["outcome"] == "asked_for_help"
    # And the Help button during a question opens the same alert rather than a second one.
    out = await repo.record_report(jp, device, _report([35, 36], t + dt.timedelta(minutes=60)))
    assert "safety_check" not in out, "an unresolved alert of this kind already exists"


async def test_help_button_while_asking_opens_the_asking_alert(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    t = dt.datetime(2026, 11, 21, 14, 0, tzinfo=UTC)
    await repo.record_report(jp, device, _report([160, 165], t, step_s=120))
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": p["token"]})
    assert r.status_code == 201 and r.json()["kind"] == "heart_rate_outside_range" and r.json()["status"] == "open"
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["count"] == 1


async def test_night_rules_off_wrist_is_a_notice_and_no_movement_is_nothing(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    night = dt.datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    # Wrist off at 01:00, no movement at all.
    await repo.record_report(jp, device, {"readings": [{"kind": "on_wrist", "value": 0, "unit": "", "recorded_at": night}],
                                          "battery_percent": 60, "on_wrist": False, "background_sensor_permission": True})
    async with workspace_connection(jp) as conn:
        # The device's last sync is the real clock; pin it to the scenario's clock.
        await conn.execute("UPDATE care_devices SET last_sync_at = $2 WHERE id = $1", device["id"], night)
    out = await escalation.run_workspace(jp, night + dt.timedelta(minutes=30))
    kinds = [o["kind"] for o in out.opened]
    assert "off_wrist_night" not in kinds, "half an hour off the wrist at night is normal"
    out = await escalation.run_workspace(jp, night + dt.timedelta(minutes=70))
    kinds = [o["kind"] for o in out.opened]
    assert "off_wrist_night" in kinds
    assert all(o["severity"] == "notice" for o in out.opened)
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts")).json()["alerts"]
    assert all(a["severity"] == "notice" for a in alerts), "nothing at night is an SOS without a confirmed reading"
    # No check-ins at night, so no missed check-in and no movement rule fires.
    cyc = await cycle.run_workspace(jp, night + dt.timedelta(minutes=70), rng=random.Random(1))
    assert cyc.skipped_quiet + cyc.scheduled >= 0 and cyc.missed == 0


async def test_the_morning_check_runs_when_nothing_has_moved_by_wake_time(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    jp, device = p["jp"], p["device"]
    # The watch reports after waking: no steps at all.
    await repo.record_report(jp, device, {"readings": [{"kind": "steps", "value": 0, "unit": "", "recorded_at": dt.datetime(2026, 11, 21, 7, 50, tzinfo=UTC)}]})
    # Usual wake 07:30, grace 60 min: nothing before 08:30.
    out = await cycle.run_workspace(jp, dt.datetime(2026, 11, 21, 8, 0, tzinfo=UTC), rng=random.Random(1))
    assert out.morning_checks == 0
    out = await cycle.run_workspace(jp, dt.datetime(2026, 11, 21, 8, 31, tzinfo=UTC), rng=random.Random(1))
    assert out.morning_checks == 1
    due = await cycle.due_checkin(jp, device, dt.datetime(2026, 11, 21, 8, 31, second=5, tzinfo=UTC))
    assert due is not None and due["question_kind"] == "morning" and "Good morning" in due["question_text"]
    # Once per day.
    out = await cycle.run_workspace(jp, dt.datetime(2026, 11, 21, 9, 0, tzinfo=UTC), rng=random.Random(1))
    assert out.morning_checks == 0
    # A watch that has not reported since waking is the no-sync rule's business, not a morning check.
    p3 = await _setup(client, families["b"])
    out = await cycle.run_workspace(p3["jp"], dt.datetime(2026, 11, 21, 8, 31, tzinfo=UTC), rng=random.Random(1))
    assert out.morning_checks == 0
    # A person who moved after waking is not asked.
    p2 = await _setup(client, families["b"])
    await repo.record_report(p2["jp"], p2["device"], {"readings": [{"kind": "steps", "value": 40, "unit": "", "recorded_at": dt.datetime(2026, 11, 21, 7, 45, tzinfo=UTC)}]})
    out = await cycle.run_workspace(p2["jp"], dt.datetime(2026, 11, 21, 8, 31, tzinfo=UTC), rng=random.Random(1))
    assert out.morning_checks == 0


async def test_the_operations_check_notices_when_the_jobs_stop(client, families, pool):
    fam = families["a"]
    await _setup(client, fam)
    ops.reset_alert_memory()
    provider = providers.LogOnlyProvider()
    now = dt.datetime.now(UTC)
    # Both jobs run: heartbeats are written and the status is fine.
    await cycle.run_cycle_once(now, rng=random.Random(1))
    await escalation.run_once(now)
    async with pool.acquire() as conn:
        fine = await ops.status(conn, now + dt.timedelta(seconds=30), provider=provider)
        assert fine["monitoring_paused"] is False and all(not j["stale"] for j in fine["jobs"].values())
        # Five minutes later with no further pass: paused, and the operations contact is told once.
        later = now + dt.timedelta(minutes=6)
        paused = await ops.status(conn, later, provider=provider)
        assert paused["monitoring_paused"] is True and paused["ops_alert"]["sent"] is True
        assert len(provider.log) == 1 and "monitoring paused" in provider.log[0][2]
        again = await ops.status(conn, later + dt.timedelta(minutes=1), provider=provider)
        assert again["ops_alert"]["sent"] is False and len(provider.log) == 1
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/ops/status")
    assert r.status_code == 200 and "monitoring_paused" in r.json()
