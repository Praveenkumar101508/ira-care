"""Research heart readings never become a person's vital sign and never raise an alert (D-086).

Against a real PostgreSQL as the runtime role:

* the readings table refuses both research sources (`care_vitals.source` CHECK), and accepts
  the same row from the watch (negative control);
* a care manager runs the research pipeline, through the API, on a simulated signal far outside
  the person's heart-rate range and on the PPG-DaLiA excerpt: no reading, alert or access-log
  row is written, and the arrival rule and the escalation run find nothing;
* negative control: the same values sent by the person's paired watch do open a safety check,
  so the rule the research results are kept away from is live in this database.
"""
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

from app.middleware import limiter  # noqa: E402
from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, heart_research, providers, repository as repo  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _setup(client, fam, *, hr=(45, 130)) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess",
                                                      "timezone": "UTC"})).json()
    s = (await client.get(f"{C}/people/{person['id']}/settings")).json()
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["heart_rate_min"], body["heart_rate_max"] = hr
    assert (await client.put(f"{C}/people/{person['id']}/settings", json=body)).status_code == 200
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    device = dict(paired)
    device["person_id"] = uuid.UUID(person["id"])
    device["id"] = uuid.UUID(paired["id"])
    person["device"] = device
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def _counts(tenant_id) -> dict:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        return {t: await admin.fetchval(f"SELECT count(*) FROM {t} WHERE tenant_id = $1", tenant_id)  # noqa: S608
                for t in ("care_vitals", "care_alerts", "care_access_log", "care_checkins")}
    finally:
        await admin.close()


@pytest.mark.parametrize("source", ["research_recording", "simulated"])
async def test_the_readings_table_refuses_research_sources(client, families, source):
    fam = families["a"]
    p = await _setup(client, fam)
    insert = ("INSERT INTO care_vitals (person_id, device_id, kind, value, unit, recorded_at, source) "
              "VALUES ($1, $2, 'heart_rate', 72, 'bpm', $3, $4)")
    at = dt.datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    async with workspace_connection(p["jp"]) as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, p["device"]["person_id"], p["device"]["id"], at, source)
    async with workspace_connection(p["jp"]) as conn:       # negative control: the watch's row is accepted
        await conn.execute(insert, p["device"]["person_id"], p["device"]["id"], at, "watch")
    assert (await _counts(fam["tenant_id"]))["care_vitals"] == 1


async def test_a_research_run_writes_nothing_and_raises_nothing(client, families):
    limiter.reset()
    fam = families["a"]
    p = await _setup(client, fam)
    before = await _counts(fam["tenant_id"])
    client.act_as(fam, "admin", "workspace_admin")
    fast = heart_research.simulate(180.0, 60.0, seed=7)        # far above the person's 45-130 range
    r = await client.post(f"{C}/research/heart/run", json={"recording": fast})
    assert r.status_code == 200, r.text
    d = r.json()
    high = [w["bpm"] for w in d["windows"] if w["status"] == "estimate"]
    assert len(high) >= 20 and all(v > 130 for v in high), "the research result itself is out of range"
    assert (d["source"], d["is_measurement"], d["may_alert"], d["stored"]) == ("simulated", False, False, False)
    r = await client.post(f"{C}/research/heart/run", json={"sample": "ppg_dalia_s1_excerpt"})
    assert r.status_code == 200 and r.json()["source"] == "research_recording"
    assert await _counts(fam["tenant_id"]) == before, "no reading, alert, access-log or check-in row"
    now = dt.datetime.now(UTC)
    async with workspace_connection(p["jp"]) as conn:
        assert await repo.evaluate_arrivals(conn, p["device"]["person_id"], now=now) is None
    run = await escalation.run_workspace(p["jp"], now + dt.timedelta(minutes=5), provider=providers.LogOnlyProvider())
    assert run.promoted == 0 and run.tiers_run == 0
    assert await _counts(fam["tenant_id"]) == before
    limiter.reset()


async def test_negative_control_the_same_values_from_the_watch_do_ask(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    start = dt.datetime.now(UTC) - dt.timedelta(minutes=3)
    report = {"readings": [{"kind": "heart_rate", "value": 180.0, "unit": "bpm",
                            "recorded_at": start + dt.timedelta(seconds=60 * i)} for i in range(3)],
              "now": start + dt.timedelta(seconds=120)}
    out = await repo.record_report(p["jp"], p["device"], report)
    assert out["safety_check"]["kind"] == "heart_rate_outside_range"
    assert (await _counts(fam["tenant_id"]))["care_alerts"] == 1
