"""IRA CARE (phase 2): the `/api/v1/care` routes over the real HTTP stack
against a real PostgreSQL as the runtime role.

Two families, each a tenant with a default workspace. Family A's administrator
sets up a person, a care circle, a watch and consents; the watch pairs and
reports; a family member acknowledges an alert. Family B's administrator tries
to reach family A's data through the API and gets 404s, and family A's watch
token is refused when presented in family B's scope.

Only `require_auth` and `require_workspace_principal` are overridden, for the
reason tests/callers/test_api_route_callers.py gives: what is being proved here
is what happens AFTER a verified principal exists. Fixtures: `_care_fixtures.py`.
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

C = "/api/v1/care"


async def _setup_person(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess"})
    assert r.status_code == 201, r.text
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Family Member", "role": "family", "tier": 1, "phone": "+440000000001",
        "user_id": str(fam["member"])})
    assert r.status_code == 201, r.text
    person["member"] = r.json()
    return person


# ── People and roles ─────────────────────────────────────────────────────────

async def test_an_administrator_creates_a_person_with_default_settings(client, families):
    person = await _setup_person(client, families["a"])
    r = await client.get(f"{C}/people/{person['id']}/settings")
    assert r.status_code == 200
    s = r.json()
    assert (s["heart_rate_min"], s["heart_rate_max"]) == (None, None)
    assert s["checkin_min_minutes"] == 15 and s["checkin_max_minutes"] == 20
    assert s["quiet_start"].startswith("22:00") and s["quiet_end"].startswith("07:00")


async def test_a_viewer_can_look_but_not_change(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    client.act_as(fam, "viewer", "viewer")
    assert (await client.get(f"{C}/people")).status_code == 200
    assert (await client.get(f"{C}/people/{person['id']}/status")).status_code == 200
    r = await client.post(f"{C}/people", json={"display_name": "Nope"})
    assert r.status_code == 403
    r = await client.put(f"{C}/people/{person['id']}/settings", json={"heart_rate_min": 40, "heart_rate_max": 120})
    assert r.status_code == 403


async def test_a_member_can_act_but_not_manage(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/people/{person['id']}/routines", json={"name": "Lunch", "kind": "meal"})
    assert r.status_code == 403
    r = await client.post(f"{C}/people/{person['id']}/medications", json={"name": "Test tablet"})
    assert r.status_code == 403


async def test_the_other_family_sees_nothing_of_this_person(client, families):
    a, b = families["a"], families["b"]
    person = await _setup_person(client, a)
    client.act_as(b, "admin", "workspace_admin")
    r = await client.get(f"{C}/people")
    assert r.status_code == 200 and r.json()["count"] == 0
    for path in (f"/people/{person['id']}", f"/people/{person['id']}/status",
                 f"/people/{person['id']}/settings", f"/people/{person['id']}/vitals",
                 f"/people/{person['id']}/transcripts", f"/people/{person['id']}/access-log"):
        r = await client.get(C + path)
        assert r.status_code == 404, (path, r.status_code, r.text)
    r = await client.patch(f"{C}/people/{person['id']}", json={"display_name": "taken"})
    assert r.status_code == 404
    r = await client.delete(f"{C}/people/{person['id']}")
    assert r.status_code == 404
    client.act_as(a, "admin", "workspace_admin")
    r = await client.get(f"{C}/people/{person['id']}")
    assert r.status_code == 200 and r.json()["display_name"] == "Test Person"


async def test_safety_settings_reject_an_inverted_range(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    r = await client.put(f"{C}/people/{person['id']}/settings", json={"heart_rate_min": 120, "heart_rate_max": 40})
    assert r.status_code == 422
    r = await client.put(f"{C}/people/{person['id']}/settings", json={"heart_rate_min": 45, "heart_rate_max": 130})
    assert r.status_code == 200 and r.json()["heart_rate_max"] == 130


# ── The watch ────────────────────────────────────────────────────────────────

async def _pair(client, fam, person) -> tuple[dict, str]:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos", "model": "Test Watch"})
    assert r.status_code == 201, r.text
    created = r.json()
    code = created["pairing_code"]
    assert "pairing_code_hash" not in created and "token_hash" not in created
    r = await client.post(f"{C}/devices/pair", json={"pairing_code": code, "app_version": "0.1-test"})
    assert r.status_code == 200, r.text
    paired = r.json()
    assert paired["status"] == "paired" and "device_token" in paired
    return paired, paired["device_token"]


async def test_a_watch_pairs_once_and_reports_readings(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    paired, token = await _pair(client, fam, person)
    # The code is single use.
    r = await client.post(f"{C}/devices/pair", json={"pairing_code": "cp1.not-a-real-code-at-all.XXXXXXXX"})
    assert r.status_code == 404
    now = dt.datetime.now(dt.timezone.utc)
    report = {
        "readings": [
            {"kind": "heart_rate", "value": 71, "unit": "bpm", "recorded_at": (now - dt.timedelta(minutes=1)).isoformat()},
            {"kind": "steps", "value": 12, "recorded_at": now.isoformat()},
        ],
        "battery_percent": 64, "on_wrist": True, "background_sensor_permission": True,
    }
    h = {"X-Device-Token": token}
    r = await client.post(f"{C}/devices/me/report", json=report, headers=h)
    assert r.status_code == 200 and r.json() == {"accepted": 2, "stored": 2}
    # The offline queue re-sends: nothing is double counted.
    r = await client.post(f"{C}/devices/me/report", json=report, headers=h)
    assert r.json() == {"accepted": 2, "stored": 0}
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{person['id']}/status")
    assert r.status_code == 200
    status = r.json()
    assert status["devices"][0]["battery_percent"] == 64 and status["devices"][0]["on_wrist"] is True
    kinds = {v["kind"]: v["value"] for v in status["latest_readings"]}
    assert kinds == {"heart_rate": 71.0, "steps": 12.0}
    r = await client.get(f"{C}/people/{person['id']}/vitals", params={"kind": "heart_rate"})
    assert r.status_code == 200 and r.json()["count"] == 1


async def test_a_watch_token_is_refused_without_the_token_and_after_revocation(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    paired, token = await _pair(client, fam, person)
    r = await client.get(f"{C}/devices/me")
    assert r.status_code == 401
    r = await client.get(f"{C}/devices/me", headers={"X-Device-Token": token + "x"})
    assert r.status_code == 401
    r = await client.get(f"{C}/devices/me", headers={"X-Device-Token": token})
    assert r.status_code == 200
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/devices/{paired['id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = await client.get(f"{C}/devices/me", headers={"X-Device-Token": token})
    assert r.status_code == 401


async def test_a_token_rescoped_to_another_family_is_refused(client, families):
    """The token names its scope; the hash is checked inside that scope only."""
    from care import devices as devmod
    a, b = families["a"], families["b"]
    person = await _setup_person(client, a)
    _paired, token = await _pair(client, a, person)
    cred = devmod.parse(token)
    forged = devmod.Credential(b["tenant_id"], b["workspace_id"], cred.secret, "token").encode()
    r = await client.get(f"{C}/devices/me", headers={"X-Device-Token": forged})
    assert r.status_code == 401


async def test_the_help_button_opens_an_sos_that_a_member_can_acknowledge(client, families):
    fam = families["a"]
    person = await _setup_person(client, fam)
    _paired, token = await _pair(client, fam, person)
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": token})
    assert r.status_code == 201
    alert = r.json()
    assert alert["kind"] == "help_requested" and alert["severity"] == "sos" and alert["status"] == "open"
    client.act_as(fam, "viewer", "viewer")
    r = await client.get(f"{C}/alerts")
    assert r.status_code == 200 and [x["id"] for x in r.json()["alerts"]] == [alert["id"]]
    r = await client.post(f"{C}/alerts/{alert['id']}/acknowledge", json={"kind": "on_my_way"})
    assert r.status_code == 403
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/alerts/{alert['id']}/acknowledge", json={"kind": "on_my_way", "note": "5 min"})
    assert r.status_code == 200, r.text
    acked = r.json()
    assert acked["status"] == "acknowledged"
    assert acked["acknowledgements"][0]["member_id"] == person["member"]["id"]
    r = await client.post(f"{C}/alerts/{alert['id']}/resolve", json={"resolution": "Visited; all well."})
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    client.act_as(families["b"], "admin", "workspace_admin")
    r = await client.get(f"{C}/alerts/{alert['id']}")
    assert r.status_code == 404


# ── Consent and the access log ───────────────────────────────────────────────

async def test_transcripts_need_the_right_consent_and_every_read_is_logged(client, families, pool):
    fam = families["a"]
    person = await _setup_person(client, fam)
    pid = person["id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(fam["tenant_id"]))
        await conn.execute("SELECT set_config('app.workspace_id',$1,false)", str(fam["workspace_id"]))
        await conn.execute(
            "INSERT INTO care_transcripts (person_id, speaker, text) VALUES ($1, 'person', 'I had porridge')",
            uuid.UUID(pid))
        for k in ("app.tenant_id", "app.workspace_id"):
            await conn.execute(f"SELECT set_config('{k}','',false)")
    # The family member, with no consent recorded: refused.
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{pid}/transcripts")
    assert r.status_code == 403
    # The administrator is not in the circle: refused even with every consent.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{pid}/consents", json={
        "kind": "transcripts_family", "granted": True, "granted_by": "person"})
    assert r.status_code == 201
    r = await client.get(f"{C}/people/{pid}/transcripts")
    assert r.status_code == 403
    # The family member, now consented: allowed.
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/people/{pid}/transcripts")
    assert r.status_code == 200 and r.json()["count"] == 1
    # Revoked: refused again.
    client.act_as(fam, "admin", "workspace_admin")
    cid = (await client.get(f"{C}/people/{pid}/consents")).json()["consents"][0]["id"]
    assert (await client.post(f"{C}/consents/{cid}/revoke")).status_code == 200
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{pid}/transcripts")).status_code == 403
    # Every look, allowed or refused, at readings or transcripts was logged.
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.get(f"{C}/people/{pid}/access-log")
    assert r.status_code == 200
    kinds = [e["resource_kind"] for e in r.json()["entries"]]
    assert kinds.count("transcript") == 1
    assert all(e["actor_user_id"] for e in r.json()["entries"])
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/people/{pid}/access-log")).status_code == 403


async def test_erasing_a_person_leaves_only_the_access_log(client, families, pool):
    fam = families["a"]
    person = await _setup_person(client, fam)
    pid = person["id"]
    await _pair(client, fam, person)
    client.act_as(fam, "member", "member")
    await client.get(f"{C}/people/{pid}/vitals")
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.delete(f"{C}/people/{pid}")
    assert r.status_code == 204
    assert (await client.get(f"{C}/people/{pid}")).status_code == 404
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(fam["tenant_id"]))
        await conn.execute("SELECT set_config('app.workspace_id',$1,false)", str(fam["workspace_id"]))
        devices = await conn.fetchval("SELECT count(*) FROM care_devices WHERE person_id = $1", uuid.UUID(pid))
        log = await conn.fetchval("SELECT count(*) FROM care_access_log WHERE person_id = $1", uuid.UUID(pid))
        for k in ("app.tenant_id", "app.workspace_id"):
            await conn.execute(f"SELECT set_config('{k}','',false)")
    assert devices == 0 and log >= 2
