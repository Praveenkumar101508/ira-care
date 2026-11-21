"""IRA CARE (phase 8): reports and history against a real PostgreSQL.

Generation is idempotent per person and period; the hourly job generates only
what is due in the person's own timezone; the routes list, generate and
fetch reports and search history, and every one of those reads is audited.
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
from care import reports  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc


async def _person_with_a_day(client, fam) -> dict:
    """A person, a watch, and one day (20 Nov 2026) with two answered check-ins and a miss."""
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Family Member", "role": "family", "user_id": str(fam["member"])})
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    device = dict(paired); device["person_id"] = uuid.UUID(person["id"]); device["id"] = uuid.UUID(paired["id"])
    jp = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    rng = random.Random(1)
    now = dt.datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    await cycle.run_workspace(jp, now, rng=rng)
    for i in range(3):
        now += dt.timedelta(minutes=21)
        asked = await cycle.due_checkin(jp, device, now, rng=rng)
        assert asked is not None
        if i < 2:
            await cycle.record_answer(jp, device, asked["id"], "I'm fine, my knee hurts a bit", now=now + dt.timedelta(seconds=20))
            now += dt.timedelta(seconds=20)
        else:
            await cycle.mark_missed(jp, device, asked["id"], now + dt.timedelta(minutes=3))
            now += dt.timedelta(minutes=3)
        await cycle.run_workspace(jp, now, rng=rng)
    async with workspace_connection(jp) as conn:
        await conn.execute(
            "INSERT INTO care_vitals (person_id, device_id, kind, value, recorded_at) VALUES ($1, $2, 'heart_rate', 72, $3), ($1, $2, 'steps', 30, $4)",
            uuid.UUID(person["id"]), device["id"], dt.datetime(2026, 11, 20, 9, 30, tzinfo=UTC), dt.datetime(2026, 11, 20, 9, 31, tzinfo=UTC))
    person["jp"] = jp
    return person


async def test_a_daily_report_is_generated_once_and_readable_by_the_family(client, families):
    fam = families["a"]
    person = await _person_with_a_day(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{person['id']}/reports/generate", json={"kind": "daily_carer", "date": "2026-11-20"})
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["kind"] == "daily_carer" and rep["period_start"] == "2026-11-20"
    assert "2 answered, 1 missed, of 3 asked" in rep["body_text"]
    assert "Heart rate: 1 readings, 72 to 72" in rep["body_text"]
    assert "knee" not in rep["body_text"], "a report never carries the person's own words"
    again = await client.post(f"{C}/people/{person['id']}/reports/generate", json={"kind": "daily_carer", "date": "2026-11-20"})
    assert again.status_code == 201 and again.json()["id"] == rep["id"], "generation is idempotent per period"
    r = await client.get(f"{C}/people/{person['id']}/reports")
    assert r.status_code == 200 and r.json()["count"] == 1
    r = await client.get(f"{C}/reports/{rep['id']}")
    assert r.status_code == 200 and r.json()["title"].startswith("Daily report for Test Person")
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/reports/{rep['id']}")).status_code == 404
    client.act_as(fam, "admin", "workspace_admin")
    log = (await client.get(f"{C}/people/{person['id']}/access-log")).json()["entries"]
    assert sum(1 for e in log if e["resource_kind"] == "report") >= 3


async def test_the_weekly_family_report_covers_the_week_and_is_summaries_only(client, families):
    fam = families["a"]
    person = await _person_with_a_day(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{person['id']}/reports/generate", json={"kind": "weekly_family", "date": "2026-11-20"})
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["period_start"] == "2026-11-16" and rep["period_end"] == "2026-11-22"
    assert "Friday: 2 of 3 check-ins answered" in rep["body_text"]
    assert "summaries only" in rep["body_text"]
    assert len(rep["body"]["days"]) == 7


async def test_the_hourly_job_generates_what_is_due_in_the_persons_timezone(client, families):
    fam = families["a"]
    person = await _person_with_a_day(client, fam)
    jp = person["jp"]
    # 06:30 UTC on 21 Nov: yesterday's daily report is not due yet (07:00 local).
    out = await reports.run_workspace(jp, dt.datetime(2026, 11, 21, 6, 30, tzinfo=UTC), deliver_reports=False)
    assert out.daily == 0 and out.weekly == 0
    out = await reports.run_workspace(jp, dt.datetime(2026, 11, 21, 7, 10, tzinfo=UTC), deliver_reports=False)
    assert out.daily == 1 and out.weekly == 0, "Saturday: daily only"
    out = await reports.run_workspace(jp, dt.datetime(2026, 11, 21, 7, 40, tzinfo=UTC), deliver_reports=False)
    assert out.daily == 0, "the same day's report is not generated twice"
    out = await reports.run_workspace(jp, dt.datetime(2026, 11, 23, 8, 10, tzinfo=UTC), deliver_reports=False)
    assert out.weekly == 1 and out.daily == 1, "Monday morning: last week's family report and Sunday's daily"
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/reports", params={"kind": "weekly_family"})
    assert r.json()["count"] == 1 and r.json()["reports"][0]["period_start"] == "2026-11-16"


async def test_history_search_spans_checkins_and_alerts_and_adds_transcripts_only_under_consent(client, families):
    fam = families["a"]
    person = await _person_with_a_day(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/history/search", params={"q": "pain"})
    assert r.status_code == 200
    kinds = {x["kind"] for x in r.json()["results"]}
    assert "checkin" in kinds and "transcript" not in kinds and r.json()["transcripts_included"] is False
    r = await client.get(f"{C}/people/{person['id']}/history/search", params={"q": "missed"})
    assert any(x["kind"] == "checkin" for x in r.json()["results"])
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{person['id']}/consents", json={"kind": "transcripts_family", "granted": True, "granted_by": "person"})
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/history/search", params={"q": "knee"})
    assert r.json()["transcripts_included"] is True and any(x["kind"] == "transcript" for x in r.json()["results"])
    client.act_as(fam, "admin", "workspace_admin")
    log = (await client.get(f"{C}/people/{person['id']}/access-log")).json()["entries"]
    searches = [e for e in log if e["action"] == "search"]
    assert len(searches) == 3 and searches[0]["detail"]["query"] in ("knee", "missed", "pain")
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{person['id']}/history/search", params={"q": "pain"})).status_code == 404
