"""D-076 against a real PostgreSQL: in one care organisation's workspace, being in one
person's circle shows you that person and nobody else.

Ada and Bea are both cared for in the same workspace. Ada's daughter is in Ada's circle
only. Every route that names a person or one of a person's things is called for Bea, taken
from the application's own route table so a new route is covered the day it is added, and
each must answer "not found". The lists (people, alerts, overview) show her Ada only. The
care manager, not in either circle, still sees both.
"""
from __future__ import annotations

import datetime as dt
import os
import re

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

from care import routes  # noqa: E402

C = "/api/v1/care"
NOW = dt.datetime.now(dt.timezone.utc)


async def _person(client, fam, name: str) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": name, "timezone": "UTC"})
    assert r.status_code == 201, r.text
    p = r.json()
    r = await client.post(f"{C}/people/{p['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    p["device_id"], p["token"] = paired["id"], paired["device_token"]
    return p


async def _things(client, fam, p) -> dict:
    """One of each of a person's things, made by the manager, keyed by its path parameter."""
    client.act_as(fam, "admin", "workspace_admin")
    pid = p["id"]
    ids = {"person_id": pid, "device_id": p["device_id"]}
    r = await client.post(f"{C}/people/{pid}/circle", json={"display_name": "A carer", "role": "carer"})
    ids["member_id"] = r.json()["id"]
    r = await client.post(f"{C}/people/{pid}/routines", json={"name": "lunch", "kind": "meal", "time_of_day": "12:30"})
    ids["routine_id"] = r.json()["id"]
    r = await client.post(f"{C}/people/{pid}/medications", json={"name": "Blue tablet", "dose": "1", "times": ["08:00"]})
    ids["medication_id"] = r.json()["id"]
    r = await client.post(f"{C}/people/{pid}/reminders", json={"text": "ring the GP", "due_at": (NOW + dt.timedelta(hours=1)).isoformat()})
    ids["reminder_id"] = r.json()["id"]
    r = await client.post(f"{C}/people/{pid}/visits", json={
        "planned_start": (NOW + dt.timedelta(hours=1)).isoformat(), "planned_end": (NOW + dt.timedelta(hours=2)).isoformat(),
        "carer_name": "Sravani", "tasks": ["Breakfast"]})
    ids["visit_id"], ids["task_id"] = r.json()["id"], r.json()["tasks"][0]["id"]
    r = await client.post(f"{C}/people/{pid}/consents", json={"kind": "data_retention", "granted": True,
                                                               "granted_by": "person", "granted_by_name": "Bea"})
    ids["consent_id"] = r.json()["id"]
    r = await client.post(f"{C}/people/{pid}/reports/generate", json={"kind": "daily_carer", "date": NOW.date().isoformat()})
    ids["report_id"] = r.json()["id"]
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": p["token"]})
    ids["alert_id"] = r.json()["id"]
    ids["day"] = NOW.date().isoformat()
    for k, v in ids.items():
        assert v, k
    return ids


def _guarded_routes():
    for r in routes.router.routes:
        path = r.path
        if "/devices/me" in path or "/providers/" in path:
            continue
        params = set(re.findall(r"{(\w+)}", path))
        if "person_id" in params or params & set(routes._RESOURCE_GUARDS):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, path


async def test_a_family_member_sees_their_own_person_and_nobody_else_in_the_workspace(client, families):
    fam = families["a"]
    ada = await _person(client, fam, "Ada")
    bea = await _person(client, fam, "Bea")
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{ada['id']}/circle", json={"display_name": "Ada's daughter", "role": "family",
                                                               "relationship": "daughter", "user_id": str(fam["member"])})
    assert r.status_code == 201, r.text
    bea_ids = await _things(client, fam, bea)
    await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": ada["token"]})

    client.act_as(fam, "member", "member")
    # The lists: Ada only.
    people = (await client.get(f"{C}/people")).json()["people"]
    assert [x["id"] for x in people] == [ada["id"]]
    alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert alerts and {a["person_id"] for a in alerts} == {ada["id"]}
    assert (await client.get(f"{C}/alerts", params={"person_id": bea["id"]})).json()["alerts"] == []
    ov = (await client.get(f"{C}/overview")).json()
    assert ov["people"] == 1 and all(a["person_id"] == ada["id"] for a in ov["alerts"])
    assert all(v["person_id"] == ada["id"] for v in ov["visits"])

    # Every route that names Bea or one of Bea's things: not found.
    leaked = []
    calls = 0
    for method, path in _guarded_routes():
        url = "/api/v1" + re.sub(r"{(\w+)}", lambda m: str(bea_ids[m.group(1)]), path)
        r = await client.request(method, url, json={})
        calls += 1
        if r.status_code != 404:
            leaked.append(f"{method} {path} -> {r.status_code}")
    assert calls >= 60
    assert not leaked, "answered for a person outside the caller's circle:\n  " + "\n  ".join(leaked)

    # Bea is untouched by all of that: the alert still open, the reminder still pending.
    client.act_as(fam, "admin", "workspace_admin")
    alert = (await client.get(f"{C}/alerts/{bea_ids['alert_id']}")).json()
    assert alert["status"] in ("open", "escalating") and not alert["acknowledgements"]

    # The care manager, in neither circle, sees both.
    people = (await client.get(f"{C}/people")).json()["people"]
    assert {x["id"] for x in people} == {ada["id"], bea["id"]}
    assert (await client.get(f"{C}/people/{bea['id']}/status")).status_code == 200


async def test_leaving_the_circle_ends_access_at_once(client, families):
    fam = families["a"]
    ada = await _person(client, fam, "Ada")
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{ada['id']}/circle", json={"display_name": "Daughter", "role": "family",
                                                               "user_id": str(fam["member"])})
    member_id = r.json()["id"]
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{ada['id']}/status")).status_code == 200
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.patch(f"{C}/circle/{member_id}", json={"active": False})).status_code == 200
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{ada['id']}/status")).status_code == 404
    assert (await client.get(f"{C}/people")).json()["people"] == []
