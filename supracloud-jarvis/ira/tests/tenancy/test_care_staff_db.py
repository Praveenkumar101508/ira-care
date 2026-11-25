"""The care team's roles and views (117, care/staff.py, care/search.py; D-081) against a real
PostgreSQL as `ira_app` through the real HTTP stack.

Covers: a care coordinator plans visits for everyone and sees every caseload, and still cannot
manage people, the circle, watches, settings or care plans; a designation does nothing for a
viewer; a carer sees only their own caseload; a viewer is offered no action; only a care manager
names coordinators, and only somebody on the care team; another organisation can do none of it;
workspace search never shows a person outside the caller's circles, matches what is typed
literally, writes the search to the access log without the words, and is rationed.
"""
from __future__ import annotations

import datetime as dt
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
UTC = dt.timezone.utc


async def _person(client, fam, name) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": name, "timezone": "UTC"})
    assert r.status_code == 201, r.text
    return r.json()


async def _join(client, fam, person_id, who: str, role: str, name: str) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{person_id}/circle",
                          json={"display_name": name, "role": role, "user_id": str(fam[who])})
    assert r.status_code == 201, r.text
    return r.json()


def _window(hours_from_now: float, length_h: float = 1.0) -> dict:
    start = dt.datetime.now(UTC) + dt.timedelta(hours=hours_from_now)
    return {"planned_start": start.isoformat(), "planned_end": (start + dt.timedelta(hours=length_h)).isoformat()}


async def _team(client, fam):
    """Ada (carer: the member, "Sam"), Bea (carer: the wearer account, "Kim")."""
    ada = await _person(client, fam, "Ada Lovelace")
    bea = await _person(client, fam, "Bea Morgan")
    sam = await _join(client, fam, ada["id"], "member", "carer", "Sam Carer")
    kim = await _join(client, fam, bea["id"], "wearer", "carer", "Kim Carer")
    return ada, bea, sam, kim


async def _designate(client, fam, who: str, on: bool = True):
    client.act_as(fam, "admin", "workspace_admin")
    return await client.put(f"{C}/staff/{fam[who]}/coordinator", json={"coordinator": on})


async def test_a_coordinator_plans_visits_for_everyone_and_manages_nothing(client, families):
    fam = families["a"]
    ada, bea, sam, kim = await _team(client, fam)

    # Before the designation Sam is Ada's carer only: Bea is "not found".
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{bea['id']}")).status_code == 404
    assert (await client.post(f"{C}/people/{bea['id']}/visits",
                              json={**_window(2), "purpose": "Lunch", "carer_member_id": kim["id"]})).status_code == 404

    r = await _designate(client, fam, "member")
    assert r.status_code == 200 and r.json()["coordinator"] is True, r.text

    client.act_as(fam, "member", "member")
    perms = (await client.get(f"{C}/me/permissions")).json()
    assert perms["coordinator"] is True and perms["label"] == "Care coordinator" and perms["sees_everyone"] is True
    assert perms["can_manage"] is False

    # Plans and moves a visit for Bea, whose circle Sam is not in.
    r = await client.post(f"{C}/people/{bea['id']}/visits",
                          json={**_window(2), "purpose": "Lunch and tablets", "carer_member_id": kim["id"],
                                "carer_name": "Kim Carer"})
    assert r.status_code == 201, r.text
    visit = r.json()
    moved = _window(3)
    r = await client.patch(f"{C}/visits/{visit['id']}", json=moved)
    assert r.status_code == 200 and r.json()["planned_start"].startswith(moved["planned_start"][:16]), r.text

    plan = (await client.get(f"{C}/visits")).json()["visits"]
    assert {v["person_name"] for v in plan} == {"Bea Morgan"} and plan[0]["purpose"] == "Lunch and tablets"
    load = (await client.get(f"{C}/caseload")).json()
    assert load["everyone"] is True
    assert {c["name"] for c in load["carers"]} == {"Sam Carer", "Kim Carer"}
    kim_entry = next(c for c in load["carers"] if c["name"] == "Kim Carer")
    assert kim_entry["people"][0]["name"] == "Bea Morgan" and kim_entry["people"][0]["next_visit"] is not None

    # And manages nothing: people, circle, watches, settings, care plan, coordinators. Reading
    # the safety settings is allowed to anyone who may see the person; changing them is not.
    settings = (await client.get(f"{C}/people/{bea['id']}/settings")).json()
    settings = {k: v for k, v in settings.items() if k not in ("person_id", "set_by", "updated_at", "created_at")}
    refused = [
        ("POST", f"{C}/people", {"display_name": "New person", "timezone": "UTC"}),
        ("POST", f"{C}/people/{bea['id']}/circle", {"display_name": "X", "role": "family"}),
        ("POST", f"{C}/people/{bea['id']}/devices", {"platform": "wearos"}),
        ("PUT", f"{C}/people/{bea['id']}/settings", settings),
        ("POST", f"{C}/people/{bea['id']}/plan", {"based_on": 0, "reason": "x"}),
        ("GET", f"{C}/staff", None),
        ("PUT", f"{C}/staff/{fam['wearer']}/coordinator", {"coordinator": True}),
    ]
    for method, path, body in refused:
        r = await client.request(method, path, json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)

    # The access notes on Bea's profile stay with the care manager.
    r = await client.get(f"{C}/people/{bea['id']}/access-notes")
    assert r.status_code in (200, 403) and (r.status_code == 403 or r.json()["allowed"] is False), r.text

    # Stopping the designation stops it at once.
    assert (await _designate(client, fam, "member", on=False)).json()["coordinator"] is False
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{bea['id']}")).status_code == 404
    assert (await client.get(f"{C}/me/permissions")).json()["coordinator"] is False


async def test_a_carer_sees_only_their_own_caseload_and_visits(client, families):
    fam = families["a"]
    ada, bea, sam, kim = await _team(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    for person, member in ((ada, sam), (bea, kim)):
        r = await client.post(f"{C}/people/{person['id']}/visits",
                              json={**_window(1), "purpose": "Morning call", "carer_member_id": member["id"]})
        assert r.status_code == 201, r.text

    client.act_as(fam, "member", "member")  # Sam, not a coordinator
    load = (await client.get(f"{C}/caseload")).json()
    assert load["everyone"] is False
    assert [c["name"] for c in load["carers"]] == ["Sam Carer"]
    assert [p["name"] for p in load["carers"][0]["people"]] == ["Ada Lovelace"]
    assert {v["person_name"] for v in (await client.get(f"{C}/visits")).json()["visits"]} == {"Ada Lovelace"}

    client.act_as(fam, "admin", "workspace_admin")  # the care manager sees both
    load = (await client.get(f"{C}/caseload")).json()
    assert load["everyone"] is True and {c["name"] for c in load["carers"]} == {"Sam Carer", "Kim Carer"}
    assert {v["person_name"] for v in (await client.get(f"{C}/visits")).json()["visits"]} == {"Ada Lovelace", "Bea Morgan"}


async def test_a_viewer_is_offered_nothing_and_a_designation_does_not_change_that(client, families):
    fam = families["a"]
    ada, bea, sam, kim = await _team(client, fam)
    await _join(client, fam, ada["id"], "viewer", "family", "Vic Family")

    r = await _designate(client, fam, "viewer")
    assert r.status_code == 200  # recorded, and of no effect: a viewer is never a coordinator

    client.act_as(fam, "viewer", "viewer")
    perms = (await client.get(f"{C}/me/permissions")).json()
    assert perms["coordinator"] is False and perms["sees_everyone"] is False and perms["label"] == "Viewer"
    acting = {k: v for k, v in perms["actions"].items() if v}
    assert set(acting) <= {"view_care_plan", "search", "view_caseload", "view_visit_plan"}, acting
    assert (await client.get(f"{C}/people/{bea['id']}")).status_code == 404
    assert (await client.post(f"{C}/people/{ada['id']}/visits", json={**_window(1)})).status_code == 403
    assert (await client.get(f"{C}/caseload")).json()["carers"] == []


async def test_only_a_care_manager_names_coordinators_and_only_from_the_care_team(client, families):
    fam, other = families["a"], families["b"]
    await _team(client, fam)

    client.act_as(fam, "member", "member")
    assert (await client.put(f"{C}/staff/{fam['member']}/coordinator", json={"coordinator": True})).status_code == 403
    client.act_as(fam, "viewer", "viewer")
    assert (await client.get(f"{C}/staff")).status_code == 403

    # The viewer account is on no circle yet: not somebody on the care team.
    assert (await _designate(client, fam, "viewer")).status_code == 404

    client.act_as(fam, "admin", "workspace_admin")
    team = (await client.get(f"{C}/staff")).json()["staff"]
    assert {s["name"] for s in team} == {"Sam Carer", "Kim Carer"} and not any(s["coordinator"] for s in team)
    assert (await _designate(client, fam, "wearer")).status_code == 200
    assert (await _designate(client, fam, "wearer")).status_code == 200  # twice is once
    team = (await client.get(f"{C}/staff")).json()["staff"]
    assert [s["name"] for s in team if s["coordinator"]] == ["Kim Carer"]

    # Another organisation's manager cannot name our carer, nor see the designation.
    client.act_as(other, "admin", "workspace_admin")
    assert (await client.put(f"{C}/staff/{fam['wearer']}/coordinator", json={"coordinator": True})).status_code == 404
    async with workspace_connection(_principal(other, "admin", "workspace_admin")) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_staff_designations") == 0
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_staff_designations") == 1


async def test_search_never_shows_a_person_outside_the_circle_and_logs_no_words(client, families):
    fam = families["a"]
    ada, bea, sam, kim = await _team(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    for person, member in ((ada, sam), (bea, kim)):
        assert (await client.post(f"{C}/people/{person['id']}/visits",
                                  json={**_window(1), "purpose": "Porridge breakfast", "carer_member_id": member["id"],
                                        "carer_name": member["display_name"]})).status_code == 201

    # The manager finds both people's visits.
    found = (await client.get(f"{C}/search", params={"q": "porridge"})).json()["results"]
    assert {r["person_name"] for r in found} == {"Ada Lovelace", "Bea Morgan"} and {r["kind"] for r in found} == {"visit"}

    # Sam, Ada's carer, finds Ada's and never Bea's: not by name, not by her visit.
    client.act_as(fam, "member", "member")
    for q in ("porridge", "Bea", "Morgan", "Kim"):
        res = (await client.get(f"{C}/search", params={"q": q})).json()["results"]
        assert all(r["person_name"] == "Ada Lovelace" for r in res), (q, res)
    assert [r["kind"] for r in (await client.get(f"{C}/search", params={"q": "Lovelace"})).json()["results"]] == ["person"]

    # Typed literally: a LIKE wildcard matches only itself.
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.get(f"{C}/search", params={"q": "%%"})).json()["results"] == []
    assert (await client.get(f"{C}/search", params={"q": "__"})).json()["results"] == []
    assert (await client.get(f"{C}/search", params={"q": "x"})).status_code == 422  # too short

    # Written down, once per person found, without the words.
    async with workspace_connection(_principal(fam, "admin", "workspace_admin")) as conn:
        rows = await conn.fetch(
            "SELECT person_id, detail FROM care_access_log WHERE resource_kind = 'person' AND action = 'search'")
    details = [json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"] for r in rows]
    assert rows and all(d["scope"] == "workspace" for d in details)
    flat = json.dumps(details).lower()
    for word in ("porridge", "lovelace", "morgan", "kim", "%%"):
        assert word not in flat, word
    assert any(r["person_id"] is None and d.get("results") == 0 for r, d in zip(rows, details))


async def test_search_is_rationed_per_address(client, families):
    from care import routes
    fam = families["a"]
    await _team(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    budget = int(routes.SEARCH_LIMIT.split("/")[0])
    codes = [(await client.get(f"{C}/search", params={"q": "Ada"})).status_code for _ in range(budget + 3)]
    assert codes[:budget] == [200] * budget and set(codes[budget:]) == {429}, codes
