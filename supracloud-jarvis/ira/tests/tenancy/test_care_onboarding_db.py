"""IRA CARE round 2, phase 6: a care organisation can start on its own.

Sign-up through the identity register (404 when disabled), the organisation
with its trial, inviting family by link with consent captured at invitation,
accepting as the invited account, the plan's limits, and the role and tenant
checks on every new route.
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

from core.notify import mail  # noqa: E402

C = "/api/v1/care"


async def _person(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    assert r.status_code == 201, r.text
    return r.json()


async def _verified_user(email: str) -> uuid.UUID:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        uid = uuid.uuid4()
        await admin.execute("INSERT INTO users (id, email, email_verified_at, display_name, auth_provider, status) "
                            "VALUES ($1, $2, NOW(), 'invited', 'password', 'active')", uid, email)
        return uid
    finally:
        await admin.close()


@pytest.fixture
def console_mail(monkeypatch):
    monkeypatch.setenv("IRA_MAIL_TRANSPORT", "console")
    mail.clear_outbox()
    yield mail
    mail.clear_outbox()


async def test_signup_is_closed_unless_enabled_and_never_says_whether_an_address_exists(client, monkeypatch, console_mail):
    r = await client.post(f"{C}/signup", json={"email": "founder@example.invalid", "password": "a-long-enough-password-01"})
    assert r.status_code == 404
    from config import get_settings
    monkeypatch.setattr(get_settings(), "ira_cloud_registration_enabled", True, raising=False)
    first = await client.post(f"{C}/signup", json={"email": f"founder-{uuid.uuid4().hex[:8]}@example.invalid", "password": "a-long-enough-password-01"})
    assert first.status_code == 202, first.text
    again = await client.post(f"{C}/signup", json=first.request.content and {"email": "founder@example.invalid", "password": "a-long-enough-password-01"})
    assert again.status_code == 202 and again.json() == first.json(), "the same answer whether or not the address was new"
    assert any("Confirm" in m.subject for m in console_mail.outbox()), "the confirmation link went through the mail provider (console)"
    short = await client.post(f"{C}/signup", json={"email": "x@example.invalid", "password": "short"})
    assert short.status_code == 422


async def test_an_invitation_creates_the_pending_member_records_consent_and_mails_a_link(client, families, console_mail):
    fam = families["a"]
    p = await _person(client, fam)
    body = {"email": "daughter@example.invalid", "display_name": "Demo Daughter", "relationship": "daughter", "role": "family",
            "tier": 1, "phone": "+440000000021",
            "consents": [{"kind": "transcripts_family", "granted": True, "granted_by": "person", "granted_by_name": "Tess"},
                         {"kind": "health_data_family", "granted": True, "granted_by": "person", "granted_by_name": "Tess"}]}
    r = await client.post(f"{C}/people/{p['id']}/invitations", json=body)
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["link"].startswith("http://localhost:3000/join?token=") and inv["consents_recorded"] == ["transcripts_family", "health_data_family"]
    assert inv["mail"] == {"delivered": True, "transport": "console"}
    assert any(inv["link"] in m.body and m.to == "daughter@example.invalid" for m in console_mail.outbox())
    circle = (await client.get(f"{C}/people/{p['id']}/circle")).json()["members"]
    pending = [m for m in circle if m["invited_email"] == "daughter@example.invalid"]
    assert len(pending) == 1 and pending[0]["user_id"] is None and pending[0]["role"] == "family"
    consents = (await client.get(f"{C}/people/{p['id']}/consents")).json()
    kinds = {c["kind"] for c in consents["consents"]}
    assert {"transcripts_family", "health_data_family"} <= kinds
    listed = (await client.get(f"{C}/people/{p['id']}/invitations")).json()["invitations"]
    assert listed[0]["accepted"] is False and listed[0]["invited_email"] == "daughter@example.invalid"
    # The invitation is in the access log as a consent event.
    log = (await client.get(f"{C}/people/{p['id']}/access-log")).json()["entries"]
    assert any(e["resource_kind"] == "consent" and e["detail"].get("event") == "invitation" for e in log)
    # The role and the address are checked.
    assert (await client.post(f"{C}/people/{p['id']}/invitations", json=dict(body, role="owner"))).status_code == 422
    assert (await client.post(f"{C}/people/{p['id']}/invitations", json=dict(body, email="not-an-address"))).status_code == 422


async def test_accepting_the_link_as_the_invited_account_joins_the_circle(client, families, console_mail):
    fam = families["a"]
    p = await _person(client, fam)
    email = f"son-{uuid.uuid4().hex[:8]}@example.invalid"
    r = await client.post(f"{C}/people/{p['id']}/invitations", json={"email": email, "role": "family", "relationship": "son"})
    token = r.json()["link"].split("token=", 1)[1]
    from urllib.parse import unquote
    token = unquote(token)
    uid = await _verified_user(email)
    # The acceptance route authenticates the invitee by their own subject (email), no tenant named.
    from api.middleware.auth import require_auth
    client.app.dependency_overrides[require_auth] = lambda: email
    try:
        r = await client.post(f"{C}/invitations/accept", json={"token": token})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["tenant_id"] == str(fam["tenant_id"]) and out["person_id"] == p["id"] and out["circle_role"] == "family"
        # Once only.
        assert (await client.post(f"{C}/invitations/accept", json={"token": token})).status_code == 400
        # A stranger's subject with no account is told to sign in.
        client.app.dependency_overrides[require_auth] = lambda: "nobody@example.invalid"
        assert (await client.post(f"{C}/invitations/accept", json={"token": token})).status_code == 401
    finally:
        client.app.dependency_overrides.pop(require_auth, None)
    client.act_as(fam, "admin", "workspace_admin")
    circle = (await client.get(f"{C}/people/{p['id']}/circle")).json()["members"]
    linked = [m for m in circle if m["invited_email"] == email]
    assert linked and linked[0]["user_id"] == str(uid)
    assert (await client.get(f"{C}/people/{p['id']}/invitations")).json()["invitations"][0]["accepted"] is True


async def test_the_plan_limits_people_and_circle_members(client, families):
    fam = families["a"]
    client.act_as(fam, "admin", "workspace_admin")
    before = (await client.get(f"{C}/plan")).json()
    assert before["subscription"] is None and before["limits"] == {} and {p["plan_id"] for p in before["plans"]} == {"care_family", "care_organisation"}
    assert before["payment_provider"] == "off"
    p = await _person(client, fam)
    from care import onboarding
    from tests.tenancy._care_fixtures import _principal
    await onboarding.seed_trial(_principal(fam, "admin", "workspace_admin"), "care_family")
    after = (await client.get(f"{C}/plan")).json()
    assert after["subscription"]["plan_id"] == "care_family" and after["entitled"] is True
    assert after["limits"] == {"care_people_max": 1, "care_circle_members_max": 6} and after["usage"]["care_people"] == 1
    r = await client.post(f"{C}/people", json={"display_name": "Second Person", "preferred_name": "", "timezone": "UTC"})
    assert r.status_code == 402 and "plan allows 1" in r.text
    for i in range(6):
        await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": f"Member {i}", "role": "family", "tier": 1})
    r = await client.post(f"{C}/people/{p['id']}/invitations", json={"email": "seventh@example.invalid", "role": "family"})
    assert r.status_code == 402 and "circle of 6" in r.text


async def test_role_and_tenant_checks_on_every_new_route(client, families, console_mail):
    fam, other = families["a"], families["b"]
    p = await _person(client, fam)
    for who, role in (("viewer", "viewer"), ("member", "member")):
        client.act_as(fam, who, role)
        assert (await client.post(f"{C}/people/{p['id']}/invitations", json={"email": "x@example.invalid"})).status_code == 403
        assert (await client.get(f"{C}/people/{p['id']}/invitations")).status_code == 403
        assert (await client.get(f"{C}/plan")).status_code == 403
    client.act_as(other, "admin", "workspace_admin")
    assert (await client.post(f"{C}/people/{p['id']}/invitations", json={"email": "x@example.invalid"})).status_code == 404
    assert (await client.get(f"{C}/people/{p['id']}/invitations")).status_code == 404
    assert (await client.get(f"{C}/plan")).json()["usage"]["care_people"] == 0, "the other family sees only its own usage"
