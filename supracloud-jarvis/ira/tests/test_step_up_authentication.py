"""Step-up authentication without a database (D-095, security review S15, threat T43).

The session token now says when its holder last proved who they are (`auth_time`) and how
(`amr`); a refresh copies both and never re-stamps them; `POST /auth/step-up` re-checks the
password (and the second factor) of the token's own subject and mints an access token stamped
with this moment; `security/stepup.py` refuses an action unless that moment is recent enough.

Everything here runs the real token code (`api/middleware/auth.py`), the real auth routes and the
real dependency, with the credential store, the second-factor store and the lockout replaced by
doubles. The database half is tests/tenancy/test_device_admin_step_up_db.py and
tests/tenancy/test_step_up_endpoint_db.py.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.token_version_store import token_version_store

_REQUIRED_ENV = {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-db-pass",
    "REDIS_PASSWORD": "ci-redis-pass",
    "VLLM_API_KEY": "ci-vllm-key",
    "DEV_MODE": "false",
}

_SECRETS = dict(ira_secret_key="ci-secret", ira_admin_password="ci-admin",
                postgres_password="ci-db", redis_password="ci-redis")


@pytest.fixture(autouse=True)
def env():
    from config import get_settings

    with patch.dict(os.environ, _REQUIRED_ENV, clear=False):
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()


def _claims(token: str) -> dict:
    import jwt

    from config import get_settings
    return jwt.decode(token, get_settings().ira_secret_key, algorithms=["HS256"])


def _sign(**claims) -> str:
    """A token signed with the real key but built by hand: how a token minted BEFORE the claims
    existed looks, or one with any claim the constructors would not produce."""
    import jwt

    from config import get_settings
    payload = {"sub": "alice", "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
               "jti": str(uuid.uuid4()), "tok": "access", "ver": 0, **claims}
    return jwt.encode(payload, get_settings().ira_secret_key, algorithm="HS256")


def _redis(*, version=None, revoked=0):
    r = AsyncMock()
    r.exists = AsyncMock(return_value=revoked)
    r.get = AsyncMock(return_value=version)
    r.incr = AsyncMock(return_value=1)
    r.setex = AsyncMock(return_value=True)
    return r


def _principal(auth_time=None, amr=()):
    from core.editions import EDITION_PRIVATE
    from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
    return Principal(user_id=private_user_id("alice"), tenant_id=PRIVATE_TENANT_ID, session_id=uuid.uuid4(),
                     edition=EDITION_PRIVATE, username="alice").with_authentication(auth_time, amr)


# ─────────────────────────────────────────────────────────────────────────────
# 1. The claims
# ─────────────────────────────────────────────────────────────────────────────


def test_both_tokens_carry_the_authentication_time_and_methods():
    from api.middleware.auth import TokenPayload, _make_access_token, _make_refresh_token

    now = int(time.time())
    for make in (_make_access_token, _make_refresh_token):
        token, _, _ = make("alice", ver=3, auth_time=now, amr=["pwd", "otp"])
        claims = _claims(token)
        assert claims["auth_time"] == now and claims["amr"] == ["pwd", "otp"], claims
        decoded = TokenPayload(**claims)
        assert decoded.auth_time == now and decoded.amr == ["pwd", "otp"]
    # Negative control: without an authentication time neither claim is written at all.
    token, _, _ = _make_access_token("alice", ver=3)
    assert "auth_time" not in _claims(token) and "amr" not in _claims(token)


def test_a_token_issued_before_the_claims_existed_still_decodes():
    from api.middleware.auth import decode_token

    payload = decode_token(_sign())
    assert payload.auth_time is None and payload.amr is None


@pytest.mark.asyncio
async def test_login_stamps_this_moment_and_the_methods_checked():
    from api.middleware.auth import create_login_tokens

    before = int(time.time())
    with token_version_store(4):
        plain = await create_login_tokens("alice")
        with_code = await create_login_tokens("alice", amr=("pwd", "otp"))
    for tokens, amr in ((plain, ["pwd"]), (with_code, ["pwd", "otp"])):
        for token in (tokens.access_token, tokens.refresh_token):
            claims = _claims(token)
            assert before <= claims["auth_time"] <= int(time.time()) + 1
            assert claims["amr"] == amr


def test_the_principal_derives_its_strength_from_the_methods():
    now = int(time.time())
    assert _principal(None, ()).authentication_strength == ""
    assert _principal(now, ("pwd",)).authentication_strength == "password"
    assert _principal(now, ("pwd", "otp")).authentication_strength == "mfa"
    assert _principal(now, ("pwd", "recovery")).authentication_strength == "mfa"
    p = _principal(now, ["pwd", "otp"])
    assert p.auth_time == now and p.amr == ("pwd", "otp")


# ─────────────────────────────────────────────────────────────────────────────
# 2. A refresh copies the moment; it never re-stamps it
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def auth_app(monkeypatch):
    async def _active(_username):
        return "trusted_user"

    monkeypatch.setattr("security.delegated.get_delegated_role", _active)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from auth.routes import register_auth_routes

    limiter = Limiter(key_func=lambda _r: "test", enabled=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    return TestClient(app)


def test_a_refresh_preserves_the_authentication_time(auth_app):
    """A session kept alive by refreshes for a week still says it was authenticated a week ago."""
    from api.middleware.auth import _make_refresh_token

    week_ago = int(time.time()) - 7 * 24 * 3600
    refresh, _, _ = _make_refresh_token("alice", ver=2, auth_time=week_ago, amr=["pwd", "otp"])
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(2):
        r = auth_app.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 200, r.text
    claims = _claims(r.json()["access_token"])
    assert claims["auth_time"] == week_ago, "the refresh re-stamped the authentication time"
    assert claims["amr"] == ["pwd", "otp"]
    assert "refresh_token" not in r.json()


def test_a_refresh_of_a_token_without_the_claims_mints_one_without_them(auth_app):
    from api.middleware.auth import _make_refresh_token

    refresh, _, _ = _make_refresh_token("alice", ver=2)
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(2):
        r = auth_app.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 200, r.text
    claims = _claims(r.json()["access_token"])
    assert "auth_time" not in claims and "amr" not in claims


# ─────────────────────────────────────────────────────────────────────────────
# 3. The decision
# ─────────────────────────────────────────────────────────────────────────────


def test_the_decision_on_age_and_factor():
    from security import stepup

    now = 1_800_000_000
    fresh = _principal(now - 30, ("pwd",))
    assert stepup.assess(fresh, max_age=600, mfa_enrolled=False, now=now).ok
    # The boundary: exactly max_age is still recent; one second more is not.
    assert stepup.assess(_principal(now - 600, ("pwd",)), max_age=600, mfa_enrolled=False, now=now).ok
    old = stepup.assess(_principal(now - 601, ("pwd",)), max_age=600, mfa_enrolled=False, now=now)
    assert old.reason == stepup.TOO_OLD and old.auth_age_seconds == 601
    # No time at all: never recently authenticated, and no age to report.
    legacy = stepup.assess(_principal(None, ()), max_age=600, mfa_enrolled=False, now=now)
    assert legacy.reason == stepup.NO_AUTH_TIME and legacy.auth_age_seconds is None
    # A second factor is enrolled and was not shown.
    pwd_only = stepup.assess(fresh, max_age=600, mfa_enrolled=True, now=now)
    assert pwd_only.reason == stepup.SECOND_FACTOR_MISSING
    assert stepup.assess(_principal(now - 30, ("pwd", "otp")), max_age=600, mfa_enrolled=True, now=now).ok
    assert stepup.assess(_principal(now - 30, ("pwd", "recovery")), max_age=600, mfa_enrolled=True, now=now).ok
    # ... unless the caller asked not to insist on it.
    assert stepup.assess(fresh, max_age=600, mfa_enrolled=True, require_mfa_if_enrolled=False, now=now).ok
    # A clock ahead of the token reads as age 0, never as a negative age.
    assert stepup.auth_age_seconds(_principal(now + 50, ("pwd",)), now=now) == 0


def test_the_refusal_names_the_code_the_window_and_the_age():
    from core.runtime.errors import STEP_UP_REQUIRED
    from security import stepup

    now = 1_800_000_000
    exc = stepup.refusal(stepup.assess(_principal(now - 900, ("pwd",)), max_age=600, mfa_enrolled=False, now=now))
    assert exc.status_code == 403 and STEP_UP_REQUIRED.status == 403
    body = exc.to_response()
    assert body["error_code"] == "step_up_required" == STEP_UP_REQUIRED.code
    assert "re-authentication" in body["message"].lower()
    details = body["safe_details"]
    assert details["max_age_seconds"] == 600 and details["auth_age_seconds"] == 900
    assert details["reason"] == stepup.TOO_OLD and details["how"] == "POST /auth/step-up"
    no_time = stepup.refusal(stepup.assess(_principal(None, ()), max_age=600, mfa_enrolled=False, now=now))
    assert "auth_age_seconds" not in no_time.safe_details and no_time.safe_details["max_age_seconds"] == 600


@pytest.mark.parametrize("seconds", [60, 600, 3600])
def test_the_window_setting_within_its_bounds_is_accepted(seconds):
    from config import Settings
    assert Settings(**_SECRETS, ira_step_up_max_age_seconds=seconds).ira_step_up_max_age_seconds == seconds


@pytest.mark.parametrize("seconds", [59, 0, -1, 3601, 86400])
def test_the_window_setting_outside_its_bounds_is_refused_at_start(seconds):
    from config import Settings
    with pytest.raises(RuntimeError, match="IRA_STEP_UP_MAX_AGE_SECONDS"):
        Settings(**_SECRETS, ira_step_up_max_age_seconds=seconds)


def test_the_default_window_is_ten_minutes():
    from config import Settings
    from security import stepup
    assert Settings(**_SECRETS).ira_step_up_max_age_seconds == 600
    assert stepup.max_age_seconds() == 600


# ─────────────────────────────────────────────────────────────────────────────
# 4. The dependency
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def protected_app(monkeypatch):
    """A route behind `require_recent_authentication`, with the caller's principal injected and
    the second-factor enrolment answered by `app.state.enrolled` (an exception when it is one)."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from core.identity.principal import Principal
    from core.runtime.errors import install_error_handlers
    from security import stepup

    app = FastAPI()
    install_error_handlers(app)
    app.state.principal = None
    app.state.enrolled = False

    async def whoami() -> Principal:
        return app.state.principal

    async def enrolled(_principal):
        if isinstance(app.state.enrolled, Exception):
            raise app.state.enrolled
        return app.state.enrolled

    monkeypatch.setattr(stepup, "mfa_enrolled", enrolled)

    @app.post("/guarded")
    async def guarded(p: Principal = Depends(stepup.require_recent_authentication(principal=whoami))):
        return {"ok": True, "age": stepup.auth_age_seconds(p)}

    @app.post("/guarded-short")
    async def guarded_short(p: Principal = Depends(stepup.require_recent_authentication(120, principal=whoami))):
        return {"ok": True}

    @app.post("/guarded-no-mfa")
    async def guarded_no_mfa(p: Principal = Depends(
            stepup.require_recent_authentication(require_mfa_if_enrolled=False, principal=whoami))):
        return {"ok": True}

    @app.post("/open")
    async def open_route(p: Principal = Depends(whoami)):
        return {"ok": True}

    return TestClient(app)


def test_the_dependency_admits_a_recent_session_and_refuses_a_stale_one(protected_app):
    now = int(time.time())
    protected_app.app.state.principal = _principal(now - 5, ("pwd",))
    assert protected_app.post("/guarded").status_code == 200
    protected_app.app.state.principal = _principal(now - 601, ("pwd",))
    r = protected_app.post("/guarded")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error_code"] == "step_up_required" and body["safe_details"]["max_age_seconds"] == 600
    assert 600 < body["safe_details"]["auth_age_seconds"] < 700
    assert "re-authentication" in body["message"].lower()
    # Stale for the route, still fine for an ordinary one.
    assert protected_app.post("/open").status_code == 200


def test_a_legacy_session_without_the_claims_is_refused_only_where_step_up_is_asked(protected_app):
    protected_app.app.state.principal = _principal(None, ())
    r = protected_app.post("/guarded")
    assert r.status_code == 403 and r.json()["error_code"] == "step_up_required"
    assert "auth_age_seconds" not in r.json()["safe_details"]
    assert protected_app.post("/open").status_code == 200


def test_the_route_may_narrow_the_window(protected_app):
    protected_app.app.state.principal = _principal(int(time.time()) - 300, ("pwd",))
    assert protected_app.post("/guarded").status_code == 200
    assert protected_app.post("/guarded-short").status_code == 403


def test_an_enrolled_second_factor_must_have_been_shown(protected_app):
    now = int(time.time())
    protected_app.app.state.enrolled = True
    protected_app.app.state.principal = _principal(now - 5, ("pwd",))
    r = protected_app.post("/guarded")
    assert r.status_code == 403 and r.json()["safe_details"]["reason"] == "second_factor_not_shown"
    assert protected_app.post("/guarded-no-mfa").status_code == 200
    protected_app.app.state.principal = _principal(now - 5, ("pwd", "otp"))
    assert protected_app.post("/guarded").status_code == 200


def test_an_unreadable_enrolment_refuses_rather_than_skipping_the_factor(protected_app):
    from security import stepup
    protected_app.app.state.enrolled = stepup.EnrolmentUnknown("down")
    protected_app.app.state.principal = _principal(int(time.time()) - 5, ("pwd",))
    assert protected_app.post("/guarded").status_code == 503


# ─────────────────────────────────────────────────────────────────────────────
# 5. POST /auth/step-up, with the stores replaced
# ─────────────────────────────────────────────────────────────────────────────


class _Stores:
    """What the route consults: the account password, the enabled factor, the lockout."""

    def __init__(self):
        self.password = "right-password"
        self.enrolled_secret = None      # a pyotp seed when a factor is enabled
        self.fails: dict[str, int] = {}
        self.locked: set[str] = set()
        self.audit: list[tuple[str, str]] = []
        self.password_unavailable = False

    async def verify_account_password(self, username, password, *, principal):
        from security.reauth import ReauthUnavailable
        if self.password_unavailable:
            raise ReauthUnavailable("down")
        return (username, password) in {("alice", self.password), ("bob", "bobs-password")}

    async def verify_active_factor(self, username, code, *, principal=None):
        import pyotp

        from security.totp import FACTOR_BAD, FACTOR_OK, NOT_ENROLLED, ActiveFactor
        if self.enrolled_secret is None:
            return NOT_ENROLLED, None
        factor = ActiveFactor(row_id="f1", enrolled_at=datetime.now(timezone.utc))
        if code and pyotp.TOTP(self.enrolled_secret).verify(code.strip(), valid_window=1):
            return FACTOR_OK, factor
        return FACTOR_BAD, factor

    async def is_locked(self, key):
        return key in self.locked

    async def record_failure(self, key):
        self.fails[key] = self.fails.get(key, 0) + 1
        if self.fails[key] >= 5:
            self.locked.add(key)
        return self.fails[key], key in self.locked

    async def clear_failures(self, key):
        self.fails.pop(key, None)

    async def record(self, event, **kw):
        self.audit.append((event, kw.get("detail", "")))


@pytest.fixture
def stepup_app(monkeypatch, auth_app):
    stores = _Stores()
    monkeypatch.setattr("security.reauth.verify_account_password", stores.verify_account_password)
    monkeypatch.setattr("security.totp.verify_active_factor", stores.verify_active_factor)
    monkeypatch.setattr("utils.account_lockout.is_locked", stores.is_locked)
    monkeypatch.setattr("utils.account_lockout.record_failure", stores.record_failure)
    monkeypatch.setattr("utils.account_lockout.clear_failures", stores.clear_failures)
    monkeypatch.setattr("core.identity.auth_audit.record", stores.record)
    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)

    async def _principal_from_username(username, _tenant=None):
        return _principal(None, ())
    monkeypatch.setattr("api.middleware.workspace.principal_from_username", _principal_from_username)
    auth_app.stores = stores
    return auth_app


def _old_token(**claims) -> str:
    """An access token whose holder signed in an hour ago."""
    return _sign(auth_time=int(time.time()) - 3600, amr=["pwd"], ver=2, uid=str(uuid.uuid4()), tid="t-1", **claims)


def _step_up(app, token, **body):
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(2):
        return app.post("/auth/step-up", json=body, headers={"Authorization": f"Bearer {token}"})


def test_step_up_with_the_right_password_mints_a_fresh_access_token_for_the_same_session(stepup_app):
    token = _old_token()
    old = _claims(token)
    r = _step_up(stepup_app, token, password="right-password")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body
    assert body["amr"] == ["pwd"] and int(time.time()) - 5 <= body["auth_time"] <= int(time.time()) + 1
    new = _claims(body["access_token"])
    assert new["auth_time"] == body["auth_time"] and new["amr"] == ["pwd"] and new["tok"] == "access"
    # The same session, re-proven: subject, tenant selection, account and version are preserved.
    assert (new["sub"], new["tid"], new["uid"], new["ver"]) == (old["sub"], old["tid"], old["uid"], old["ver"])
    assert new["jti"] != old["jti"]
    assert ("sign_in_succeeded", "step-up: pwd") in stepup_app.stores.audit
    # The new token satisfies the check the old one failed.
    from security import stepup
    assert stepup.assess(_principal(old["auth_time"], old["amr"]), max_age=600, mfa_enrolled=False).reason
    assert stepup.assess(_principal(new["auth_time"], new["amr"]), max_age=600, mfa_enrolled=False).ok


def test_step_up_with_a_wrong_password_is_refused_counted_and_never_logged(stepup_app, caplog):
    token = _old_token()
    with caplog.at_level(logging.DEBUG):
        r = _step_up(stepup_app, token, password="wrong-password-7Qz")
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()
    assert stepup_app.stores.fails == {"alice": 1}
    assert ("sign_in_refused", "step-up refused: password") in stepup_app.stores.audit
    assert "wrong-password-7Qz" not in caplog.text and "right-password" not in caplog.text
    # Negative control: the same request with the right password passes.
    assert _step_up(stepup_app, token, password="right-password").status_code == 200


def test_step_up_counts_against_the_sign_in_lockout(stepup_app):
    token = _old_token()
    for _ in range(4):
        assert _step_up(stepup_app, token, password="nope").status_code == 401
    assert _step_up(stepup_app, token, password="nope").status_code == 429
    # Locked: even the right password is not tried.
    assert _step_up(stepup_app, token, password="right-password").status_code == 429


def test_step_up_with_a_second_factor_enrolled_needs_the_code(stepup_app, caplog):
    import pyotp

    seed = pyotp.random_base32()
    stepup_app.stores.enrolled_secret = seed
    token = _old_token()
    without = _step_up(stepup_app, token, password="right-password")
    assert without.status_code == 401 and without.json().get("second_factor") == "totp", without.text
    wrong = "000000" if not pyotp.TOTP(seed).verify("000000", valid_window=1) else "999999"
    with caplog.at_level(logging.DEBUG):
        bad = _step_up(stepup_app, token, password="right-password", totp_code=wrong)
    assert bad.status_code == 401
    assert wrong not in caplog.text
    assert stepup_app.stores.fails == {"alice": 2}
    assert ("sign_in_refused", "step-up refused: totp") in stepup_app.stores.audit
    ok = _step_up(stepup_app, token, password="right-password", totp_code=pyotp.TOTP(seed).now())
    assert ok.status_code == 200, ok.text
    assert ok.json()["amr"] == ["pwd", "otp"]
    assert _claims(ok.json()["access_token"])["amr"] == ["pwd", "otp"]
    assert stepup_app.stores.fails == {}


def test_step_up_checks_the_password_of_the_tokens_subject_not_of_anybody_else(stepup_app):
    """A token for `bob` with alice's password proves nothing: the body cannot name an account."""
    token = _sign(sub="bob", auth_time=int(time.time()) - 3600, amr=["pwd"], ver=2)
    r = _step_up(stepup_app, token, password="right-password")
    assert r.status_code == 401, r.text
    assert stepup_app.stores.fails == {"bob": 1}


def test_the_step_up_token_is_for_the_tokens_subject(stepup_app):
    """Bob's step-up, with bob's password, is a token for bob: a step-up token issued for one
    account can never be presented as another's."""
    token = _sign(sub="bob", auth_time=int(time.time()) - 3600, amr=["pwd"], ver=2, uid=str(uuid.uuid4()))
    r = _step_up(stepup_app, token, password="bobs-password")
    assert r.status_code == 200, r.text
    new = _claims(r.json()["access_token"])
    assert new["sub"] == "bob" and new["uid"] == _claims(token)["uid"]


def test_step_up_refuses_a_revoked_session(stepup_app):
    """A token version behind the account's counter (log out everywhere) never reaches the password."""
    token = _old_token()
    with patch("utils.redis_client.get_redis", return_value=_redis()), token_version_store(3):
        r = stepup_app.post("/auth/step-up", json={"password": "right-password"},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text
    assert stepup_app.stores.fails == {}


def test_step_up_refuses_a_refresh_token_and_an_unavailable_credential_store(stepup_app):
    from api.middleware.auth import _make_refresh_token

    refresh, _, _ = _make_refresh_token("alice", ver=2, auth_time=int(time.time()) - 3600, amr=["pwd"])
    assert _step_up(stepup_app, refresh, password="right-password").status_code == 401
    stepup_app.stores.password_unavailable = True
    assert _step_up(stepup_app, _old_token(), password="right-password").status_code == 503
    assert stepup_app.stores.fails == {}


def test_step_up_is_rate_limited_like_sign_in():
    """The route shares /auth/token's budget: a password check behind a stolen session must not
    be a faster oracle than the sign-in form."""
    import inspect

    import auth.routes as routes
    source = inspect.getsource(routes)
    start = source.index('@app.post("/auth/step-up"')
    decorator = source[start:source.index("async def step_up(", start)]
    assert '@limiter.limit("5/minute")' in decorator
    login = source[source.index('@app.post("/auth/token"'):source.index("async def login(")]
    assert '@limiter.limit("5/minute")' in login
