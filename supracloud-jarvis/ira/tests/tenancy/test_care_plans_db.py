"""The care plan (116, care/plans.py) and staff roles (/care/me/permissions) against a real
PostgreSQL as `ira_app` through the real HTTP stack.

Covers: versions with reasons and the history kept; a stale editor refused instead of
overwriting; only a care manager writes; a carer or viewer in the circle reads; a carer
outside the circle, and another workspace, get "not found"; the table refuses UPDATE and
DELETE from the application; reads and writes are in the access log without the plan's
words; the person's erasure takes the plan with them; the permissions each role is told.
"""
from __future__ import annotations

import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import _principal, client, families, pool  # noqa: E402,F401

from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
SECRET_WORDS = "Ada likes porridge with honey"


def _body(based_on: int, reason: str, **extra) -> dict:
    return {"based_on": based_on, "reason": reason, "goals": "Stay at home safely", "support": SECRET_WORDS,
            "routine": [{"what": "Breakfast and tablets", "when": "08:00", "who": "carer"},
                        {"what": "Phone call", "when": "evening", "who": "family"}],
            "risks": "Falls on the stairs", "emergency": "Ring her daughter, then 999", "review_on": "2027-02-01",
            **extra}


async def _person(client, fam, name="Ada") -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": name, "timezone": "UTC"})
    assert r.status_code == 201, r.text
    return r.json()


async def _join(client, fam, person_id, who: str, role: str) -> None:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{person_id}/circle",
                          json={"display_name": f"The {who}", "role": role, "user_id": str(fam[who])})
    assert r.status_code == 201, r.text


async def test_each_change_is_a_new_version_and_the_history_is_kept(client, families):
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{ada['id']}/plan")).json() == {"plan": None}

    r = await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan, after the assessment"))
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1 and v1["routine"][1]["who"] == "family" and v1["review_on"] == "2027-02-01"
    r = await client.post(f"{C}/people/{ada['id']}/plan", json=_body(1, "Evening visit added", risks="Falls; forgets the oven"))
    assert r.status_code == 201, r.text

    cur = (await client.get(f"{C}/people/{ada['id']}/plan")).json()["plan"]
    assert cur["version"] == 2 and cur["risks"] == "Falls; forgets the oven"
    hist = (await client.get(f"{C}/people/{ada['id']}/plan/history")).json()
    assert [v["version"] for v in hist["versions"]] == [2, 1]
    assert [v["reason"] for v in hist["versions"]] == ["Evening visit added", "First plan, after the assessment"]
    assert hist["versions"][1]["risks"] == "Falls on the stairs", "the earlier version is kept as it was"

    # An editor who started from version 1 after version 2 was saved is refused, not merged.
    r = await client.post(f"{C}/people/{ada['id']}/plan", json=_body(1, "Stale change"))
    assert r.status_code == 409 and "version 2" in r.text
    # A change needs a reason.
    assert (await client.post(f"{C}/people/{ada['id']}/plan", json=_body(2, "   "))).status_code == 422
    assert len((await client.get(f"{C}/people/{ada['id']}/plan/history")).json()["versions"]) == 2


async def test_only_a_care_manager_writes_and_the_circle_reads(client, families):
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan"))).status_code == 201
    await _join(client, fam, ada["id"], "member", "carer")
    await _join(client, fam, ada["id"], "viewer", "family")

    for who, role in (("member", "member"), ("viewer", "viewer")):
        client.act_as(fam, who, role)
        r = await client.get(f"{C}/people/{ada['id']}/plan")
        assert r.status_code == 200 and r.json()["plan"]["version"] == 1, (who, r.text)
        r = await client.post(f"{C}/people/{ada['id']}/plan", json=_body(1, "Carer edit"))
        assert r.status_code == 403, (who, r.status_code)
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{ada['id']}/plan")).json()["plan"]["version"] == 1


async def test_a_carer_outside_the_circle_and_another_workspace_get_not_found(client, families):
    fam, other = families["a"], families["b"]
    ada = await _person(client, fam)
    bea = await _person(client, fam, "Bea")
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{bea['id']}/plan", json=_body(0, "First plan"))).status_code == 201
    await _join(client, fam, ada["id"], "member", "carer")  # Ada's carer, not Bea's

    client.act_as(fam, "member", "member")
    for path in (f"{C}/people/{bea['id']}/plan", f"{C}/people/{bea['id']}/plan/history"):
        assert (await client.get(path)).status_code == 404, path
    assert (await client.post(f"{C}/people/{bea['id']}/plan", json=_body(1, "x"))).status_code == 404

    # Another organisation's manager: not found over HTTP, and nothing visible to RLS.
    client.act_as(other, "admin", "workspace_admin")
    assert (await client.get(f"{C}/people/{bea['id']}/plan")).status_code == 404
    assert (await client.post(f"{C}/people/{bea['id']}/plan", json=_body(1, "x"))).status_code == 404
    async with workspace_connection(_principal(other, "admin", "workspace_admin")) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_plans") == 0
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_plans WHERE person_id = $1", bea["id"]) == 1


async def test_the_application_cannot_rewrite_or_delete_a_version(client, families):
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan"))
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE care_plans SET goals = 'changed' WHERE person_id = $1", ada["id"])
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM care_plans WHERE person_id = $1", ada["id"])
    # Not even the schema owner rewrites a version: the trigger refuses UPDATE for everyone.
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute("UPDATE care_plans SET goals = 'changed' WHERE person_id = $1", ada["id"])
    finally:
        await admin.close()


async def test_reads_and_writes_are_in_the_access_log_without_the_plans_words(client, families):
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan"))
    await client.get(f"{C}/people/{ada['id']}/plan")
    await client.get(f"{C}/people/{ada['id']}/plan/history")
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        rows = await conn.fetch("SELECT action, detail::text AS detail FROM care_access_log "
                                "WHERE person_id = $1 AND resource_kind = 'care_plan' ORDER BY at", ada["id"])
    assert [r["action"] for r in rows] == ["change", "view", "view"]
    for r in rows:
        assert "porridge" not in r["detail"] and "stairs" not in r["detail"]
    assert json.loads(rows[0]["detail"]) == {"version": 1}


async def test_erasing_the_person_takes_their_plan(client, families):
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan"))
    await client.post(f"{C}/people/{ada['id']}/plan", json=_body(1, "Second"))
    assert (await client.delete(f"{C}/people/{ada['id']}")).status_code == 204
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_plans WHERE person_id = $1", ada["id"]) == 0


async def test_each_role_is_told_what_it_may_do(client, families):
    fam = families["a"]
    expected = {"admin": ("workspace_admin", "Care manager", True), "member": ("member", "Carer", False),
                "viewer": ("viewer", "Viewer", False)}
    for who, (role, label, manage) in expected.items():
        client.act_as(fam, who, role)
        perms = (await client.get(f"{C}/me/permissions")).json()
        assert perms["label"] == label and perms["can_manage"] is manage
        assert perms["actions"]["edit_care_plan"] is manage and perms["actions"]["view_care_plan"] is True
        assert perms["actions"]["acknowledge_alerts"] is (role != "viewer")


async def test_deleting_the_authors_account_keeps_the_plan_and_its_words(client, families):
    """Account erasure clears the author link (ON DELETE SET NULL); the trigger lets exactly that
    through and nothing else, so the plan, its name and its words stay with the person."""
    fam = families["a"]
    ada = await _person(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    await client.post(f"{C}/people/{ada['id']}/plan", json=_body(0, "First plan"))
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        author = await admin.fetchval("SELECT author_user_id FROM care_plans WHERE person_id = $1", ada["id"])
        assert author == fam["admin"]
        await admin.execute("UPDATE care_plans SET author_user_id = NULL WHERE person_id = $1", ada["id"])
        row = await admin.fetchrow("SELECT author_user_id, support, version FROM care_plans WHERE person_id = $1", ada["id"])
        assert row["author_user_id"] is None and row["support"] == SECRET_WORDS and row["version"] == 1
        # Clearing the link together with any other change is still refused.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute("UPDATE care_plans SET author_user_id = NULL, support = 'x' WHERE person_id = $1", ada["id"])
    finally:
        await admin.close()
