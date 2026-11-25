"""IRA Care Activity end to end, against a real PostgreSQL (F7, D-084).

The watch's hourly summaries go in through the device-authenticated endpoint, only under the
person's `activity_sharing` consent; carers read a daily and weekly trend through a person-level
route; retention and erasure reach the rows; and nothing about it can open an alert.

Input is SIMULATED: the backend's reference estimator (`care/activity_estimate.py`, the model the
watch runs, byte for byte) classifies synthetic wrist signals, and the windows are summarised
into hours as the watch does (`ActivitySummariser`). No dataset content and no real person.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import numpy as np
import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import activity, escalation, providers, retention  # noqa: E402
from care import activity_estimate as est  # noqa: E402
from care import checkin_cycle as cycle  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
VERSION, SHA, CLASSES = activity.known_model()


# ── simulated input: the reference estimator on synthetic wrist signals ──────

def _wrist(kind: str, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Ten seconds at 20 Hz: a still wrist, or a swinging one (steps and arm swing)."""
    rng = np.random.default_rng(seed)
    t = np.arange(200) / est.FS_HZ
    if kind == "still":
        acc = np.column_stack([0.2 + 0.02 * rng.standard_normal(200), -0.5 + 0.02 * rng.standard_normal(200),
                               9.78 + 0.02 * rng.standard_normal(200)])
        gyro = 0.01 * rng.standard_normal((200, 3))
    else:
        acc = np.column_stack([0.3 + 2.5 * np.sin(2 * np.pi * 1.8 * t), -2.0 + 1.5 * np.sin(2 * np.pi * 0.9 * t + 0.4),
                               9.5 + 3.0 * np.sin(2 * np.pi * 1.8 * t + 1.1)]) + 0.05 * rng.standard_normal((200, 3))
        gyro = np.column_stack([1.2 * np.sin(2 * np.pi * 0.9 * t), 0.6 * np.sin(2 * np.pi * 1.8 * t),
                                0.4 * np.cos(2 * np.pi * 0.9 * t)]) + 0.02 * rng.standard_normal((200, 3))
    return acc, gyro


def _window(kind: str, seed: int) -> tuple[str, float | None, str | None]:
    """(label, confidence, reason) for one simulated window, as the watch maps it."""
    if kind == "off_wrist":
        return "unavailable", None, "off_wrist"
    if kind == "gap":
        return "unavailable", None, "sensor_gap"
    if kind == "flat":
        a = np.tile([0.0, 0.0, 9.81], (200, 1))
        r = est.estimate_activity(a, np.zeros((200, 3)))
        return "unavailable", None, "too_short" if r.reason == "too_short" else "sensor_fault"
    r = est.estimate_activity(*_wrist(kind, seed))
    if r.activity is None:
        return "unavailable", None, "too_short" if r.reason == "too_short" else "sensor_fault"
    if r.activity == "unknown":
        return "unknown", r.confidence, "low_confidence"
    return r.activity, r.confidence, None


def simulated_hour(start: dt.datetime, plan: list[tuple[str, int]], seed: int = 0) -> dict:
    """One hour as the watch's summariser would send it. `plan` is (kind, windows) in order;
    windows not planned are simply not recorded."""
    seconds: dict[str, int] = {}
    reasons: dict[str, int] = {}
    conf: list[float] = []
    n = 0
    for kind, count in plan:
        for _ in range(count):
            label, c, reason = _window(kind, seed + n)
            seconds[label] = seconds.get(label, 0) + 10
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 10
            if c is not None:
                conf.append(c)
            n += 1
    assert n <= 360
    return {"period_start": start.isoformat(), "period_end": (start + dt.timedelta(hours=1)).isoformat(),
            "model_version": VERSION, "model_sha256": SHA, "source": "simulated", "seconds": seconds,
            "reasons": reasons, "windows": n, "mean_confidence": round(sum(conf) / len(conf), 3) if conf else None}


def exact_hour(start: dt.datetime, seconds: dict, reasons: dict | None = None) -> dict:
    """An hour with chosen seconds, for checking the arithmetic exactly."""
    return {"period_start": start.isoformat(), "period_end": (start + dt.timedelta(hours=1)).isoformat(),
            "model_version": VERSION, "model_sha256": SHA, "source": "simulated", "seconds": seconds,
            "reasons": reasons or {}, "windows": sum(seconds.values()) // 10, "mean_confidence": 0.8}


# ── setup ────────────────────────────────────────────────────────────────────

async def _setup(client, fam, *, consent: bool) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "timezone": "UTC"})).json()
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["device_id"] = paired["id"]
    if consent:
        await _consent(client, fam, person["id"], True)
    return person


async def _consent(client, fam, pid: str, granted: bool) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{pid}/consents", json={"kind": "activity_sharing", "granted": granted,
                                                           "granted_by": "person"})
    assert r.status_code == 201, r.text
    return r.json()


def _hour(h: int, day: dt.date = dt.date(2026, 11, 24)) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h), tzinfo=UTC)


async def _count(fam, pid) -> int:
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    async with workspace_connection(jp) as conn:
        return await conn.fetchval("SELECT count(*) FROM care_activity_periods WHERE person_id = $1", uuid.UUID(str(pid)))


# ── ingestion and consent ────────────────────────────────────────────────────

async def test_without_the_consent_nothing_is_stored_and_the_watch_is_told(client, families):
    fam = families["a"]
    p = await _setup(client, fam, consent=False)
    me = (await client.get(f"{C}/devices/me", headers=p["h"])).json()
    assert me["activity"] == {"sharing": False}
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"],
                          json={"periods": [simulated_hour(_hour(9), [("swing", 30), ("still", 60)])]})
    assert r.status_code == 200 and r.json() == {"accepted": 1, "stored": 0, "sharing": False}
    assert await _count(fam, p["id"]) == 0
    # Declined is the same as never asked.
    await _consent(client, fam, p["id"], False)
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": [exact_hour(_hour(9), {"sitting": 600})]})
    assert r.json()["stored"] == 0 and await _count(fam, p["id"]) == 0


async def test_with_the_consent_hours_are_stored_once_and_revoking_stops_both_ways(client, families):
    fam = families["a"]
    p = await _setup(client, fam, consent=True)
    assert (await client.get(f"{C}/devices/me", headers=p["h"])).json()["activity"] == {"sharing": True}
    hours = [simulated_hour(_hour(9), [("swing", 120), ("still", 180), ("off_wrist", 30), ("gap", 6), ("flat", 3)], seed=1),
             simulated_hour(_hour(10), [("still", 300)], seed=2)]
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": hours})
    assert r.status_code == 200 and r.json() == {"accepted": 2, "stored": 2, "sharing": True}
    # A re-send (the watch did not hear the answer) is ignored, not doubled.
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": hours})
    assert r.json()["stored"] == 0 and await _count(fam, p["id"]) == 2
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    async with workspace_connection(jp) as conn:
        row = await conn.fetchrow("SELECT * FROM care_activity_periods WHERE period_start = $1", _hour(9))
    assert row["source"] == "simulated" and row["model_sha256"] == SHA and row["windows"] == 339
    seconds = __import__("json").loads(row["seconds"])
    reasons = __import__("json").loads(row["reasons"])
    assert seconds["unavailable"] >= 390 and reasons["off_wrist"] == 300 and reasons["sensor_gap"] == 60
    assert reasons.get("sensor_fault", 0) + reasons.get("too_short", 0) >= 30
    # Revoked: the watch is told, nothing more is stored, and carers see nothing.
    consents = (await client.get(f"{C}/people/{p['id']}/consents")).json()["consents"]
    cid = next(c["id"] for c in consents if c["kind"] == "activity_sharing")
    assert (await client.post(f"{C}/consents/{cid}/revoke")).status_code == 200
    assert (await client.get(f"{C}/devices/me", headers=p["h"])).json()["activity"] == {"sharing": False}
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": [exact_hour(_hour(11), {"sitting": 600})]})
    assert r.json() == {"accepted": 1, "stored": 0, "sharing": False}
    out = (await client.get(f"{C}/people/{p['id']}/activity")).json()
    assert out["sharing"] is False and out["days"] == [] and out["weeks"] == []


@pytest.mark.parametrize("change, why", [
    ({"model_sha256": "0" * 64}, "model"),
    ({"model_version": "ira-care-activity-mlp/2"}, "model"),
    ({"seconds": {"ate_lunch": 60}}, "label"),
    ({"seconds": {"unknown": 60}, "reasons": {}}, "low_confidence"),
    ({"seconds": {"unavailable": 60}, "reasons": {"off_wrist": 30}}, "reasons"),
    ({"seconds": {"sitting": 3610}, "windows": 360}, "more than the period"),
    ({"period_end": _hour(12).isoformat()}, "at most one hour"),
    ({"period_start": (dt.datetime.now(UTC) + dt.timedelta(days=1)).replace(minute=0, second=0, microsecond=0).isoformat(),
      "period_end": (dt.datetime.now(UTC) + dt.timedelta(days=1, hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()},
     "future"),
])
async def test_a_summary_that_breaks_the_contract_refuses_the_whole_batch(client, families, change, why):
    fam = families["a"]
    p = await _setup(client, fam, consent=True)
    good = exact_hour(_hour(8), {"walking": 600})
    bad = dict(exact_hour(_hour(9), {"sitting": 600}), **change)
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": [good, bad]})
    assert r.status_code == 422 and why in r.text
    assert await _count(fam, p["id"]) == 0


async def test_only_a_paired_watch_can_send(client, families):
    fam = families["a"]
    await _setup(client, fam, consent=True)
    body = {"periods": [exact_hour(_hour(8), {"walking": 600})]}
    assert (await client.post(f"{C}/devices/me/activity", json=body)).status_code == 401
    assert (await client.post(f"{C}/devices/me/activity", json=body, headers={"X-Device-Token": "not-a-token"})).status_code == 401


# ── the carer's trend ────────────────────────────────────────────────────────

async def test_the_daily_and_weekly_trend_groups_minutes_and_never_names_eating(client, families):
    fam = families["a"]
    p = await _setup(client, fam, consent=True)
    today = dt.datetime.now(UTC).date()
    day = today - dt.timedelta(days=1)
    hours = [
        exact_hour(_hour(9, day), {"walking": 1200, "stairs": 600, "sitting": 1200, "standing": 600}),
        exact_hour(_hour(12, day), {"eating_drinking": 1800, "other": 600, "unknown": 600, "unavailable": 600},
                   {"low_confidence": 600, "off_wrist": 600}),
    ]
    assert (await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": hours})).json()["stored"] == 2
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.get(f"{C}/people/{p['id']}/activity", params={"days": 7, "weeks": 2})
    assert r.status_code == 200
    out = r.json()
    assert out["label"] == "Estimated from the watch's motion sensor; not a measurement of health."
    assert out["sharing"] is True and out["sources"] == ["simulated"] and len(out["days"]) == 7 and len(out["weeks"]) == 2
    d = next(x for x in out["days"] if x["date"] == day.isoformat())["minutes"]
    assert (d["active"], d["still"], d["other_movement"], d["unknown"], d["unavailable"]) == (30, 30, 40, 10, 10)
    assert d["not_recorded"] == 24 * 60 - 120
    assert d["unknown_or_unavailable"] == 10 + 10 + 24 * 60 - 120
    assert out["weeks"][-1]["minutes"]["active"] == 30
    text = r.text.lower()
    for word in ("eating", "drinking", "ate", "meal", "fall", "alert"):
        assert f'"{word}' not in text and f" {word} " not in text, word
    # The read is on the record, without the numbers.
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    async with workspace_connection(jp) as conn:
        log = await conn.fetchrow("SELECT * FROM care_access_log WHERE person_id = $1 ORDER BY at DESC LIMIT 1", uuid.UUID(p["id"]))
    assert log["resource_kind"] == "vitals" and '"activity": true' in log["detail"]


async def test_only_people_who_may_see_the_person_can_read_the_trend(client, families):
    fam, other = families["a"], families["b"]
    p = await _setup(client, fam, consent=True)
    await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": [exact_hour(_hour(9), {"walking": 600})]})
    # A viewer outside the person's circle (D-076): not found.
    client.act_as(fam, "viewer", "viewer")
    assert (await client.get(f"{C}/people/{p['id']}/activity")).status_code == 404
    # Inside the circle: allowed.
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Family Viewer", "role": "family",
                                                          "user_id": str(fam["viewer"])})
    client.act_as(fam, "viewer", "viewer")
    assert (await client.get(f"{C}/people/{p['id']}/activity")).status_code == 200
    # Another organisation: not found, and its row-level security hides the rows outright.
    client.act_as(other, "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{p['id']}/activity")).status_code == 404
    jp_other = cycle.job_principal(other["tenant_id"], other["workspace_id"])
    async with workspace_connection(jp_other) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_activity_periods") == 0


# ── retention and erasure ────────────────────────────────────────────────────

async def test_retention_purges_old_hours_and_erasure_takes_the_rest(client, families):
    fam = families["a"]
    p = await _setup(client, fam, consent=True)
    pid = uuid.UUID(p["id"])
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    now = dt.datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    async with workspace_connection(jp) as conn:
        for when in (now - dt.timedelta(days=400), now - dt.timedelta(days=10)):
            start = when.replace(minute=0, second=0, microsecond=0)
            await conn.execute(
                "INSERT INTO care_activity_periods (person_id, period_start, period_end, model_version, model_sha256, seconds) "
                "VALUES ($1, $2, $3, $4, $5, '{\"sitting\": 600}'::jsonb)", pid, start, start + dt.timedelta(hours=1), VERSION, SHA)
    out = await retention.run_workspace(jp, now)
    assert out.deleted.get("care_activity_periods") == 1
    assert await _count(fam, pid) == 1
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.delete(f"{C}/people/{pid}")).status_code in (200, 204)
    assert await _count(fam, pid) == 0


# ── no alert path ────────────────────────────────────────────────────────────

async def test_a_day_of_stillness_off_the_wrist_and_unknown_opens_no_alert(client, families):
    """Twenty-four hours that would look alarming if anybody read them as health data: the
    watch off the wrist, then nothing but stillness and 'unknown'. Ingestion answers with no
    safety check, and the escalation pass (every rule, every person) opens nothing."""
    fam = families["a"]
    p = await _setup(client, fam, consent=True)
    now = dt.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    hours = []
    for h in range(24, 0, -1):
        start = now - dt.timedelta(hours=h)
        if h > 12:
            hours.append(exact_hour(start, {"unavailable": 3600}, {"off_wrist": 3600}))
        else:
            hours.append(exact_hour(start, {"sitting": 1800, "unknown": 1800}, {"low_confidence": 1800}))
    r = await client.post(f"{C}/devices/me/activity", headers=p["h"], json={"periods": hours})
    assert r.json() == {"accepted": 24, "stored": 24, "sharing": True}
    assert "safety_check" not in r.json()
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    # The device itself is healthy (worn, charged, syncing), so any alert would have to come from the activity.
    await client.post(f"{C}/devices/me/report", headers=p["h"], json={"readings": [], "battery_percent": 80, "on_wrist": True,
                                                                       "background_sensor_permission": True})
    out = await escalation.run_workspace(jp, dt.datetime.now(UTC), provider=providers.LogOnlyProvider())
    assert out.alerts_opened == 0
    async with workspace_connection(jp) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_alerts WHERE person_id = $1", uuid.UUID(p["id"])) == 0
        assert await conn.fetchval("SELECT count(*) FROM care_checkins WHERE person_id = $1", uuid.UUID(p["id"])) == 0
