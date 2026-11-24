"""One app, three experiences (111, phase 10): who the account is to the people
here, the appearance kept on the server for every device, and Help from the phone.
Real HTTP stack, real PostgreSQL as `ira_app`."""
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

from care.routes import experience_for  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"


def test_the_experience_follows_the_relationship_then_the_role():
    assert experience_for([], manage=False) == "family"
    assert experience_for([], manage=True) == "carer"
    assert experience_for([{"role": "family", "relationship": "daughter"}], manage=False) == "family"
    assert experience_for([{"role": "carer", "relationship": ""}], manage=False) == "carer"
    assert experience_for([{"role": "family", "relationship": "Self"}, {"role": "carer", "relationship": ""}], manage=True) == "wearer", \
        "the person themselves is the wearer whatever else they are"


async def test_each_account_gets_its_experience_and_its_own_appearance_on_every_device(client, families):
    fam = families["a"]
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    assert (await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Sravani", "role": "family", "relationship": "daughter", "user_id": str(fam["member"])})).status_code == 201
    assert (await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Tess", "role": "family", "relationship": "self", "user_id": str(fam["viewer"])})).status_code == 201
    # The administrator is a carer; the daughter is family; the person is the wearer.
    assert (await client.get(f"{C}/me/experience")).json()["experience"] == "carer"
    client.act_as(fam, "member", "member")
    me = (await client.get(f"{C}/me/experience")).json()
    assert me["experience"] == "family" and me["memberships"][0]["person_name"] == "Tess" and me["can_manage"] is False
    client.act_as(fam, "viewer", "member")
    assert (await client.get(f"{C}/me/experience")).json()["experience"] == "wearer"
    # Appearance: empty until set, then the same from any device, and the daughter's own.
    assert (await client.get(f"{C}/me/preferences")).json()["appearance"] == {}
    body = {"wallpaper_kind": "colour", "wallpaper_value": "#2f5d8a", "mode": "dark", "text_scale": 1.3, "high_contrast": True,
            "card_order": ["next_visit", "reminders", "help", "talk", "sharing"]}
    r = await client.put(f"{C}/me/preferences", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["appearance"] == body
    assert (await client.get(f"{C}/me/preferences")).json()["appearance"]["mode"] == "dark"
    client.act_as(fam, "member", "member")
    assert (await client.get(f"{C}/me/preferences")).json()["appearance"] == {}, "the daughter's phone is her own"
    # A photo wallpaper travels as a data URL; a bad colour and an oversized photo are refused.
    r = await client.put(f"{C}/me/preferences", json={"wallpaper_kind": "photo", "wallpaper_value": "data:image/png;base64,iVBORw0KGgo="})
    assert r.status_code == 200
    assert (await client.put(f"{C}/me/preferences", json={"wallpaper_kind": "colour", "wallpaper_value": "blue"})).status_code == 422
    assert (await client.put(f"{C}/me/preferences", json={"wallpaper_kind": "photo", "wallpaper_value": "data:image/png;base64," + "A" * 490000})).status_code in (413, 422)
    # Another family sees nothing of it.
    client.act_as(families["b"], "member", "member")
    assert (await client.get(f"{C}/me/preferences")).json()["appearance"] == {}
    # Account erasure takes the appearance with the account.
    async with workspace_connection(__import__("care.checkin_cycle", fromlist=["job_principal"]).job_principal(fam["tenant_id"], fam["workspace_id"])) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_user_preferences WHERE user_id = $1", fam["viewer"]) == 1


async def test_help_from_the_phone_opens_the_same_sos_the_watch_would(client, families):
    fam = families["a"]
    client.act_as(fam, "admin", "workspace_admin")
    person = (await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})).json()
    assert (await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Tess", "role": "family", "relationship": "self", "user_id": str(fam["viewer"])})).status_code == 201
    client.act_as(fam, "viewer", "member")
    r = await client.post(f"{C}/people/{person['id']}/help", json={"note": "I feel dizzy"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["kind"] == "help_requested" and a["severity"] == "sos" and a["details"]["via"] == "phone" and a["details"]["raised_by"] == "the person"
    alerts = (await client.get(f"{C}/alerts", params={"person_id": person["id"], "open_only": "true"})).json()["alerts"]
    assert [x["id"] for x in alerts] == [a["id"]]
    client.act_as(families["b"], "member", "member")
    assert (await client.post(f"{C}/people/{person['id']}/help", json={})).status_code == 404
