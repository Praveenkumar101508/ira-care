"""A deployment administrator must have proved who they are recently before touching a watch
(D-095, security review S15, threat T43; care/routes.py, security/stepup.py), against a real
database through the real app.

The administrator's session arrives as a Principal carrying `auth_time` and `amr`, exactly as
`principal_from_username` builds one from the access token's claims (tests/test_step_up_authentication.py
proves that mapping and the token side). Here: who is refused, with what, and what still works.
The grant is checked first, so an account without one learns nothing about step-up; the history
needs no step-up; D-089's two-person rule for a factory reset is untouched by a fresh session.
"""
from __future__ import annotations

import asyncio
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

from tests.tenancy._care_fixtures import _principal, client, families, pool  # noqa: E402,F401
from tests.tenancy.test_device_admin_db import (  # noqa: E402,F401
    ISSUE, KEY, REASON, _approve, _body, _collect, _history, _request_reset, _row, _target,
    _verify_as_the_watch, _watch, act_as_admin, admins, policy_key,
)

from care import deviceadmin  # noqa: E402

ME = f"{ISSUE.rsplit('/', 1)[0]}/me"
STALE = 601          # one second past the default ten-minute window
MAX_AGE = 600


def _is_step_up(r) -> bool:
    return r.status_code == 403 and r.json().get("error_code") == "step_up_required"


def _details(r) -> dict:
    return r.json()["safe_details"]


def _is_admin_refusal(r) -> bool:
    """The refusal an account without a grant has always had: nothing about step-up in it."""
    body = r.json()
    return r.status_code == 403 and body.get("error_code") == "permission_denied" and \
        body.get("message") == deviceadmin.REFUSED and "safe_details" not in body and "step" not in r.text.lower()


async def _enrol_factor(conn, fam, uid: uuid.UUID, *, enabled: bool = True) -> None:
    """An enabled (or merely started) second factor for the account, as the operator's connection
    sees the table: existence is all the step-up check reads, never the seed."""
    await conn.execute(
        "INSERT INTO totp_secrets (tenant_id, user_id, username, secret_ciphertext, secret_nonce, "
        "encryption_key_version, encrypted_at, migration_status, enabled) "
        "VALUES ($1, $2, $3, '\\xcd'::bytea, '\\xef'::bytea, 1, NOW(), 'ENCRYPTED', $4)",
        fam["tenant_id"], uid, f"ops-{uid.hex[:8]}@example.invalid", enabled)


# ─────────────────────────────────────────────────────────────────────────────
# Who is refused, and with what
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_stale_session_is_refused_on_issue_approve_and_revoke_and_told_why(client, families, admins,
                                                                                    policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    first, second = await admins(), await admins()

    # A session that signed in eleven minutes ago (or a stolen access token kept alive by
    # refreshes): a deployment administrator, and still refused.
    act_as_admin(client, families, first, auth_age=STALE)
    r = await client.post(ISSUE, json=_body(fam, device))
    assert _is_step_up(r), r.text
    detail = _details(r)
    assert detail["max_age_seconds"] == MAX_AGE and STALE <= detail["auth_age_seconds"] < STALE + 60
    assert detail["reason"] == "authentication_too_old" and detail["how"] == "POST /auth/step-up"
    assert "re-authentication" in r.json()["message"].lower()
    assert await _history(client, fam, device) == [], "a refused issue left a row behind"

    # Control: the same administrator, signed in just now, is served; the history never needed
    # step-up and the stale session can still read it.
    act_as_admin(client, families, first)
    issued = (await client.post(ISSUE, json=_body(fam, device))).json()
    act_as_admin(client, families, first, auth_age=STALE)
    assert [i["id"] for i in await _history(client, fam, device)] == [issued["id"]]

    # Revoke, stale: refused; fresh: done.
    revoke = {**_target(fam), "reason": "support ticket 1234: closed"}
    assert _is_step_up(await client.post(f"{ISSUE}/{issued['id']}/revoke", json=revoke))
    assert (await _row(issued["id"]))["revoked_at"] is None
    act_as_admin(client, families, first)
    assert (await client.post(f"{ISSUE}/{issued['id']}/revoke", json=revoke)).status_code == 200

    # Approve, stale: refused before the two-person rule is even consulted; the request stays
    # pending and no `approval_refused` event is written (the step-up refusal is not an approval).
    req = await _request_reset(client, families, first, device)
    act_as_admin(client, families, second, auth_age=STALE)
    assert _is_step_up(await client.post(f"{ISSUE}/{req['id']}/approve", json=_target(fam)))
    assert (await _row(req["id"]))["state"] == "pending_approval"
    events = [e["event"] for i in await _history(client, fam, device) if i["id"] == req["id"] for e in i["events"]]
    assert "approval_refused" not in events and "approved" not in events
    act_as_admin(client, families, second)
    assert (await client.post(f"{ISSUE}/{req['id']}/approve", json=_target(fam))).status_code == 200


async def test_a_session_without_an_authentication_time_is_refused_only_where_step_up_is_asked(
        client, families, admins, policy_key):
    """A token issued before the claims existed: still a working session everywhere else."""
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    uid = await admins()
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    client.principal = Principal(user_id=uid, tenant_id=families["b"]["tenant_id"], session_id=uuid.uuid4(),
                                 edition=EDITION_CLOUD)
    assert client.principal.auth_time is None and client.principal.authentication_strength == ""
    r = await client.post(ISSUE, json=_body(fam, device))
    assert _is_step_up(r), r.text
    assert "auth_age_seconds" not in _details(r) and _details(r)["reason"] == "no_authentication_time"
    assert await _history(client, fam, device) == []            # an ordinary read still works
    me = await client.get(ME)
    assert me.status_code == 200 and me.json() == {"deployment_admin": True, "auth_age_seconds": None,
                                                    "step_up_required": True, "max_age_seconds": MAX_AGE,
                                                    "mfa_enrolled": False}


async def test_a_non_administrator_with_a_fresh_session_still_gets_the_administrator_refusal(
        client, families, admins, policy_key):
    """The grant comes first: nobody without one learns that step-up exists."""
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    ungranted = await admins(grant=False)
    before = len(await deviceadmin.history(tenant_id=fam["tenant_id"], workspace_id=fam["workspace_id"],
                                           device_id=uuid.UUID(device)))
    for who, age in ((ungranted, 0), (ungranted, STALE)):
        act_as_admin(client, families, who, auth_age=age, amr=("pwd", "otp"))
        assert _is_admin_refusal(await client.post(ISSUE, json=_body(fam, device)))
        assert _is_admin_refusal(await client.post(f"{ISSUE}/{uuid.uuid4()}/approve", json=_target(fam)))
        assert _is_admin_refusal(await client.post(f"{ISSUE}/{uuid.uuid4()}/revoke",
                                                   json={**_target(fam), "reason": "support ticket 1234: x"}))
    # A care manager of the workspace, freshly signed in with a second factor: the same.
    client.principal = _principal(fam, "admin", "workspace_admin").with_authentication(
        int(dt.datetime.now(dt.timezone.utc).timestamp()), ("pwd", "otp"))
    assert _is_admin_refusal(await client.post(ISSUE, json=_body(fam, device)))
    me = await client.get(ME)
    assert me.status_code == 200 and me.json() == {"deployment_admin": False}
    assert len(await deviceadmin.history(tenant_id=fam["tenant_id"], workspace_id=fam["workspace_id"],
                                         device_id=uuid.UUID(device))) == before


async def test_an_administrator_with_a_second_factor_must_have_shown_it(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    uid = await admins()
    await _enrol_factor(admins.conn, families["b"], uid)

    # Signed in a moment ago with the password alone: refused, and told which part is missing.
    act_as_admin(client, families, uid, amr=("pwd",))
    r = await client.post(ISSUE, json=_body(fam, device))
    assert _is_step_up(r), r.text
    assert _details(r)["reason"] == "second_factor_not_shown"
    me = (await client.get(ME)).json()
    assert me["mfa_enrolled"] is True and me["step_up_required"] is True and me["auth_age_seconds"] < 60

    # With the code (or a recovery code): served.
    for amr in (("pwd", "otp"), ("pwd", "recovery")):
        act_as_admin(client, families, uid, amr=amr)
        assert (await client.get(ME)).json()["step_up_required"] is False
        assert (await client.post(ISSUE, json=_body(fam, device))).status_code == 201

    # Control: an enrolment that was started and never confirmed is not a factor anybody signs
    # in with, so the password alone is enough for THAT account.
    other = await admins()
    await _enrol_factor(admins.conn, families["b"], other, enabled=False)
    act_as_admin(client, families, other, amr=("pwd",))
    assert (await client.get(ME)).json()["mfa_enrolled"] is False
    assert (await client.post(ISSUE, json=_body(fam, device))).status_code == 201


async def test_me_reports_the_sessions_age_against_the_window(client, families, admins, policy_key):
    uid = await admins()
    act_as_admin(client, families, uid, auth_age=120)
    me = (await client.get(ME)).json()
    assert me["deployment_admin"] is True and me["step_up_required"] is False
    assert 120 <= me["auth_age_seconds"] < 180 and me["max_age_seconds"] == MAX_AGE and me["mfa_enrolled"] is False
    act_as_admin(client, families, uid, auth_age=MAX_AGE + 1)
    assert (await client.get(ME)).json()["step_up_required"] is True


async def test_the_window_is_the_configured_one(client, families, admins, policy_key, monkeypatch):
    """A deployment that sets IRA_STEP_UP_MAX_AGE_SECONDS=60 refuses a two-minute-old session."""
    from config import get_settings
    token, device = await _watch(client, families["a"])
    uid = await admins()
    monkeypatch.setenv("IRA_STEP_UP_MAX_AGE_SECONDS", "60")
    get_settings.cache_clear()
    try:
        act_as_admin(client, families, uid, auth_age=120)
        r = await client.post(ISSUE, json=_body(families["a"], device))
        assert _is_step_up(r) and _details(r)["max_age_seconds"] == 60, r.text
        act_as_admin(client, families, uid, auth_age=30)
        assert (await client.post(ISSUE, json=_body(families["a"], device))).status_code == 201
    finally:
        monkeypatch.delenv("IRA_STEP_UP_MAX_AGE_SECONDS")
        get_settings.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# The two-person rule is untouched by a fresh session
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_fresh_session_does_not_let_one_person_reset_a_watch(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    requester = await admins()
    req = await _request_reset(client, families, requester, device)
    assert req["state"] == "pending_approval"
    # The requester, signed in with password and code a moment ago, still may not approve.
    act_as_admin(client, families, requester, amr=("pwd", "otp"))
    r = await client.post(f"{ISSUE}/{req['id']}/approve", json=_target(fam))
    assert r.status_code == 403 and not _is_admin_refusal(r) and not _is_step_up(r), r.text
    assert (await _row(req["id"]))["state"] == "pending_approval"
    assert (await _collect(client, token))["instructions"] == []


async def test_concurrent_approvals_after_step_up_sign_the_reset_exactly_once(client, families, admins, policy_key):
    """Two other administrators, both freshly stepped up, approve at the same moment: one signs it,
    the other finds it already signed; the requester, racing them, is refused."""
    from fastapi import Header

    from api.middleware.workspace import require_tenant_principal
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    requester, a, b = await admins(), await admins(), await admins()
    req = await _request_reset(client, families, requester, device)

    actors = {}
    for name, uid in (("requester", requester), ("a", a), ("b", b)):
        actors[name] = act_as_admin(client, families, uid, amr=("pwd", "otp")).principal

    async def pick(x_test_actor: str | None = Header(default=None)):
        return actors[x_test_actor] if x_test_actor else client.principal
    client.app.dependency_overrides[require_tenant_principal] = pick

    answers = await asyncio.gather(*(
        client.post(f"{ISSUE}/{req['id']}/approve", json=_target(fam), headers={"X-Test-Actor": who})
        for who in ("a", "b", "requester")))
    by_actor = dict(zip(("a", "b", "requester"), answers))
    codes = sorted(r.status_code for r in answers)
    # Exactly one signs it. The requester is refused either as the requester (403) or, if the
    # race was already won, as approving a request no longer waiting (409); never served.
    assert codes in ([200, 403, 409], [200, 409, 409]), [(r.status_code, r.text) for r in answers]
    assert by_actor["requester"].status_code in (403, 409)
    row = await _row(req["id"])
    assert row["state"] == "signed" and row["approved_by"] in (a, b) and row["approved_by"] != requester
    items = (await _collect(client, token))["instructions"]
    assert len(items) == 1 and _verify_as_the_watch(items[0], device)["action"] == "FACTORY_RESET"
    # One signed reset, not two: the loser's approval wrote nothing the watch could be given.
    assert len((await _collect(client, token))["instructions"]) == 1


async def test_a_request_still_lapses_whatever_the_approvers_session(client, families, admins, policy_key):
    token, device = await _watch(client, families["a"])
    fam = families["a"]
    requester, second = await admins(), await admins()
    asked = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=31)
    req = await deviceadmin.issue(admin_id=requester, tenant_id=fam["tenant_id"], workspace_id=fam["workspace_id"],
                                  device_id=uuid.UUID(device), action="FACTORY_RESET", minutes=10, reason=REASON,
                                  key=KEY, now=asked)
    act_as_admin(client, families, second, amr=("pwd", "otp"))
    r = await client.post(f"{ISSUE}/{req['id']}/approve", json=_target(fam))
    assert r.status_code == 409, r.text
    assert (await _row(req["id"]))["state"] == "pending_approval"
    assert (await _collect(client, token))["instructions"] == []
    # Control: inside the window, the same fresh approver signs it.
    ok = await deviceadmin.issue(admin_id=requester, tenant_id=fam["tenant_id"], workspace_id=fam["workspace_id"],
                                 device_id=uuid.UUID(device), action="FACTORY_RESET", minutes=10, reason=REASON,
                                 key=KEY, now=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=29))
    assert (await _approve(client, families, second, ok["id"])).status_code == 200
