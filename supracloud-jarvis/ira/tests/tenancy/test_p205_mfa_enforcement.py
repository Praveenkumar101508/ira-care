"""P2-05 — the second factor is actually demanded, end to end.

The regression this file exists for is not subtle once you see it, and was
invisible until someone ran the query as the application role:

    async with acquire() as conn:                     # binds no GUCs
        row = await conn.fetchrow(
            "SELECT secret FROM totp_secrets WHERE username=$1 AND enabled=TRUE", u)
    if row:                                           # always None
        ...demand a code...

`totp_secrets` has had FORCE ROW LEVEL SECURITY since 036 with a policy on
`app.user_id`. An unbound connection leaves the GUC NULL, `user_id = NULL` is
NULL rather than true, and the statement matches **zero rows** — with no error,
no warning and no log line. So the branch never ran and every enrolled user
signed in on a password alone.

These tests drive the real `/auth/token` route against a real PostgreSQL, so
they fail if the login path is ever changed back to an unbound connection. That
is the point: the unit-level guards check shape, and this one checks outcome.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("fastapi")
pyotp = pytest.importorskip("pyotp")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from core.identity.principal import PRIVATE_TENANT_ID, private_user_id  # noqa: E402

USERNAME = "admin"
PASSWORD = "p205-correct-horse"
SECRET = pyotp.random_base32()

#: The owner's identity as `resolve_principal` derives it in the private
#: edition. The enrolment row must carry exactly these, or RLS hides it from the
#: very connection the login path opens — which is the whole finding.
OWNER_USER_ID = private_user_id(USERNAME)
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-2222222222b5")
OTHER_USER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000b5b1")


@pytest.fixture
async def enrolled():
    """`admin` holds an ENABLED TOTP enrolment, and a second tenant holds one too."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,'Other','p205-mfa-other')"
            " ON CONFLICT DO NOTHING", OTHER_TENANT)
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status)"
            " VALUES ($1,'Owner','local','active'),($2,'Other','local','active')"
            " ON CONFLICT DO NOTHING", OWNER_USER_ID, OTHER_USER)
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", USERNAME)
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id)"
            " VALUES ($1,$2,TRUE,$3,$4)",
            USERNAME, SECRET, OWNER_USER_ID, PRIVATE_TENANT_ID)
        # Tenant B enrols the SAME username with a different seed. Nothing tenant
        # B does may change what tenant A's login demands.
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id)"
            " VALUES ($1,'OTHERTENANTSEED',TRUE,$2,$3)",
            USERNAME, OTHER_USER, OTHER_TENANT)
        yield
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", USERNAME)
        await admin.close()


@pytest.fixture
async def pool():
    """`utils.db._pool` pointed at the test database, restored afterwards.

    The same injection `test_key_management_rls.py` uses. Without it
    `tenant_connection` raises RuntimeError, the login path reports the
    enrolment state as undeterminable, and every test here 503s — which is the
    new fail-closed behaviour working correctly on a broken harness, and not
    what these tests are trying to measure.
    """
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
def client(monkeypatch, pool):
    """The real `/auth/token` route.

    Only Redis-backed lockout and the trust-console write are stubbed — they are
    not what these tests are about, and both are already fail-soft. Password
    verification, principal resolution, the database and RLS are all real.
    """
    import auth.routes as routes_mod
    from fastapi import FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from auth.routes import register_auth_routes

    # Patched on `auth.routes`, not on `api.middleware.auth`: the route module
    # does `from api.middleware.auth import authenticate_user` at import time,
    # so the name it calls is its own. Patching the source module would leave
    # the real bcrypt check in place and every login would 401 — which is what
    # the first run of this file did.
    monkeypatch.setattr(routes_mod, "authenticate_user",
                        lambda u, p: u == USERNAME and p == PASSWORD)
    # The delegated fallback only runs when the owner check fails; stub it so a
    # failed login is a clean 401 rather than a database round trip.
    monkeypatch.setattr("security.delegated.authenticate_delegated",
                        _async_return(False), raising=False)
    monkeypatch.setattr("utils.account_lockout.is_locked",
                        _async_return(False), raising=False)
    monkeypatch.setattr("utils.account_lockout.record_failure",
                        _async_return((1, False)), raising=False)
    monkeypatch.setattr("utils.account_lockout.clear_failures",
                        _async_return(None), raising=False)
    monkeypatch.setattr("utils.canary.check_canary_username",
                        _async_return(False), raising=False)

    limiter = Limiter(key_func=lambda _r: "p205", enabled=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    # httpx + ASGITransport rather than TestClient: TestClient drives the app on
    # its own event loop, and an asyncpg pool created on the test's loop cannot
    # be used from another one — every request failed with
    # ConnectionDoesNotExistError, which the new fail-closed login correctly
    # reported as 503. Same loop for the pool and the app, and it works.
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://p205")


def _async_return(value):
    async def _f(*_a, **_k):
        return value
    return _f


async def _login(client, code=None):
    data = {"username": USERNAME, "password": PASSWORD}
    if code is not None:
        data["totp_code"] = code
    return await client.post("/auth/token", data=data)


# ─────────────────────────────────────────────────────────────────────────────
# The enforcement itself
# ─────────────────────────────────────────────────────────────────────────────


async def test_login_without_the_second_factor_is_refused(client, enrolled):
    """**The regression.** This returned 200 with a full token pair.

    If this ever passes with a 200 again, the login path has stopped seeing the
    enrolment — almost certainly because it went back to an unbound connection.
    """
    r = await _login(client)
    assert r.status_code == 401, (
        f"an enrolled user signed in with no second factor (HTTP "
        f"{r.status_code}). The enrolment exists; the login path cannot see it.")
    assert "totp" in r.text.lower()


async def test_login_with_a_wrong_code_is_refused(client, enrolled):
    r = await _login(client, code="000000")
    assert r.status_code == 401


async def test_login_with_the_correct_code_succeeds(client, enrolled):
    """Positive control. Without it, a login path that refused everything would
    satisfy both assertions above."""
    r = await _login(client, code=pyotp.TOTP(SECRET).now())
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


async def test_the_enrolment_is_genuinely_invisible_without_bound_context(enrolled):
    """Why the bug was silent, asserted directly.

    The row exists — the fixture wrote it. An unbound connection sees nothing
    and raises nothing. Any code that treats "no row" as "not enrolled" is
    therefore fail-open, which is what the login path was.
    """
    async def _check():
        app = await asyncpg.connect(DSN)
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            visible_unbound = await app.fetchval(
                "SELECT count(*) FROM totp_secrets WHERE username=$1", USERNAME)
            really_there = await admin.fetchval(
                "SELECT count(*) FROM totp_secrets WHERE username=$1", USERNAME)
            return visible_unbound, really_there
        finally:
            await app.close()
            await admin.close()

    unbound, actual = await _check()
    assert actual == 2, "fixture did not seed the enrolments"
    assert unbound == 0, (
        "an unbound connection could see the enrolment, so this test would no "
        "longer detect the bug it exists for")


async def test_tenant_b_cannot_change_what_tenant_a_must_present(client, enrolled):
    """Both tenants hold an enrolment for the username `admin`, with different
    seeds. Tenant B's seed must never satisfy tenant A's login."""
    other_code = pyotp.TOTP("OTHERTENANTSEED").now()
    r = await _login(client, code=other_code)
    assert r.status_code == 401, (
        "another tenant's TOTP seed satisfied this tenant's login")

    # …and the correct one still works, so the refusal above is about the seed
    # rather than about everything being broken.
    assert (await _login(client, code=pyotp.TOTP(SECRET).now())).status_code == 200


async def test_deleting_tenant_bs_enrolment_does_not_affect_tenant_a(client, enrolled):
    async def _drop_other():
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                "DELETE FROM totp_secrets WHERE tenant_id=$1", OTHER_TENANT)
        finally:
            await admin.close()

    await _drop_other()
    assert (await _login(client)).status_code == 401
    assert (await _login(client, code=pyotp.TOTP(SECRET).now())).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# The unknown-state direction
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_undeterminable_enrolment_state_refuses_the_login(client, monkeypatch):
    """Not "sign in without the factor". The old code had no way to express
    this, because a failed lookup and an absent enrolment were the same value."""
    from security import totp as totp_mod

    async def _boom(_u, _code):
        raise totp_mod.TotpUnavailable("store unreachable")

    monkeypatch.setattr(totp_mod, "check_login_second_factor", _boom)
    r = await _login(client, code="123456")
    assert r.status_code == 503, (
        f"login returned {r.status_code} when the MFA store was unreachable; "
        f"it must refuse rather than proceed without the factor")


async def test_a_user_with_no_enrolment_still_signs_in(client):
    """The control that keeps the fix from being "demand a code from everybody".

    No fixture here, so `admin` has no enrolment.
    """
    r = await _login(client)
    assert r.status_code == 200, r.text


# ─────────────────────────────────────────────────────────────────────────────
# The encrypted enrolment, driven through the real login route (P2-05)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def enrolled_encrypted(pool):
    """`admin` holds an ENCRYPTED enrolment — no plaintext column at all.

    The fixtures above store the seed in `secret`, which is what the table
    looked like before this migration and is still a supported shape while rows
    migrate. This one is the shape every row ends up in, and nothing was
    asserting that a login can be completed from it: an encryption change that
    broke verification would have left every test above green, because they all
    read a column the production write path no longer populates.
    """
    import base64
    import os as _os

    _os.environ.setdefault(
        "IRA_CONNECTOR_ENCRYPTION_KEY",
        base64.b64encode(b"p205-mfa-enforcement-kek-32byte!").decode())

    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    from core.identity.db_context import tenant_connection
    from security.mfa_crypto import seal_totp_secret

    principal = Principal(
        user_id=OWNER_USER_ID, tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(), edition=EDITION_CLOUD)

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status) "
            "VALUES ($1,'Owner','local','active') ON CONFLICT DO NOTHING",
            OWNER_USER_ID)
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", USERNAME)
        await admin.execute(
            "DELETE FROM user_mfa_keys WHERE user_id=$1", OWNER_USER_ID)

        ciphertext, nonce, version = await seal_totp_secret(principal, SECRET)
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,secret_ciphertext,"
            " secret_nonce,encryption_key_version,encrypted_at,migration_status,"
            " enabled,user_id,tenant_id) "
            "VALUES ($1,NULL,$2,$3,$4,NOW(),'ENCRYPTED',TRUE,$5,$6)",
            USERNAME, ciphertext, nonce, version,
            OWNER_USER_ID, PRIVATE_TENANT_ID)
        yield admin
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", USERNAME)
        await admin.execute(
            "DELETE FROM user_mfa_keys WHERE user_id=$1", OWNER_USER_ID)
        await admin.close()


async def test_an_encrypted_enrolment_still_demands_a_code(client, enrolled_encrypted):
    """The original P2-05 finding, re-asked of the encrypted row. Encryption
    must not reintroduce "no row found, so no second factor"."""
    r = await _login(client)
    assert r.status_code == 401, (
        f"login returned {r.status_code} with no TOTP code against an "
        f"ENCRYPTED enrolment; the second factor was not demanded")


async def test_a_valid_code_signs_in_against_an_encrypted_seed(client, enrolled_encrypted):
    """The positive control, and the one that proves the seed is genuinely
    recoverable inside the authentication path — not merely that verification
    fails consistently."""
    r = await _login(client, code=pyotp.TOTP(SECRET).now())
    assert r.status_code == 200, r.text


async def test_a_wrong_code_is_refused_against_an_encrypted_seed(client, enrolled_encrypted):
    r = await _login(client, code="000000")
    assert r.status_code == 401


async def test_a_login_response_never_carries_the_seed(client, enrolled_encrypted):
    """"Secrets must never appear in ... API responses." Checked on the success
    path, because that is the response nobody inspects."""
    r = await _login(client, code=pyotp.TOTP(SECRET).now())
    assert r.status_code == 200
    assert SECRET not in r.text


async def test_a_ciphertext_with_no_nonce_refuses_the_login(client, enrolled_encrypted):
    """AES-GCM needs a nonce and it is not derivable, so a row missing one
    cannot be opened. The login must refuse rather than treat an unopenable
    enrolment as an absent one — that is the fail-OPEN direction this whole
    finding is about, reached by a different route.

    The status moves to ENCRYPTING in the same statement because
    `totp_secrets_encrypted_is_complete` forbids a nonce-less ENCRYPTED row
    outright — the constraint makes this state unreachable for a finished row,
    which is the point of it. What remains representable is a row interrupted
    mid-migration, and that is what is set up here.
    """
    await enrolled_encrypted.execute(
        "UPDATE totp_secrets SET secret_nonce = NULL, "
        "migration_status = 'ENCRYPTING' WHERE username=$1", USERNAME)
    r = await _login(client, code=pyotp.TOTP(SECRET).now())
    assert r.status_code == 503, (
        f"login returned {r.status_code} for an enrolment whose ciphertext "
        f"cannot be opened; an undeterminable second factor must refuse")
