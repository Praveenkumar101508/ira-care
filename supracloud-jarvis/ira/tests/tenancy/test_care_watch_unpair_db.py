"""Unpairing on the wrist (remediation C-21) against a real PostgreSQL.

The watch asks before it unpairs, and unpairs only once the server has answered. On the
server, one request ends the token and tells the care circle that monitoring has stopped,
so unpairing never passes in silence and never leaves a live token behind.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401

C = "/api/v1/care"


async def _paired(client, fam, name):
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": name, "timezone": "UTC"})).json()
    code = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()["pairing_code"]
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": code})).json()
    return person, paired


async def test_unpairing_on_the_wrist_ends_the_token_and_tells_the_circle(client, families):
    fam = families["a"]
    person, paired = await _paired(client, fam, "Una Pair")
    other, other_paired = await _paired(client, fam, "Stays Paired")
    h = {"X-Device-Token": paired["device_token"]}
    assert (await client.get(f"{C}/devices/me", headers=h)).status_code == 200

    r = await client.post(f"{C}/devices/me/unpair", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["unpaired"] is True and r.json()["device_id"] == paired["id"]

    # The token is dead at once, for everything, including a second unpair.
    assert (await client.get(f"{C}/devices/me", headers=h)).status_code == 401
    assert (await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=h)).status_code == 401
    assert (await client.post(f"{C}/devices/me/unpair", headers=h)).status_code == 401

    # The circle is told: one open notice, saying monitoring has stopped.
    client.act_as(fam, "admin", "workspace_admin")
    alerts = (await client.get(f"{C}/alerts", params={"person_id": person["id"], "open_only": "true"})).json()["alerts"]
    told = [a for a in alerts if a["kind"] == "watch_unpaired"]
    assert len(told) == 1 and told[0]["severity"] == "notice"
    assert "not watching over" in told[0]["details"]["summary"]
    devices = (await client.get(f"{C}/people/{person['id']}/devices")).json()["devices"]
    assert [d["status"] for d in devices] == ["revoked"]

    # Nobody else's watch is touched.
    assert (await client.get(f"{C}/devices/me", headers={"X-Device-Token": other_paired["device_token"]})).status_code == 200
    others = (await client.get(f"{C}/alerts", params={"person_id": other["id"], "open_only": "true"})).json()["alerts"]
    assert not [a for a in others if a["kind"] == "watch_unpaired"]

    # Pairing again works with a fresh code, as before.
    code = (await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})).json()["pairing_code"]
    again = await client.post(f"{C}/devices/pair", json={"pairing_code": code})
    assert again.status_code in (200, 201) and again.json()["device_token"] != paired["device_token"]
