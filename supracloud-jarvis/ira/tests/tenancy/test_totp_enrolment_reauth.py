"""P0-01 — a bearer token alone cannot replace somebody's second factor.

── The defect ──────────────────────────────────────────────────────────────

`POST /auth/totp/enroll` needed nothing but a valid access token, and its
`ON CONFLICT ... DO UPDATE` overwrote the seed of an ENABLED factor while
leaving it enabled. So whoever held the owner's token — and an access token
copied out of a browser, a proxy log or a shared screen works for thirty
minutes for whoever copied it — could:

    POST /auth/totp/enroll                      (no password, no current code)
      → 201, a seed of the attacker's choosing, still `enabled`
    POST /auth/token  username+password+<owner's code>   → 401, forever

The owner's authenticator stopped working and the attacker's started: the
second factor, the one control meant to survive a leaked credential, became
the attacker's. Even an honest re-enrolment was unsafe: the new seed was live
before anybody had scanned it, so an abandoned QR code locked the account.

── What these tests drive ──────────────────────────────────────────────────

The real `/auth/token`, `/auth/totp/enroll` and `/auth/totp/verify` routes over
HTTP, against a migrated PostgreSQL as `ira_app` (forced RLS on `totp_secrets`),
with sealed seeds under a real MFA key hierarchy, real bcrypt against the real
owner credential and a real household account, and real access tokens checked
by the real `require_auth`. The account lockout runs for real against Redis at
`IRA_TEST_REDIS_URL` when it is set, otherwise against an in-memory stand-in
with the same commands; either way `utils/account_lockout.py` itself is not
replaced.

Replaced: nothing on the authentication path. `dev_mode_active` is forced off,
so the run means the same whether or not the shell has `DEV_MODE=true`.
"""
from __future__ import annotations

import base64 as _b64
import os
import secrets as _stdlib_secrets
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
httpx = pytest.importorskip("httpx")
pyotp = pytest.importorskip("pyotp")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

#: The KEK the MFA keys are wrapped under. Generated per process, never written
#: down — a 32-byte literal in a test file is what Gitleaks blocks. `user_mfa_keys`
#: rows for this file's accounts are deleted on both sides of every test, so a
#: key wrapped by an earlier process cannot be met here.
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY",
                      _b64.b64encode(_stdlib_secrets.token_bytes(32)).decode())
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-p001")
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from config import get_settings  # noqa: E402
from core.identity.principal import PRIVATE_TENANT_ID, private_user_id  # noqa: E402

HOUSEHOLD = "p001-household"
HOUSEHOLD_PASSWORD = "household-own-secret-1"


def _owner() -> tuple[str, str]:
    s = get_settings()
    return s.ira_admin_username, s.ira_admin_password


OWNER_ID = private_user_id(_owner()[0])
HOUSEHOLD_ID = private_user_id(HOUSEHOLD)


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


async def _purge(admin) -> None:
    ids = [OWNER_ID, HOUSEHOLD_ID]
    await admin.execute("DELETE FROM totp_secrets WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM user_mfa_keys WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM delegated_users WHERE username = $1", HOUSEHOLD)
    # 096: a token-version counter blocks deleting its user (ON DELETE RESTRICT).
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id = $1", HOUSEHOLD_ID)
    await admin.execute("DELETE FROM users WHERE id = $1", HOUSEHOLD_ID)
    await admin.execute("DELETE FROM access_audit_log WHERE target = $1", HOUSEHOLD)


@pytest.fixture
async def admin():
    """A superuser connection for seeding and inspection. Clean either side."""
    conn = await asyncpg.connect(ADMIN_DSN)
    await _purge(conn)
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


class _DictRedis:
    """The commands `utils/account_lockout.py` and the token checks use."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, *_a, **_k):
        self.data[key] = str(value)
        return True

    async def setex(self, key, _ttl, value):
        self.data[key] = str(value)
        return True

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    async def expire(self, key, _ttl):
        return key in self.data

    async def exists(self, *keys):
        return sum(k in self.data for k in keys)

    async def delete(self, *keys):
        return sum(self.data.pop(k, None) is not None for k in keys)


def _lock_keys() -> list[str]:
    names = [_owner()[0].strip().lower(), HOUSEHOLD]
    return ([f"ira:login_fails:{n}" for n in names]
            + [f"ira:login_lock:{n}" for n in names])


@pytest.fixture
async def lockout(monkeypatch):
    """The lockout's Redis: the disposable one when configured, else a dict."""
    from utils import redis_client

    url = os.getenv("IRA_TEST_REDIS_URL", "")
    if url:
        import redis.asyncio as redis

        store = redis.from_url(url, decode_responses=True,
                               socket_connect_timeout=2, socket_timeout=2)
        assert await store.ping()
    else:
        store = _DictRedis()
    monkeypatch.setattr(redis_client, "_redis", store)
    await store.delete(*_lock_keys())
    try:
        yield store
    finally:
        await store.delete(*_lock_keys())
        if url:
            await store.aclose()


@pytest.fixture
async def client(monkeypatch, pool, admin, lockout):
    """The real sign-in and TOTP routes, on the test's event loop."""
    from fastapi import FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from api.routes.totp import router as totp_router
    from auth.routes import register_auth_routes
    from core.identity.provisioning import ensure_private_owner_identity

    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)
    await ensure_private_owner_identity()

    app = FastAPI()
    limiter = Limiter(key_func=lambda _r: "p001", enabled=False)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    app.include_router(totp_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://p001") as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _login(client, username=None, password=None, code=None):
    owner, owner_password = _owner()
    data = {"username": username or owner, "password": password or owner_password}
    if code is not None:
        data["totp_code"] = code
    return await client.post("/auth/token", data=data)


async def _bearer(client, username=None, password=None, code=None) -> dict:
    r = await _login(client, username, password, code)
    assert r.status_code == 200, f"sign-in failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _enrol(client, headers, *, password=None, code=None, body=None):
    if body is None:
        body = {}
        if password is not None:
            body["password"] = password
        if code is not None:
            body["code"] = code
    return await client.post("/auth/totp/enroll", json=body, headers=headers)


async def _verify(client, headers, code, token=None):
    body = {"code": code}
    if token is not None:
        body["enrolment_token"] = token
    return await client.post("/auth/totp/verify", json=body, headers=headers)


def _now(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def _wrong(secret: str) -> str:
    """A six-digit code that is NOT valid for `secret` in the accepted window."""
    totp = pyotp.TOTP(secret)
    n = 0
    while True:
        candidate = f"{n:06d}"
        if not totp.verify(candidate, valid_window=1):
            return candidate
        n += 1


async def _row(admin, user_id=OWNER_ID):
    return await admin.fetchrow(
        "SELECT id, secret_ciphertext, enabled, enrolled_at FROM totp_secrets "
        "WHERE user_id=$1 AND tenant_id=$2 AND revoked_at IS NULL",
        user_id, PRIVATE_TENANT_ID)


async def _fail_count(store, name=None) -> int:
    value = await store.get(f"ira:login_fails:{(name or _owner()[0]).strip().lower()}")
    return int(value) if value else 0


async def _enabled_factor(client) -> tuple[str, dict]:
    """The owner enrols and confirms a factor the honest way. Returns its seed
    and a bearer header for a session signed in WITH it."""
    _, password = _owner()
    headers = await _bearer(client)
    r = await _enrol(client, headers, password=password)
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    v = await _verify(client, headers, _now(secret))
    assert v.status_code == 200, v.text
    return secret, await _bearer(client, code=_now(secret))


# ─────────────────────────────────────────────────────────────────────────────
# The defect
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_bearer_token_alone_cannot_replace_an_enabled_factor(client, admin):
    """**The regression.** On `main` at 6237697 this returned 201 and the
    owner's own codes stopped signing in.

    The token is a real one, issued by `/auth/token` for a session that
    presented the owner's code — exactly what a copied token is.
    """
    secret, stolen = await _enabled_factor(client)
    before = await _row(admin)

    r = await _enrol(client, stolen, body={})
    assert r.status_code in (401, 422), (
        f"/auth/totp/enroll accepted a request with no password over an enabled "
        f"factor (HTTP {r.status_code}). A copied access token can take over "
        f"the second factor.")
    assert "secret" not in r.json()

    after = await _row(admin)
    assert after["secret_ciphertext"] == before["secret_ciphertext"], (
        "the stored seed changed")
    assert after["enabled"] is True
    signed_in = await _login(client, code=_now(secret))
    assert signed_in.status_code == 200, (
        "the owner's own authenticator no longer signs in")


async def test_a_stolen_token_with_a_guessed_password_is_refused_and_counted(
        client, admin, lockout):
    _, stolen = await _enabled_factor(client)
    before = await _row(admin)

    r = await _enrol(client, stolen, password="not-the-owner-password")
    assert r.status_code == 401
    assert r.json()["reauth"] == "password"
    assert await _fail_count(lockout) == 1, "a wrong password was not counted"
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


async def test_the_password_without_a_current_code_is_refused(client, admin, lockout):
    """The password is what the second factor exists to be MORE than. With a
    factor on, knowing the password must not be enough to replace it."""
    _, password = _owner()
    _, headers = await _enabled_factor(client)
    before = await _row(admin)

    r = await _enrol(client, headers, password=password)
    assert r.status_code == 401, r.text
    assert r.json()["reauth"] == "totp"
    assert "secret" not in r.json()
    assert await _fail_count(lockout) == 1
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


async def test_the_password_with_a_wrong_current_code_is_refused(client, admin, lockout):
    _, password = _owner()
    secret, headers = await _enabled_factor(client)
    before = await _row(admin)

    r = await _enrol(client, headers, password=password, code=_wrong(secret))
    assert r.status_code == 401, r.text
    assert r.json()["reauth"] == "totp"
    assert await _fail_count(lockout) == 1
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


# ─────────────────────────────────────────────────────────────────────────────
# A replacement is pending until a code from it works
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_valid_re_enrolment_leaves_the_old_factor_in_place(client, admin):
    """Password and current code: a new seed comes back, and NOTHING is
    written. The old authenticator still signs in; the new one does not yet."""
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)

    r = await _enrol(client, headers, password=password, code=_now(old))
    assert r.status_code == 201, r.text
    body = r.json()
    new = body["secret"]
    assert body["replaces_active_factor"] is True
    assert body["enrolment_token"] and body["expires_in"] > 0
    assert new not in body["enrolment_token"], "the token carries the plaintext seed"

    after = await _row(admin)
    assert after["secret_ciphertext"] == before["secret_ciphertext"]
    assert after["enrolled_at"] == before["enrolled_at"]
    assert after["enabled"] is True
    assert (await _login(client, code=_now(old))).status_code == 200
    assert (await _login(client, code=_now(new))).status_code == 401


async def test_confirming_with_a_code_from_the_new_authenticator_replaces_it(
        client, admin):
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    r = await _verify(client, headers, _now(started["secret"]), started["enrolment_token"])
    assert r.status_code == 200, r.text
    assert r.json() == {"verified": True, "enabled": True, "replaced": True}

    after = await _row(admin)
    assert after["id"] == before["id"], "the replacement made a second enrolment"
    assert after["secret_ciphertext"] != before["secret_ciphertext"]
    assert after["enabled"] is True
    assert (await _login(client, code=_now(started["secret"]))).status_code == 200
    assert (await _login(client, code=_now(old))).status_code == 401, (
        "the replaced authenticator still signs in")


async def test_confirming_with_a_wrong_code_changes_nothing(client, admin, lockout):
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    r = await _verify(client, headers, _wrong(started["secret"]), started["enrolment_token"])
    assert r.status_code == 401
    assert await _fail_count(lockout) == 1, "a wrong confirmation code was not counted"
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]
    assert (await _login(client, code=_now(old))).status_code == 200


async def test_confirming_with_the_old_authenticators_code_changes_nothing(
        client, admin):
    """The code must come from the NEW seed. A current code from the old one
    proves nothing about whether the new one was set up."""
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    r = await _verify(client, headers, _now(old), started["enrolment_token"])
    assert r.status_code == 401
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


@pytest.mark.parametrize("mangle", [
    pytest.param(lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"),
                 id="tampered-signature"),
    pytest.param(lambda t: "not-a-token", id="garbage"),
])
async def test_a_forged_enrolment_token_is_refused(client, admin, mangle):
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    r = await _verify(client, headers, _now(started["secret"]),
                      mangle(started["enrolment_token"]))
    assert r.status_code == 400, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


async def test_an_expired_enrolment_token_is_refused(client, admin, monkeypatch):
    import security.totp_enrolment as enrolment

    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    monkeypatch.setattr(enrolment, "REENROLMENT_TTL_SECONDS", -1)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    r = await _verify(client, headers, _now(started["secret"]), started["enrolment_token"])
    assert r.status_code == 400, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


async def test_an_older_replacement_cannot_be_confirmed_after_a_newer_one(client, admin):
    """Compare-and-swap. A token names the factor it may replace; once that
    factor has been replaced, the token is useless rather than a way back to an
    older seed."""
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    first = (await _enrol(client, headers, password=password, code=_now(old))).json()
    second = (await _enrol(client, headers, password=password, code=_now(old))).json()

    done = await _verify(client, headers, _now(second["secret"]), second["enrolment_token"])
    assert done.status_code == 200, done.text
    replaced = await _row(admin)

    stale = await _verify(client, headers, _now(first["secret"]), first["enrolment_token"])
    assert stale.status_code == 409, stale.text
    assert (await _row(admin))["secret_ciphertext"] == replaced["secret_ciphertext"]
    assert (await _login(client, code=_now(second["secret"]))).status_code == 200


@pytest.fixture
async def owner_version_restored(admin):
    """Put the owner's token-version counter back as it was.

    "Log out everywhere" raises it in PostgreSQL, where it outlives this test,
    and other suites in the same database mint owner tokens at version 0. Left
    raised, every one of those is refused as "All tokens invalidated" — which
    is how `test_evidence_full_middleware.py` failed in a pre-push run that
    reused a database this file had already run against.
    """
    before = await admin.fetchval(
        "SELECT version FROM auth_token_versions WHERE user_id = $1", OWNER_ID)
    try:
        yield
    finally:
        # Delete and re-insert: the counter's trigger refuses any UPDATE that
        # lowers it (`auth_token_versions_monotonic`), which is the point of it.
        await admin.execute(
            "DELETE FROM auth_token_versions WHERE user_id = $1", OWNER_ID)
        if before is not None:
            await admin.execute(
                "INSERT INTO auth_token_versions (user_id, version) VALUES ($1, $2)",
                OWNER_ID, before)


async def test_log_out_everywhere_cancels_a_pending_replacement(
        client, admin, owner_version_restored):
    """The token carries the account's token version, as every IRA token does.
    "Log out everywhere" after starting a replacement leaves it unconfirmable,
    even from a fresh session that signs in afterwards."""
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    out = await client.post("/auth/logout/all", headers=headers)
    assert out.status_code == 200, out.text
    fresh = await _bearer(client, code=_now(old))

    r = await _verify(client, fresh, _now(started["secret"]), started["enrolment_token"])
    assert r.status_code == 400, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


async def test_an_enrolment_token_is_not_an_access_token(client):
    """Signed under a derived key with its own audience: it cannot be presented
    as a bearer credential, and an access token cannot stand in for it."""
    _, password = _owner()
    old, headers = await _enabled_factor(client)
    started = (await _enrol(client, headers, password=password, code=_now(old))).json()

    # The signing keys differ, which is the property the two checks below rest
    # on: the access-token key does not verify a re-enrolment token at all.
    import jwt

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(started["enrolment_token"], get_settings().ira_secret_key,
                   algorithms=["HS256"], options={"verify_aud": False})

    as_bearer = {"Authorization": f"Bearer {started['enrolment_token']}"}
    assert (await client.get("/auth/totp/status", headers=as_bearer)).status_code == 401

    access = headers["Authorization"].split(" ", 1)[1]
    r = await _verify(client, headers, _now(started["secret"]), access)
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# First enrolment
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_first_enrolment_without_a_password_is_refused(client, admin):
    """No factor yet, and the token alone still cannot enrol one: a factor the
    attacker enrolled would lock the owner out just the same once confirmed."""
    headers = await _bearer(client)
    r = await _enrol(client, headers, body={})
    assert r.status_code == 422, r.text
    assert await _row(admin) is None


async def test_a_first_enrolment_with_a_wrong_password_is_refused(client, admin, lockout):
    headers = await _bearer(client)
    r = await _enrol(client, headers, password="not-the-owner-password")
    assert r.status_code == 401
    assert r.json()["reauth"] == "password"
    assert await _fail_count(lockout) == 1
    assert await _row(admin) is None


async def test_a_first_enrolment_stays_off_until_verified(client, admin):
    """The pre-existing contract, kept: enrolling stores the seed, not enabled,
    and sign-in asks for nothing until `/verify` turns it on."""
    _, password = _owner()
    headers = await _bearer(client)
    r = await _enrol(client, headers, password=password)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["replaces_active_factor"] is False
    assert body["enrolment_token"] is None
    row = await _row(admin)
    assert row is not None and row["enabled"] is False
    assert (await _login(client)).status_code == 200

    v = await _verify(client, headers, _now(body["secret"]))
    assert v.status_code == 200, v.text
    assert (await _row(admin))["enabled"] is True


async def test_an_unconfirmed_enrolment_can_be_restarted_with_the_password(client, admin):
    """Lost the QR code before confirming: nothing is on, so the password is
    enough, and the restart replaces the unconfirmed seed rather than adding a
    second row."""
    _, password = _owner()
    headers = await _bearer(client)
    first = (await _enrol(client, headers, password=password)).json()
    second = await _enrol(client, headers, password=password)
    assert second.status_code == 201, second.text
    assert second.json()["secret"] != first["secret"]
    assert await admin.fetchval(
        "SELECT count(*) FROM totp_secrets WHERE user_id=$1", OWNER_ID) == 1

    assert (await _verify(client, headers, _now(first["secret"]))).status_code == 401
    assert (await _verify(client, headers, _now(second.json()["secret"]))).status_code == 200


async def test_verify_with_a_wrong_code_is_counted(client, admin, lockout):
    _, password = _owner()
    headers = await _bearer(client)
    secret = (await _enrol(client, headers, password=password)).json()["secret"]
    r = await _verify(client, headers, _wrong(secret))
    assert r.status_code == 401
    assert await _fail_count(lockout) == 1
    assert (await _row(admin))["enabled"] is False


async def test_the_first_enrolment_statement_never_overwrites_an_enabled_factor(
        client, admin, monkeypatch):
    """Belt and braces under the re-authentication: the INSERT's own
    `DO UPDATE ... WHERE enabled = FALSE`. Here the route is told no factor is
    on (as it would be if one were turned on between the check and the write)
    while one IS on; the statement must refuse rather than replace it."""
    from security import totp as totp_mod

    _, password = _owner()
    secret, headers = await _enabled_factor(client)
    before = await _row(admin)

    async def _nothing_on(*_a, **_k):
        return totp_mod.NOT_ENROLLED, None

    real = totp_mod.verify_active_factor
    monkeypatch.setattr(totp_mod, "verify_active_factor", _nothing_on)
    r = await _enrol(client, headers, password=password)
    monkeypatch.setattr(totp_mod, "verify_active_factor", real)
    assert r.status_code == 409, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]
    assert (await _login(client, code=_now(secret))).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Lockout and the unknown state
# ─────────────────────────────────────────────────────────────────────────────


async def test_failed_re_authentication_locks_sign_in_too(client, admin, lockout):
    """One budget, shared with `/auth/token`. Five wrong passwords at `/enroll`
    lock the account: the sixth attempt is refused even with the right password,
    and so is sign-in."""
    from utils.account_lockout import MAX_FAILURES

    _, password = _owner()
    headers = await _bearer(client)
    statuses = [(await _enrol(client, headers, password=f"wrong-{i}")).status_code
                for i in range(MAX_FAILURES)]
    assert statuses[:-1] == [401] * (MAX_FAILURES - 1)
    assert statuses[-1] == 429, statuses

    assert (await _enrol(client, headers, password=password)).status_code == 429
    assert (await _login(client)).status_code == 429
    assert await _row(admin) is None


async def test_successful_re_authentication_clears_the_counter(client, lockout):
    _, password = _owner()
    headers = await _bearer(client)
    assert (await _enrol(client, headers, password="wrong")).status_code == 401
    assert await _fail_count(lockout) == 1
    assert (await _enrol(client, headers, password=password)).status_code == 201
    assert await _fail_count(lockout) == 0


async def test_an_undeterminable_factor_state_refuses_and_changes_nothing(
        client, admin, monkeypatch):
    from security import totp as totp_mod

    _, password = _owner()
    _, headers = await _enabled_factor(client)
    before = await _row(admin)

    async def _boom(*_a, **_k):
        raise totp_mod.TotpUnavailable("store unreachable")

    monkeypatch.setattr(totp_mod, "verify_active_factor", _boom)
    r = await _enrol(client, headers, password=password)
    assert r.status_code == 503, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


# ─────────────────────────────────────────────────────────────────────────────
# Sign-in tells a client a code is wanted
# ─────────────────────────────────────────────────────────────────────────────


async def test_sign_in_says_when_a_second_factor_is_required(client):
    """The signal the web sign-in keys its code step off — a field, not prose."""
    secret, _ = await _enabled_factor(client)

    missing = await _login(client)
    assert missing.status_code == 401
    assert missing.json()["second_factor"] == "totp"

    wrong = await _login(client, code=_wrong(secret))
    assert wrong.status_code == 401
    assert wrong.json()["second_factor"] == "totp"

    assert (await _login(client, code=_now(secret))).status_code == 200


async def test_a_wrong_password_does_not_say_anything_about_a_second_factor(client):
    await _enabled_factor(client)
    owner, _ = _owner()
    r = await _login(client, username=owner, password="not-the-owner-password")
    assert r.status_code == 401
    assert "second_factor" not in r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Other accounts: each re-authenticates with its own password
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_household_account_re_authenticates_with_its_own_password(client, admin):
    from security.delegated import create_delegated_user

    await create_delegated_user(username=HOUSEHOLD, password=HOUSEHOLD_PASSWORD,
                                role="trusted_user")
    headers = await _bearer(client, HOUSEHOLD, HOUSEHOLD_PASSWORD)

    _, owner_password = _owner()
    with_owners = await _enrol(client, headers, password=owner_password)
    assert with_owners.status_code == 401, (
        "a household account re-authenticated with the OWNER's password")
    assert await _row(admin, HOUSEHOLD_ID) is None

    own = await _enrol(client, headers, password=HOUSEHOLD_PASSWORD)
    assert own.status_code == 201, own.text
    assert (await _row(admin, HOUSEHOLD_ID))["enabled"] is False


async def test_another_accounts_enrolment_token_is_refused(client, admin):
    """A token names its subject, user and tenant. The household account cannot
    confirm the owner's replacement, even with a valid code for it."""
    from security.delegated import create_delegated_user

    _, password = _owner()
    old, owner_headers = await _enabled_factor(client)
    before = await _row(admin)
    started = (await _enrol(client, owner_headers, password=password,
                            code=_now(old))).json()

    await create_delegated_user(username=HOUSEHOLD, password=HOUSEHOLD_PASSWORD,
                                role="trusted_user")
    household = await _bearer(client, HOUSEHOLD, HOUSEHOLD_PASSWORD)
    r = await _verify(client, household, _now(started["secret"]),
                      started["enrolment_token"])
    assert r.status_code == 400, r.text
    assert (await _row(admin))["secret_ciphertext"] == before["secret_ciphertext"]


# ─────────────────────────────────────────────────────────────────────────────
# A cloud account re-authenticates against its own credential
# ─────────────────────────────────────────────────────────────────────────────

CLOUD_TENANT = uuid.UUID("11111111-1111-1111-1111-1111111100a1")
CLOUD_USER = uuid.UUID("aaaa0000-0000-0000-0000-0000000000a1")
CLOUD_OTHER = uuid.UUID("aaaa0000-0000-0000-0000-0000000000a2")
CLOUD_DOMAIN = "p001-reauth.example"
CLOUD_EMAIL = f"member@{CLOUD_DOMAIN}"
CLOUD_PASSWORD = "cloud-member-own-passphrase"
CLOUD_OTHER_PASSWORD = "cloud-other-member-passphrase"


async def _wipe_cloud(admin) -> None:
    ids = [CLOUD_USER, CLOUD_OTHER]
    await admin.execute("DELETE FROM totp_secrets WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM user_mfa_keys WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM tenant_memberships WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM user_credentials WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[]) "
                        "OR normalised_email LIKE $2", ids, f"%@{CLOUD_DOMAIN}")
    await admin.execute("DELETE FROM tenants WHERE id = $1", CLOUD_TENANT)


@pytest.fixture
async def cloud_member(monkeypatch, pool, admin, lockout):
    """One verified cloud member with a password, and a second member of the
    same tenant with a different one."""
    from api.middleware.auth import hash_password
    from core.editions import EDITION_CLOUD

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)
    monkeypatch.setattr("core.identity.resolver.current_edition", lambda: EDITION_CLOUD)
    await _wipe_cloud(admin)
    try:
        await admin.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,'P001','p001-reauth')",
            CLOUD_TENANT)
        for uid, address, password in (
                (CLOUD_USER, CLOUD_EMAIL, CLOUD_PASSWORD),
                (CLOUD_OTHER, f"other@{CLOUD_DOMAIN}", CLOUD_OTHER_PASSWORD)):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status,"
                " email_verified_at) VALUES ($1,$2,'T','password','active',NOW())",
                uid, address)
            await admin.execute(
                "INSERT INTO user_credentials (user_id,password_hash) VALUES ($1,$2)",
                uid, hash_password(password))
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status,joined_at)"
                " VALUES ($1,$2,'member','active',NOW())", CLOUD_TENANT, uid)
        subject = await admin.fetchval(
            "SELECT normalised_email FROM users WHERE id=$1", CLOUD_USER)
        yield subject
    finally:
        await _wipe_cloud(admin)


async def test_a_cloud_member_re_authenticates_with_their_own_password(cloud_member, admin):
    """Driven through the route function, as `test_p4_mfa_is_tenant_scoped.py`
    drives it: a cloud bearer token needs a recorded session that this file has
    no reason to build. Principal resolution, the credential lookup, bcrypt and
    the write are all real."""
    from api.routes import totp as totp_routes

    wrong = await totp_routes.totp_enroll(
        totp_routes.EnrollRequest(password=CLOUD_OTHER_PASSWORD), _user=cloud_member)
    assert wrong.status_code == 401, (
        "a cloud member re-authenticated with ANOTHER member's password")
    assert await admin.fetchval(
        "SELECT count(*) FROM totp_secrets WHERE user_id=$1", CLOUD_USER) == 0

    ok = await totp_routes.totp_enroll(
        totp_routes.EnrollRequest(password=CLOUD_PASSWORD), _user=cloud_member)
    assert isinstance(ok, totp_routes.EnrollResponse), ok
    assert await admin.fetchval(
        "SELECT tenant_id FROM totp_secrets WHERE user_id=$1", CLOUD_USER) == CLOUD_TENANT


async def test_an_unreadable_cloud_credential_store_refuses_with_503(
        cloud_member, admin, monkeypatch):
    from api.routes import totp as totp_routes

    async def _down(_email):
        raise ConnectionError("credential store unreachable")

    monkeypatch.setattr("core.identity.cloud_login._locate_credential", _down)
    r = await totp_routes.totp_enroll(
        totp_routes.EnrollRequest(password=CLOUD_PASSWORD), _user=cloud_member)
    assert r.status_code == 503
    assert await admin.fetchval(
        "SELECT count(*) FROM totp_secrets WHERE user_id=$1", CLOUD_USER) == 0
