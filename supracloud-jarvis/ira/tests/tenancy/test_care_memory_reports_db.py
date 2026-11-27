"""What the person deletes or corrects does not survive in what was derived from it (register F6.b,
F6.c, round 9), against a real PostgreSQL.

Reports are generated once per person and period and stored (`care_reports`). A day's memory
summary is copied into the daily carer report for that day; the weekly report covering it is
rebuilt too, so nothing derived from that day keeps the old text. Before round 9,
forgetting the day removed it from what IRA reads but left it in those stored reports, where the
family and carers would still read it. Now:

  * forgetting a day rebuilds the stored reports that cover it, without it;
  * correcting a day rebuilds them with the correction, never the corrected-away text;
  * a second session (a carer's) reading the record after the forget sees none of it;
  * the reports for other days are left exactly as they were.
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

from tests.tenancy._care_fixtures import add_wearer, client, families, pool  # noqa: E402,F401
from tests.tenancy.test_care_memory_db import _person, _talk  # noqa: E402

C = "/api/v1/care"
SECRET = "Uncle Hector's surprise visit"


async def _reports_text(person_id: str) -> list[tuple[str, str, str]]:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await conn.fetch("SELECT kind, period_start::text, body_text || ' ' || body::text AS t FROM care_reports "
                                "WHERE person_id = $1 ORDER BY kind, period_start", uuid.UUID(person_id))
        return [(r["kind"], r["period_start"], r["t"]) for r in rows]
    finally:
        await conn.close()


async def _seed_memory(person_id: str, day: dt.date, summary: str) -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        # The tenant columns are copied from the row the real conversation wrote for this person.
        await conn.execute(
            "INSERT INTO care_conversation_memory (tenant_id, workspace_id, person_id, day, summary, exchanges, updated_at) "
            "SELECT tenant_id, workspace_id, person_id, $2, $3, 1, NOW() FROM care_conversation_memory WHERE person_id = $1 LIMIT 1 "
            "ON CONFLICT (person_id, day) DO UPDATE SET summary = EXCLUDED.summary", uuid.UUID(person_id), day, summary)
    finally:
        await conn.close()


async def _generate(client, fam, person_id: str, kind: str, day: dt.date) -> None:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{person_id}/reports/generate", json={"kind": kind, "date": day.isoformat()})
    assert r.status_code == 201, r.text


async def test_forgetting_a_day_takes_it_out_of_the_stored_reports_that_cover_it(client, families):
    fam = families["a"]
    p = await _person(client, fam)
    await _talk(client, fam, p, "Just saying hello")
    client.act_as(fam, "member", "member")
    day = dt.date.fromisoformat((await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"][0]["day"])
    other = day - dt.timedelta(days=8)        # a day in another week, whose reports must not change
    await _seed_memory(p["id"], day, f"Talked about {SECRET}.")
    await _seed_memory(p["id"], other, "Talked about the allotment.")
    for kind, d in (("daily_carer", day), ("weekly_family", day), ("daily_carer", other)):
        await _generate(client, fam, p["id"], kind, d)
    before = await _reports_text(p["id"])
    # The daily carer report carries the day's memory; the weekly family one does not (render_weekly).
    assert [k for k, _, t in before if SECRET in t] == ["daily_carer"], "the daily report carries the day's memory"
    untouched = [r for r in before if r[1] == other.isoformat()]

    client.act_as(fam, "member", "member")
    assert (await client.delete(f"{C}/people/{p['id']}/memory/{day.isoformat()}")).status_code == 204

    after = await _reports_text(p["id"])
    assert not any(SECRET in t for _, _, t in after), "a forgotten day lives on in a stored report"
    assert {(k, s) for k, s, _ in after} == {(k, s) for k, s, _ in before}, "the reports are rebuilt, not lost"
    assert [r for r in after if r[1] == other.isoformat()] == untouched, "another week's report changed"

    # A second session, a carer's, reading the person's reports and memory afterwards sees nothing of it.
    client.act_as(fam, "admin", "workspace_admin")
    listed = (await client.get(f"{C}/people/{p['id']}/reports")).json()
    assert SECRET not in str(listed)
    assert SECRET not in str((await client.get(f"{C}/people/{p['id']}/memory")).json())


async def test_correcting_a_day_rebuilds_the_reports_with_the_correction(client, families):
    fam = families["a"]
    p = await _person(client, fam)
    await _talk(client, fam, p, "Just saying hello")
    client.act_as(fam, "member", "member")
    day = dt.date.fromisoformat((await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"][0]["day"])
    await _seed_memory(p["id"], day, f"Talked about {SECRET}.")
    await _generate(client, fam, p["id"], "daily_carer", day)
    assert any(SECRET in t for _, _, t in await _reports_text(p["id"]))

    client.act_as(fam, "member", "member")
    corrected = "CORRECTED: a quiet day with a book."
    assert (await client.put(f"{C}/people/{p['id']}/memory/{day.isoformat()}", json={"summary": corrected})).status_code == 200
    after = await _reports_text(p["id"])
    assert not any(SECRET in t for _, _, t in after), "the corrected-away text lives on in a stored report"
    assert any(corrected in t for _, _, t in after)
