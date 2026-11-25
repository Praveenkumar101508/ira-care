"""IRA Care Memory keeps its promises (register F6), against a real PostgreSQL.

What the person, or their representative, changes or deletes stays changed or deleted:
  * a correction survives IRA's next note that day, and is what IRA reads next;
  * a forgotten day is gone from what IRA reads next;
  * erasing the person removes their conversation memory;
  * an answered check-in writes nothing into the assistant's general memory store, where no
    deletion promise could reach it (115 purges what earlier versions wrote; tested below,
    run as ira_schema_owner under FORCE RLS, inside a transaction that is rolled back).
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import add_wearer, client, families, pool  # noqa: E402,F401

from care import checkin_cycle as cycle  # noqa: E402
from care import talk  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"


async def _person(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    p = (await client.post(f"{C}/people", json={"display_name": "Mem Ory", "preferred_name": "Mem", "timezone": "UTC"})).json()
    await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Daughter", "role": "family",
                                                          "relationship": "daughter", "user_id": str(fam["member"])})
    await add_wearer(client, fam, p["id"])
    p["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return p


async def _talk(client, fam, p, text):
    client.act_as(fam, "wearer", "member")
    assert (await client.post(f"{C}/people/{p['id']}/talk", json={"text": text})).status_code == 200


async def _context(p, day) -> str:
    """What IRA itself reads back before it answers."""
    async with workspace_connection(p["jp"]) as conn:
        return await talk.memory_summary(conn, uuid.UUID(p["id"]), day)


async def test_a_correction_survives_iras_next_note_and_is_what_ira_reads(client, families):
    fam = families["a"]
    p = await _person(client, fam)
    await _talk(client, fam, p, "I had a lovely morning in the garden")
    client.act_as(fam, "member", "member")
    mem = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]
    day, original = mem[0]["day"], mem[0]["summary"]
    corrected = "CORRECTED by the family: a quiet morning indoors."
    assert (await client.put(f"{C}/people/{p['id']}/memory/{day}", json={"summary": corrected})).status_code == 200

    await _talk(client, fam, p, "The kettle is on, I'll have some tea")   # IRA adds to today
    client.act_as(fam, "member", "member")
    after = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"][0]["summary"]
    assert after.startswith(corrected), "the correction is kept, IRA only adds to it"
    # IRA's note for an exchange is generic, so the new exchange may add the same line again;
    # what must not happen is the corrected-away note coming back on top of the new one.
    assert after.count(original) <= 1, "the corrected-away text does not come back"
    ctx = await _context(p, dt.date.fromisoformat(day))
    assert "CORRECTED by the family" in ctx and ctx.count(original) <= 1


async def test_a_forgotten_day_is_gone_from_what_ira_reads_next(client, families):
    fam = families["a"]
    p = await _person(client, fam)
    await _talk(client, fam, p, "My neighbour Ben came round for coffee")
    client.act_as(fam, "member", "member")
    day = (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"][0]["day"]
    assert (await _context(p, dt.date.fromisoformat(day))) != ""
    assert (await client.delete(f"{C}/people/{p['id']}/memory/{day}")).status_code == 204
    assert (await _context(p, dt.date.fromisoformat(day))) == ""
    # Forgetting twice says so, rather than pretending.
    assert (await client.delete(f"{C}/people/{p['id']}/memory/{day}")).status_code == 404


async def test_erasing_the_person_removes_their_conversation_memory(client, families):
    fam = families["a"]
    p = await _person(client, fam)
    await _talk(client, fam, p, "I watched the cricket this afternoon")
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.delete(f"{C}/people/{p['id']}")).status_code in (200, 204)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await conn.fetchval("SELECT count(*) FROM care_conversation_memory WHERE person_id = $1", uuid.UUID(p["id"])) == 0
    finally:
        await conn.close()


async def test_an_answered_check_in_writes_nothing_to_the_general_memory_store(client, families, monkeypatch):
    fam = families["a"]
    p = await _person(client, fam)
    written: list[str] = []

    async def spy(principal, content, **kw):  # the general store's entry point
        written.append(content)
        return "x"
    import memory.store as store
    monkeypatch.setattr(store, "remember", spy)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/devices", json={"platform": "wearos"})
    h = {"X-Device-Token": (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()["device_token"]}
    assert (await client.post(f"{C}/people/{p['id']}/checkins/ask-now", json={"kind": "wellbeing"})).status_code in (200, 201)
    due = (await client.get(f"{C}/devices/me/checkins/due", headers=h)).json()["checkin"]
    r = await client.post(f"{C}/devices/me/checkins/{due['id']}/answer", json={"text": "I'm fine, I had my lunch", "via": "voice"}, headers=h)
    assert r.status_code == 200, r.text
    assert written == [], "a check-in answer went into the general memory store"
    # The person's own day summary still has it, where it can be corrected and deleted.
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{p['id']}/memory")).json()["memory"]


# ── 115: the purge, run as the migration role under FORCE RLS ────────────────

def _body(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    return re.sub(r"(?im)^COMMIT;\s*$", "", text)


def test_both_files_are_wrapped_in_a_transaction():
    for f in ("115_care_checkin_embeddings_purge.sql", "rollback/115_care_checkin_embeddings_purge_down.sql"):
        text = (POSTGRES / f).read_text()
        if "DELETE" in text:
            assert re.search(r"(?im)^BEGIN;", text) and re.search(r"(?im)^COMMIT;", text), f


async def test_115_purges_only_check_in_notes_even_under_force_rls():
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        tid, wid = uuid.uuid4(), uuid.uuid4()
        slug = f"m115-{tid.hex[:8]}"
        await conn.execute("INSERT INTO tenants (id,name,slug,edition) VALUES ($1,'115',$2,'cloud')", tid, slug)
        org = await conn.fetchval("INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id", tid, slug)
        await conn.execute("INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) VALUES ($1,$2,$3,'W',$4,TRUE)",
                           wid, tid, org, slug)
        ids = {k: uuid.uuid4() for k in ("checkin", "fact", "transcript")}
        for label, source_type in (("checkin", "care_checkin"), ("fact", "fact"), ("transcript", "conversation")):
            await conn.execute("INSERT INTO memory_embeddings (id,tenant_id,workspace_id,user_id,source_type,content) "
                               "VALUES ($1,$2,$3,'owner',$4,$5)", ids[label], tid, wid, source_type, label)
        await conn.execute("SET LOCAL ROLE ira_schema_owner")
        await conn.execute(_body(POSTGRES / "115_care_checkin_embeddings_purge.sql"))
        await conn.execute("RESET ROLE")
        left = {r["id"] for r in await conn.fetch("SELECT id FROM memory_embeddings WHERE tenant_id = $1", tid)}
        assert ids["checkin"] not in left, "the check-in note is purged, even with FORCE RLS on"
        assert {ids["fact"], ids["transcript"]} <= left, "nothing else is touched"
        forced = await conn.fetchval("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'memory_embeddings'")
        assert forced, "FORCE RLS is restored"
    finally:
        await tx.rollback()
        await conn.close()
