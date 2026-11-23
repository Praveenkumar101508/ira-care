"""IRA CARE round 2, phase 2: home, access notes and the one-off location.

The watch sends its location once, when help is asked for; it is stored on
the alert, "at home" is decided from the carer's home coordinates, SOS texts
carry the address and whereabouts, and the sealed access notes open only for
tier 1 and 2 recipients while the alert is open, every view logged.
"""
from __future__ import annotations

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

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
HOME = {"home_address": "1 Example Street, Testtown, TE5 7ST", "home_latitude": 51.5, "home_longitude": -0.12,
        "home_radius_m": 150, "emergency_summary": "Hard of hearing; speak clearly and face them.",
        "access_notes": "Key safe left of the front door. Small friendly dog called Biscuit."}


async def _setup(client, fam, *, home=True) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    body = {"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"}
    if home:
        body.update(HOME)
    r = await client.post(f"{C}/people", json=body)
    assert r.status_code == 201, r.text
    person = r.json()
    await client.post(f"{C}/people/{person['id']}/circle", json={"display_name": "Family Member", "role": "family",
                      "tier": 1, "user_id": str(fam["member"]), "phone": "+440000000001", "sms_enabled": True})
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["token"] = paired["device_token"]
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def test_the_person_record_keeps_the_home_but_never_returns_the_notes_in_clear(client, families, pool):
    fam = families["a"]
    p = await _setup(client, fam)
    assert p["home_address"] == HOME["home_address"] and p["has_access_notes"] is True
    assert "access_notes" not in p and "access_notes_ciphertext" not in p
    got = (await client.get(f"{C}/people/{p['id']}")).json()
    assert got["emergency_summary"] == HOME["emergency_summary"] and got["has_access_notes"] is True
    listed = (await client.get(f"{C}/people")).json()["people"][0]
    assert "access_notes_ciphertext" not in listed
    # On disk the notes are ciphertext, not text.
    async with workspace_connection(p["jp"]) as conn:
        raw = await conn.fetchrow("SELECT access_notes_ciphertext, access_notes_wrapped_dek FROM care_people WHERE id = $1",
                                  uuid.UUID(p["id"]))
    assert raw["access_notes_ciphertext"] is not None and b"Biscuit" not in bytes(raw["access_notes_ciphertext"])
    # Clearing the notes.
    r = await client.patch(f"{C}/people/{p['id']}", json={"access_notes": ""})
    assert r.status_code == 200 and r.json()["has_access_notes"] is False
    # Half a coordinate pair is refused.
    r = await client.patch(f"{C}/people/{p['id']}", json={"home_latitude": 51.5})
    assert r.status_code == 422


async def test_help_with_a_location_is_stored_on_the_alert_and_told_to_the_circle(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    # Away from home: 2 km north.
    r = await client.post(f"{C}/devices/me/help", json={"via": "button", "location": {
        "latitude": 51.518, "longitude": -0.12, "accuracy_m": 12}}, headers={"X-Device-Token": p["token"]})
    assert r.status_code == 201, r.text
    alert = r.json()
    assert alert["at_home"] is False and alert["location_latitude"] == 51.518 and alert["location_at"]
    provider = providers.LogOnlyProvider()
    out = await escalation.run_workspace(p["jp"], provider=provider)
    assert out.tiers_run == 1
    texts = [t for ch, _to, t in provider.log if ch == "sms"]
    assert texts, "the family member has a phone and texts on"
    assert "AWAY FROM HOME" in texts[0] and "1 Example Street" in texts[0] and "openstreetmap" in texts[0]
    assert f"/alerts/{alert['id']}" in texts[0]
    client.act_as(fam, "member", "member")
    seen = (await client.get(f"{C}/alerts/{alert['id']}")).json()
    assert seen["at_home"] is False and seen["location_longitude"] == -0.12


async def test_at_home_and_no_location_are_both_stated_honestly(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/help", json={"via": "voice", "location": {
        "latitude": 51.5002, "longitude": -0.1201, "accuracy_m": 25}}, headers={"X-Device-Token": p["token"]})
    assert r.json()["at_home"] is True
    client.act_as(fam, "member", "member")
    alerts = (await client.get(f"{C}/alerts")).json()["alerts"]
    await client.post(f"{C}/alerts/{alerts[0]['id']}/resolve", json={"resolution": "All well.", "outcome": "all_well"})
    # No location at all.
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": p["token"]})
    alert = r.json()
    assert alert["at_home"] is None and alert["location_latitude"] is None
    provider = providers.LogOnlyProvider()
    await escalation.run_workspace(p["jp"], provider=provider)
    texts = [t for ch, _to, t in provider.log if ch == "sms"]
    assert "Location unavailable" in texts[-1] and "1 Example Street" in texts[-1]
    # A person with no home coordinates: at home is unknown, never guessed.
    p2 = await _setup(client, families["b"], home=False)
    r = await client.post(f"{C}/devices/me/help", json={"via": "button", "location": {"latitude": 51.5, "longitude": -0.12}},
                          headers={"X-Device-Token": p2["token"]})
    assert r.json()["at_home"] is None and r.json()["location_latitude"] == 51.5


async def test_access_notes_open_for_tier_1_while_the_alert_is_open_and_every_view_is_logged(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers={"X-Device-Token": p["token"]})
    alert_id = r.json()["id"]
    # The family member is tier 1 and linked to their account.
    client.act_as(fam, "member", "member")
    r = await client.get(f"{C}/alerts/{alert_id}/access-notes")
    assert r.status_code == 200, r.text
    assert r.json()["access_notes"].startswith("Key safe") and "Biscuit" in r.json()["access_notes"]
    assert r.json()["emergency_summary"] == HOME["emergency_summary"]
    # A viewer who is not in the circle may not.
    client.act_as(fam, "viewer", "viewer")
    assert (await client.get(f"{C}/alerts/{alert_id}/access-notes")).status_code == 403
    # The organisation's manager may (tier 2 by role).
    client.act_as(fam, "admin", "workspace_admin")
    assert (await client.get(f"{C}/alerts/{alert_id}/access-notes")).status_code == 200
    # Still allowed once acknowledged (the responder is on the way); never once resolved.
    client.act_as(fam, "member", "member")
    await client.post(f"{C}/alerts/{alert_id}/acknowledge", json={"kind": "on_my_way", "note": ""})
    assert (await client.get(f"{C}/alerts/{alert_id}/access-notes")).status_code == 200
    await client.post(f"{C}/alerts/{alert_id}/resolve", json={"resolution": "All well.", "outcome": "all_well"})
    assert (await client.get(f"{C}/alerts/{alert_id}/access-notes")).status_code == 403
    # Three successful openings, three log rows naming the alert.
    client.act_as(fam, "admin", "workspace_admin")
    log = (await client.get(f"{C}/people/{p['id']}/access-log")).json()["entries"]
    views = [e for e in log if e["resource_kind"] == "access_notes"]
    assert len(views) == 3 and all(e["detail"]["alert_status"] in ("open", "acknowledged") for e in views)
    assert {e["actor_label"] for e in views} == {"member", "workspace_admin"}
    # The other family's alert is not reachable at all.
    client.act_as(families["b"], "admin", "workspace_admin")
    assert (await client.get(f"{C}/alerts/{alert_id}/access-notes")).status_code == 404


async def test_a_watch_cannot_send_a_location_outside_an_sos(client, families):
    """There is no route that accepts a location on its own: readings do not
    carry one, so the watch has nothing to stream."""
    fam = families["a"]
    p = await _setup(client, fam)
    r = await client.post(f"{C}/devices/me/report", json={"readings": [{"kind": "location", "value": 51.5,
                          "unit": "", "recorded_at": "2026-11-21T10:00:00Z"}]}, headers={"X-Device-Token": p["token"]})
    assert r.status_code == 422
