"""`POST /auth/step-up` with real tokens, real passwords and a real second factor (D-095).

The same harness as tests/tenancy/test_totp_enrolment_reauth.py: the real `/auth/token`,
`/auth/step-up`, `/auth/refresh`, `/auth/logout/all` and TOTP routes over HTTP, a migrated
PostgreSQL as `ira_app`, real bcrypt against the owner's and a household account's credentials,
sealed TOTP seeds, the real `require_auth`, and the account lockout against Redis or an in-memory
stand-in. Nothing on the authentication path is replaced.

What is proven here and nowhere else: a token that signed in carries this moment; a refresh carries
it forward unchanged; a step-up with the right password (and code) mints a token that satisfies
the recent-authentication check the old one failed; a step-up checks the password of the token's
own account; a signed-out-everywhere session cannot step up; failed attempts spend the sign-in
lockout.
"""
from __future__ import annotations

import os
import time

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
pytest.importorskip("httpx")
pytest.importorskip("pyotp")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy.test_totp_enrolment_reauth import (  # noqa: E402,F401
    HOUSEHOLD, HOUSEHOLD_PASSWORD, _bearer, _enabled_factor, _fail_count, _login, _now, _owner, _wrong,
    admin, client, lockout, owner_version_restored, pool,
)


def _claims(token: str) -> dict:
    from api.middleware.auth import decode_token
    return decode_token(token)


async def _step_up(client, headers, **body):
    return await client.post("/auth/step-up", json=body, headers=headers)


def _principal_of(token: str):
    """The Principal the API would build for this token (what `security/stepup.py` reads)."""
    from core.editions import EDITION_PRIVATE
    from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
    p = _claims(token)
    return Principal(user_id=private_user_id(p.sub), tenant_id=PRIVATE_TENANT_ID, session_id=__import__("uuid").uuid4(),
                     edition=EDITION_PRIVATE, username=p.sub).with_authentication(p.auth_time, p.amr or ())


async def test_sign_in_stamps_the_moment_and_a_refresh_carries_it_unchanged(client):
    from security import stepup

    before = int(time.time())
    r = await _login(client)
    assert r.status_code == 200, r.text
    access, refresh = _claims(r.json()["access_token"]), _claims(r.json()["refresh_token"])
    assert before <= access.auth_time <= int(time.time()) and access.amr == ["pwd"]
    assert refresh.auth_time == access.auth_time and refresh.amr == ["pwd"]

    time.sleep(1.1)
    renewed = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {r.json()['refresh_token']}"})
    assert renewed.status_code == 200, renewed.text
    again = _claims(renewed.json()["access_token"])
    assert again.auth_time == access.auth_time, "the refresh re-stamped the authentication time"
    assert again.amr == ["pwd"] and again.jti != access.jti

    # The refreshed session is a working one, and the check reads its real age, not the refresh.
    assert stepup.assess(_principal_of(renewed.json()["access_token"]), max_age=600, mfa_enrolled=False).ok
    assert stepup.assess(_principal_of(renewed.json()["access_token"]), max_age=0, mfa_enrolled=False).reason \
        == stepup.TOO_OLD


async def test_step_up_with_the_password_mints_a_recent_token_for_the_same_session(client, lockout):
    from security import stepup

    headers = await _bearer(client)
    old = _claims(headers["Authorization"].split(" ", 1)[1])
    _, password = _owner()
    time.sleep(1.1)
    r = await _step_up(client, headers, password=password)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "auth_time", "amr"}, body
    assert body["amr"] == ["pwd"] and body["auth_time"] > old.auth_time
    new = _claims(body["access_token"])
    assert (new.sub, new.uid, new.ver, new.tok) == (old.sub, old.uid, old.ver, "access")
    assert new.auth_time == body["auth_time"] and new.amr == ["pwd"]
    # The new token is a usable session token, and a recent one.
    me = await client.get("/auth/cloud/sessions", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code in (200, 404), me.text          # 404: the private edition has no cloud sessions
    assert stepup.assess(_principal_of(body["access_token"]), max_age=600, mfa_enrolled=False).ok
    assert await _fail_count(lockout) == 0


async def test_a_wrong_password_is_refused_and_spends_the_sign_in_lockout(client, lockout):
    headers = await _bearer(client)
    r = await _step_up(client, headers, password="not-the-owner-password-9Zq")
    assert r.status_code == 401 and "access_token" not in r.json(), r.text
    assert await _fail_count(lockout) == 1
    # Four more and the account is locked for sign-in too: one budget, not one per endpoint.
    for _ in range(4):
        await _step_up(client, headers, password="not-the-owner-password-9Zq")
    assert (await _login(client)).status_code == 429
    _, password = _owner()
    assert (await _step_up(client, headers, password=password)).status_code == 429


async def test_step_up_checks_the_password_of_the_tokens_own_account(client, admin, lockout):
    """A household session with the OWNER's password: refused. With its own: a token for itself."""
    from security.delegated import create_delegated_user

    await create_delegated_user(username=HOUSEHOLD, password=HOUSEHOLD_PASSWORD, role="trusted_user")
    headers = await _bearer(client, HOUSEHOLD, HOUSEHOLD_PASSWORD)
    _, owner_password = _owner()
    assert (await _step_up(client, headers, password=owner_password)).status_code == 401
    assert await _fail_count(lockout, HOUSEHOLD) == 1 and await _fail_count(lockout) == 0

    own = await _step_up(client, headers, password=HOUSEHOLD_PASSWORD)
    assert own.status_code == 200, own.text
    assert _claims(own.json()["access_token"]).sub == HOUSEHOLD
    assert await _fail_count(lockout, HOUSEHOLD) == 0


async def test_with_a_second_factor_the_code_is_required_and_recorded(client, admin, lockout):
    from security import stepup

    secret, headers = await _enabled_factor(client)
    _, password = _owner()
    # Signed in with the code: the token says so, and satisfies a check that insists on it.
    signed_in = _claims(headers["Authorization"].split(" ", 1)[1])
    assert signed_in.amr == ["pwd", "otp"]

    without = await _step_up(client, headers, password=password)
    assert without.status_code == 401 and without.json().get("second_factor") == "totp", without.text
    wrong = await _step_up(client, headers, password=password, totp_code=_wrong(secret))
    assert wrong.status_code == 401, wrong.text
    assert await _fail_count(lockout) == 2
    bad_password = await _step_up(client, headers, password="wrong-" + password, totp_code=_now(secret))
    assert bad_password.status_code == 401
    assert await _fail_count(lockout) == 3

    ok = await _step_up(client, headers, password=password, totp_code=_now(secret))
    assert ok.status_code == 200, ok.text
    assert ok.json()["amr"] == ["pwd", "otp"]
    p = _principal_of(ok.json()["access_token"])
    assert p.authentication_strength == "mfa"
    assert stepup.assess(p, max_age=600, mfa_enrolled=True).ok
    assert await _fail_count(lockout) == 0
    # The account's enrolment is what the check reads, and it reads it as enrolled.
    assert await stepup.mfa_enrolled(p) is True


async def test_a_session_signed_out_everywhere_cannot_step_up(client, owner_version_restored, lockout):
    headers = await _bearer(client)
    _, password = _owner()
    out = await client.post("/auth/logout/all", headers=headers)
    assert out.status_code == 200, out.text
    r = await _step_up(client, headers, password=password)
    assert r.status_code == 401 and "access_token" not in r.json(), r.text
    assert await _fail_count(lockout) == 0, "a revoked session never reached the password"
    # Control: a fresh sign-in steps up.
    fresh = await _bearer(client)
    assert (await _step_up(client, fresh, password=password)).status_code == 200


async def test_a_refresh_token_cannot_step_up_and_a_step_up_issues_no_refresh_token(client):
    r = await _login(client)
    refresh = {"Authorization": f"Bearer {r.json()['refresh_token']}"}
    _, password = _owner()
    assert (await _step_up(client, refresh, password=password)).status_code == 401
    stepped = await _step_up(client, {"Authorization": f"Bearer {r.json()['access_token']}"}, password=password)
    assert stepped.status_code == 200 and "refresh_token" not in stepped.json()
