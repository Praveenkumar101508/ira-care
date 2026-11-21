"""IRA CARE (phase 9): retention enforced, and the check-in cycle under load.

Retention: rows older than the person's retention period go; newer rows,
open alerts and the access log stay; the purge is itself on the record.

Load: 1,000 people in one workspace; one scheduling pass and one pass with
every check-in due complete in bounded time.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import time
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
from care import escalation, retention  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _person(client, fam) -> uuid.UUID:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    return uuid.UUID(r.json()["id"])


async def _counts(conn, pid) -> dict:
    out = {}
    for table in ("care_vitals", "care_checkins", "care_transcripts", "care_alerts", "care_reports", "care_medication_events"):
        out[table] = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE person_id = $1", pid)  # noqa: S608
    out["care_access_log"] = await conn.fetchval("SELECT count(*) FROM care_access_log WHERE person_id = $1", pid)
    return out


async def test_retention_removes_only_what_is_older_than_the_persons_period(client, families):
    fam = families["a"]
    pid = await _person(client, fam)
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    old = now - dt.timedelta(days=400)
    recent = now - dt.timedelta(days=10)
    async with workspace_connection(jp) as conn:
        for when in (old, recent):
            await conn.execute("INSERT INTO care_vitals (person_id, kind, value, recorded_at) VALUES ($1, 'steps', 1, $2)", pid, when)
            await conn.execute("INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind) VALUES ($1, $2, 'answered', 'mood')", pid, when)
            await conn.execute("INSERT INTO care_transcripts (person_id, speaker, text, created_at) VALUES ($1, 'person', 'old words', $2)", pid, when)
            await conn.execute("INSERT INTO care_reports (person_id, kind, period_start, period_end, generated_at, title, body_text) "
                               "VALUES ($1, 'daily_carer', $2::date, $2::date, $2, 'Daily report', 'text')", pid, when)
        await conn.execute("INSERT INTO care_alerts (person_id, kind, severity, status, opened_at, resolved_at) VALUES ($1, 'low_battery', 'notice', 'resolved', $2, $2)", pid, old)
        await conn.execute("INSERT INTO care_alerts (person_id, kind, severity, status, opened_at) VALUES ($1, 'help_requested', 'sos', 'acknowledged', $2)", pid, old)
        await conn.execute("INSERT INTO care_access_log (person_id, resource_kind, at) VALUES ($1, 'vitals', $2)", pid, old)
        before = await _counts(conn, pid)
    assert before["care_vitals"] == 2 and before["care_alerts"] == 2 and before["care_access_log"] == 1
    out = await retention.run_workspace(jp, now)
    assert out.people == 1 and out.total == 5
    async with workspace_connection(jp) as conn:
        after = await _counts(conn, pid)
        kept_alert = await conn.fetchval("SELECT kind FROM care_alerts WHERE person_id = $1", pid)
        log = await conn.fetch("SELECT * FROM care_access_log WHERE person_id = $1 ORDER BY at", pid)
    assert after == {"care_vitals": 1, "care_checkins": 1, "care_transcripts": 1, "care_alerts": 1, "care_reports": 1,
                     "care_medication_events": 0, "care_access_log": 2}
    assert kept_alert == "help_requested", "an alert that was never resolved is never purged"
    assert "retention purge" in str(log[-1]["detail"]) and "400" not in str(log[-1]["detail"])
    # A second pass finds nothing more and writes no further row.
    out2 = await retention.run_workspace(jp, now)
    assert out2.total == 0
    async with workspace_connection(jp) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_access_log WHERE person_id = $1", pid) == 2


async def test_a_shorter_consented_retention_removes_more(client, families):
    fam = families["a"]
    pid = await _person(client, fam)
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{pid}/consents", json={"kind": "data_retention", "granted": True, "granted_by": "person", "retention_months": 3})
    assert r.status_code == 201
    async with workspace_connection(jp) as conn:
        await conn.execute("INSERT INTO care_vitals (person_id, kind, value, recorded_at) VALUES ($1, 'steps', 1, $2)", pid, now - dt.timedelta(days=120))
        await conn.execute("INSERT INTO care_vitals (person_id, kind, value, recorded_at) VALUES ($1, 'steps', 1, $2)", pid, now - dt.timedelta(days=60))
    out = await retention.run_workspace(jp, now)
    assert out.deleted["care_vitals"] == 1


async def test_a_thousand_people_fit_in_one_cycle_pass(client, families):
    """The check-in cycle and the monitoring pass over 1,000 people, timed.

    The bound is generous (a minute's job must finish well inside a minute on a
    shared CI runner); the point is that the pass is linear and does not fall
    over, and that the timings are printed for the record.
    """
    fam = families["a"]
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    n = 1000
    async with workspace_connection(jp) as conn:
        ids = [uuid.uuid4() for _ in range(n)]
        await conn.executemany("INSERT INTO care_people (id, display_name, preferred_name, timezone) VALUES ($1, $2, 'P', 'UTC')",
                               [(i, f"Load Person {k}") for k, i in enumerate(ids)])
        await conn.executemany("INSERT INTO care_safety_settings (person_id) VALUES ($1)", [(i,) for i in ids])
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    t0 = time.perf_counter()
    out = await cycle.run_workspace(jp, now, rng=random.Random(1))
    t_schedule = time.perf_counter() - t0
    assert out.people == n and out.scheduled == n
    later = now + dt.timedelta(minutes=45)  # past every window plus the grace
    t0 = time.perf_counter()
    out2 = await cycle.run_workspace(jp, later, rng=random.Random(2))
    t_due = time.perf_counter() - t0
    assert out2.missed == n and out2.scheduled == n, "every check-in was due and uncollected, so every one is a miss"
    t0 = time.perf_counter()
    mon = await escalation.run_workspace(jp, later)
    t_monitor = time.perf_counter() - t0
    assert mon.errors == 0
    print(f"\nload: {n} people; schedule pass {t_schedule:.2f}s, due pass {t_due:.2f}s, monitoring pass {t_monitor:.2f}s")
    assert t_schedule < 60 and t_due < 60 and t_monitor < 60
