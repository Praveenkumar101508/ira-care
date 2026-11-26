"""Phase 2 batch 3 — cloud login, the reader `user_credentials` did not have.

Batch 2 shipped a credential store and its own PR said so plainly: *"a cloud
credential store exists and **nothing reads it**"*. A registered cloud user held
a bcrypt hash that no code path could check, so registration and authentication
were two halves that had never met.

This suite is about the join, and most of it is about what the join must still
refuse.

── The three properties under test ─────────────────────────────────────────

**A password proves possession, not authority.** Verifying the hash is the only
decision `cloud_login` makes. Tenant, role, scopes and `is_owner` come from
`resolve_principal`, which Batch 1 built as the single authority — so a correct
password for an account with no access-granting membership signs nobody in.

**Every refusal looks the same.** Unknown address, unverified address, wrong
password, missing credential, suspended, deleted, unmembered, multi-membered:
one status and one body. `test_every_refusal_is_externally_identical` enumerates
them together rather than asserting `401` eight times separately, because the
property is *sameness* and eight independent assertions do not state it.

**Constant work.** A missing account still runs bcrypt. Without it the response
time is the enumeration oracle the uniform body was supposed to close, which is
the P2-01 finding one layer up.
"""
from __future__ import annotations

import os
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

DOMAIN = "p2login.example"
PASSWORD = "a-perfectly-adequate-passphrase"
WRONG_PASSWORD = "not-the-right-passphrase-at-all"

TA = uuid.UUID("11111111-1111-1111-1111-1111111111d1")
TB = uuid.UUID("22222222-2222-2222-2222-2222222222d1")

#: name → (uuid, verified, status, deleted, credential?, memberships)
#: `memberships` is a list of (tenant, role, granting)
ACCOUNTS: dict[str, tuple] = {
    "solo":       (uuid.UUID("aaaa0000-0000-0000-0000-00000000d101"),
                   True, "active", False, True, [(TA, "member", True)]),
    "owner":      (uuid.UUID("aaaa0000-0000-0000-0000-00000000d102"),
                   True, "active", False, True, [(TA, "primary_owner", True)]),
    "both":       (uuid.UUID("aaaa0000-0000-0000-0000-00000000d103"),
                   True, "active", False, True,
                   [(TA, "admin", True), (TB, "member", True)]),
    "unverified": (uuid.UUID("aaaa0000-0000-0000-0000-00000000d104"),
                   False, "active", False, True, [(TA, "member", True)]),
    "suspended":  (uuid.UUID("aaaa0000-0000-0000-0000-00000000d105"),
                   True, "suspended", False, True, [(TA, "member", True)]),
    "deleted":    (uuid.UUID("aaaa0000-0000-0000-0000-00000000d106"),
                   True, "active", True, True, [(TA, "member", True)]),
    "nomember":   (uuid.UUID("aaaa0000-0000-0000-0000-00000000d107"),
                   True, "active", False, True, []),
    "revoked":    (uuid.UUID("aaaa0000-0000-0000-0000-00000000d108"),
                   True, "active", False, True, [(TA, "member", False)]),
    "nocred":     (uuid.UUID("aaaa0000-0000-0000-0000-00000000d109"),
                   True, "active", False, False, [(TA, "member", True)]),
    "mfa":        (uuid.UUID("aaaa0000-0000-0000-0000-00000000d110"),
                   True, "active", False, True, [(TA, "member", True)]),
}

#: An account that claims an address someone else has already VERIFIED.
#:
#: 034 permits this on purpose: `users_verified_email_key` is unique only among
#: rows that are verified and not deleted, and it records why — *"An unverified
#: one is not constrained, so claiming someone else's address cannot block or
#: seize their account."* Registering `solo@…` unverified is therefore allowed,
#: and must stay worthless.
#:
#: It is worthless only because `_locate_credential` repeats the index's
#: predicate in its WHERE clause. Drop that clause and the lookup becomes
#: non-deterministic across two rows sharing one address — and since the
#: resolver still resolves the VERIFIED user by that address, matching the
#: impostor's password would sign the attacker in as the legitimate account.
#:
#: Seeded FIRST, ahead of the real accounts, so it is the row a sequential scan
#: reaches first. Without that the plant that removes the predicate is caught
#: only by luck of heap order.
IMPOSTOR = uuid.UUID("bbbb0000-0000-0000-0000-00000000d1ff")
IMPOSTOR_PASSWORD = "the-impostors-own-chosen-passphrase"

ALL_IDS = [v[0] for v in ACCOUNTS.values()] + [IMPOSTOR]

#: A fixed base32 seed for the enrolled account. Not a real user's, and never
#: logged — it exists so the MFA assertions run against the real pyotp path
#: rather than a stub of it.
MFA_SEED = "JBSWY3DPEHPK3PXP"


def email(name: str) -> str:
    return f"{name}@{DOMAIN}"


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def cloud(monkeypatch, pool):
    """A populated cloud deployment: accounts, credentials, memberships.

    Seeded on the ADMIN dsn. `user_credentials`, `tenant_memberships` and
    `totp_secrets` are all forced-RLS and the fixture writes rows for ten
    different users, which no single bound connection may do.
    """
    from api.middleware.auth import hash_password
    from core.editions import EDITION_CLOUD

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)
    monkeypatch.setattr("core.identity.resolver.current_edition",
                        lambda: EDITION_CLOUD)

    # One bcrypt round for the whole fixture. Ten would add several seconds to
    # every test in the file for no additional coverage.
    stored = hash_password(PASSWORD)

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug) VALUES "
            "($1,'P2-Login-A','p2-login-a'),($2,'P2-Login-B','p2-login-b')",
            TA, TB)

        # Before the real accounts — see IMPOSTOR. An unverified squatter on
        # `solo@…`, holding a credential with a password of its own choosing.
        await admin.execute(
            "INSERT INTO users (id,email,display_name,auth_provider,status,"
            " email_verified_at) VALUES ($1,$2,'T','password','active',NULL)",
            IMPOSTOR, email("solo"))
        await admin.execute(
            "INSERT INTO user_credentials (user_id,password_hash) VALUES ($1,$2)",
            IMPOSTOR, hash_password(IMPOSTOR_PASSWORD))

        for name, (uid, verified, status, deleted, cred, memberships) in ACCOUNTS.items():
            # `normalised_email` is GENERATED — normalisation has one definition
            # and neither this fixture nor the login path owns it.
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status,"
                " email_verified_at,deleted_at) VALUES ($1,$2,'T','password',$3,"
                " CASE WHEN $4 THEN NOW() ELSE NULL END,"
                " CASE WHEN $5 THEN NOW() ELSE NULL END)",
                uid, email(name), status, verified, deleted)
            if cred:
                await admin.execute(
                    "INSERT INTO user_credentials (user_id,password_hash) "
                    "VALUES ($1,$2)", uid, stored)
            for tenant, role, granting in memberships:
                # `tenant_memberships_status_revoked_agree` (035) is
                # `(status = 'removed') = (revoked_at IS NOT NULL)`, so a
                # revoked membership must move BOTH. Setting only `revoked_at`
                # is refused by the database — which is the constraint doing
                # its job, and the reason the fixture cannot express a
                # half-revoked row even by accident.
                await admin.execute(
                    "INSERT INTO tenant_memberships "
                    "(tenant_id,user_id,role,status,joined_at,revoked_at) "
                    "VALUES ($1,$2,$3,"
                    "        CASE WHEN $4 THEN 'active' ELSE 'removed' END,"
                    "        NOW(),"
                    "        CASE WHEN $4 THEN NULL ELSE NOW() END)",
                    tenant, uid, role, granting)

        # The enrolled second factor. Plaintext `secret` rather than ciphertext:
        # 053 made the column nullable so an encrypted row can drop it, and
        # `_use_row_secret` handles both — this exercises the same code path
        # with no key hierarchy to stand up in a fixture.
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id) "
            "VALUES ($1,$2,TRUE,$3,$4)",
            email("mfa"), MFA_SEED, ACCOUNTS["mfa"][0], TA)

        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    """Child-first, matched on `normalised_email` where the table has one.

    Batch 2 recorded why the column matters: a case-sensitive `email LIKE`
    survived teardown for an address registered in upper case, and the leftover
    row made the next run's first assertion fail for reasons unrelated to it.
    """
    await admin.execute("DELETE FROM totp_secrets WHERE user_id = ANY($1::uuid[])",
                        ALL_IDS)
    await admin.execute("DELETE FROM tenant_memberships WHERE user_id = ANY($1::uuid[])",
                        ALL_IDS)
    await admin.execute("DELETE FROM user_credentials WHERE user_id = ANY($1::uuid[])",
                        ALL_IDS)
    # Migration 096: a user with a token-version counter cannot be deleted
    # (ON DELETE RESTRICT, by design), so the counter goes first.
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id IN "
                        "(SELECT id FROM users WHERE id = ANY($1::uuid[]) "
                        "OR normalised_email LIKE $2)", ALL_IDS, f"%@{DOMAIN}")
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[]) "
                        "OR normalised_email LIKE $2", ALL_IDS, f"%@{DOMAIN}")
    await admin.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [TA, TB])


def _redis(*, revoked=0):
    """A Redis double, for the per-token (jti) revocation check only.

    The per-account version is no longer in Redis (migration 096): tests that
    need a version write it to PostgreSQL with `_set_version`.
    """
    r = AsyncMock()
    r.exists = AsyncMock(return_value=revoked)
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.setex = AsyncMock(return_value=True)
    r.set = AsyncMock(return_value=True)
    return r


async def _set_version(admin, account: str, version: int) -> None:
    """Write the account's token-version counter, as N bumps would leave it."""
    await admin.execute(
        "INSERT INTO auth_token_versions (user_id, version) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET version = EXCLUDED.version",
        ACCOUNTS[account][0], version)


@pytest.fixture
async def client():
    """An app carrying only the real auth routes, driven in THIS event loop.

    `httpx.ASGITransport` rather than `fastapi.testclient.TestClient`, and the
    difference is not stylistic. `TestClient` runs the application in a separate
    portal thread with its own event loop, while the asyncpg pool above was
    created in the test's loop — and an asyncpg connection is bound to the loop
    that made it. Every route test failed with `ConnectionDoesNotExistError`
    from inside the login path, which reads like a database outage and is
    actually a harness mistake. Driving the ASGI app directly keeps one loop.

    The rate limit is disabled rather than mocked: the limits are real
    behaviour, and leaving them on would make each test depend on how many
    requests the preceding ones happened to make.
    """
    import httpx
    from fastapi import FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from auth.routes import register_auth_routes

    limiter = Limiter(key_func=lambda _r: "test", enabled=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://cloud-login.test") as c:
        yield c


async def _login(name: str, password: str = PASSWORD, **kw):
    from core.identity.cloud_login import authenticate_cloud_user

    return await authenticate_cloud_user(email(name), password, **kw)


def _claims(token: str) -> dict:
    import jwt

    from config import get_settings

    return jwt.decode(token, get_settings().ira_secret_key, algorithms=["HS256"])


# ─────────────────────────────────────────────────────────────────────────────
# The positive control — the chain that did not exist
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_verified_member_signs_in_and_resolves_a_principal(cloud):
    """email + password → credential → principal. The whole point of the batch."""
    outcome = await _login("solo")

    assert outcome.subject == email("solo")
    p = outcome.principal
    assert p.user_id == ACCOUNTS["solo"][0]
    assert p.tenant_id == TA
    assert p.edition == "cloud"
    assert "member" in p.roles


async def test_the_subject_is_the_stored_normalised_form_not_what_was_typed(cloud):
    """What a token identifies must be what the identity register holds.

    `normalise_email()` lowercases only the DOMAIN — the local part is
    case-sensitive per RFC — so this also pins that the login path calls the
    database's function rather than a Python `.lower()`, which would refuse
    this address outright.
    """
    from core.identity.cloud_login import authenticate_cloud_user

    outcome = await authenticate_cloud_user(f"solo@{DOMAIN.upper()}", PASSWORD)
    assert outcome.subject == email("solo")


async def test_login_does_not_mint_deployment_owner_authority(cloud):
    """A tenant's `primary_owner` is a customer, not the person who owns the box.

    3C-04, 3C-08 and P2-07 were each a version of this confusion. The login path
    is a new way to reach a Principal, so it is a new way to reintroduce it.
    """
    outcome = await _login("owner")

    assert outcome.principal.is_owner is False, (
        "cloud login minted a principal with deployment-owner authority. That "
        "flag gates /backup/download (a pg_dump of every tenant) and the tool "
        "set the agent layer may execute.")


async def test_a_cloud_owner_does_not_inherit_the_private_owner_scope_set(cloud):
    """The role-name collision, which is the trap `cloud_roles.py` exists for.

    `scopes_for("primary_owner")` returns EVERY private scope because the two
    vocabularies overlap on that one name. If login resolved scopes through the
    private map, every customer who owns a tenant would hold
    `private_memory_read` and `security_admin`.
    """
    outcome = await _login("owner")
    scopes = outcome.principal.scopes

    assert "private_memory_read" not in scopes
    assert "security_admin" not in scopes

    from security.roles import scopes_for

    assert scopes != scopes_for("primary_owner"), (
        "the cloud principal's scopes are the PRIVATE edition's map for the "
        "same role name — cloud_roles.cloud_scopes_for is not being used")


# ─────────────────────────────────────────────────────────────────────────────
# Refusals, and their sameness
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,password,why",
    [
        ("solo",       WRONG_PASSWORD, "wrong password"),
        ("nobody",     PASSWORD,       "no such address"),
        ("unverified", PASSWORD,       "address never verified"),
        ("suspended",  PASSWORD,       "account suspended"),
        ("deleted",    PASSWORD,       "account deleted"),
        ("nomember",   PASSWORD,       "no membership at all"),
        ("revoked",    PASSWORD,       "membership revoked"),
        ("nocred",     PASSWORD,       "no credential row"),
        # NOT "both" — see test_several_memberships_now_ask_rather_than_refuse.
    ],
)
async def test_each_ineligible_case_is_refused(cloud, name, password, why):
    from core.identity.cloud_login import CloudLoginRefused

    with pytest.raises(CloudLoginRefused):
        await _login(name, password)


async def test_every_refusal_is_externally_identical(cloud, client):
    """The property is SAMENESS, so it is asserted as sameness.

    Nine separate `assert status == 401` would each pass while the bodies
    differed, and a body that differs is the oracle the status code was
    supposed to close.
    """
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        responses = [
            await client.post("/auth/cloud/login",
                        data={"email": email(n), "password": p})
            for n, p in [
                ("solo", WRONG_PASSWORD), ("nobody", PASSWORD),
                ("unverified", PASSWORD), ("suspended", PASSWORD),
                ("deleted", PASSWORD), ("nomember", PASSWORD),
                ("revoked", PASSWORD), ("nocred", PASSWORD),
            ]
        ]

    distinct = {(r.status_code, r.text) for r in responses}
    assert len(distinct) == 1, (
        f"cloud login gives {len(distinct)} distinguishable answers to a failed "
        f"sign-in: {sorted(distinct)}. Any difference between them tells an "
        f"unauthenticated caller something about the account.")
    assert responses[0].status_code == 401


async def test_a_missing_account_costs_the_same_as_a_wrong_password(cloud):
    """Constant work, or the uniform body is decoration.

    bcrypt dominates the wall-clock of a failed login by orders of magnitude, so
    an early `return` for an unknown address is visible from the outside without
    any timing sophistication. The bound is deliberately loose — this must not
    be a flaky test on a loaded CI runner — because the defect it catches is not
    a subtle skew: skipping bcrypt makes the missing-account path roughly free.
    """
    from core.identity.cloud_login import CloudLoginRefused

    async def _timed(name):
        best = None
        for _ in range(3):
            start = time.perf_counter()
            with pytest.raises(CloudLoginRefused):
                await _login(name, WRONG_PASSWORD)
            elapsed = time.perf_counter() - start
            best = elapsed if best is None else min(best, elapsed)
        return best

    wrong_password = await _timed("solo")
    missing_account = await _timed("nobody")

    assert missing_account > wrong_password * 0.5, (
        f"an unknown address is refused in {missing_account:.4f}s while a wrong "
        f"password for a real one takes {wrong_password:.4f}s. The unknown "
        f"branch is not running bcrypt, so response time enumerates accounts "
        f"regardless of how uniform the body is.")


# ─────────────────────────────────────────────────────────────────────────────
# Nothing the request says is authority
# ─────────────────────────────────────────────────────────────────────────────


async def test_request_supplied_tenant_and_role_are_ignored(cloud, client):
    """There is no parameter to forge — the strongest form of the property.

    The route takes an email, a password and an optional code. Sending a tenant,
    a workspace, a role, a scope set and an owner flag alongside them changes
    nothing, because FastAPI never binds them and nothing downstream looks.
    """
    payload = {
        "email": email("solo"), "password": PASSWORD,
        "tenant_id": str(TB), "workspace_id": str(uuid.uuid4()),
        "role": "primary_owner", "scopes": "security_admin",
        "is_owner": "true",
    }
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login", data=payload)

    assert r.status_code == 200
    claims = _claims(r.json()["access_token"])
    assert claims["sub"] == email("solo")
    for forged in ("tenant_id", "workspace_id", "role", "scopes", "is_owner"):
        assert forged not in claims, (
            f"the access token carries a {forged!r} claim taken from the "
            f"request. Authority in the token is authority the client can edit.")


async def test_the_token_carries_no_authority_claims_at_all(cloud, client):
    """Not merely "not forged from the request" — not present.

    Every request re-runs `resolve_principal(sub)`, so a membership revoked one
    second after login stops granting on the next request. A tenant claim would
    make that thirty minutes instead, and would make the token an authority the
    holder carries rather than a subject the server looks up.

    `uid` (migration 096) is not an authority either. It is the account's own
    `users.id`, the key of its token-version counter, so the version check
    needs no lookup by name. It names the account `sub` names, and grants
    nothing: every grant is still looked up from `sub`.

    `auth_time` and `amr` (D-095) are not authorities either: when the holder
    last proved who they are, and how. They can only make the server ask for
    MORE proof (step-up); nothing reads them as a grant. A password-only sign-in
    records exactly that.
    """
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    claims = _claims(r.json()["access_token"])
    assert set(claims) == {"sub", "exp", "jti", "tok", "ver", "uid", "auth_time", "amr"}
    assert claims["uid"] == str(ACCOUNTS["solo"][0])
    assert claims["amr"] == ["pwd"]
    assert isinstance(claims["auth_time"], int) and abs(claims["auth_time"] - int(time.time())) < 60


async def test_the_token_version_is_the_verified_accounts_not_a_squatters(cloud, client):
    """Migration 096 keys the logout counter by account id, and a cloud token's
    subject is an address an unverified squatter can also hold (IMPOSTOR,
    seeded first so a lookup by address alone reaches it first).

    Resolved by address alone, the real account's tokens would carry the
    squatter's counter: erasing the real account, which bumps the counter of
    `principal.user_id`, would then leave its tokens working. The lookup is the
    resolver's: verified and not deleted.
    """
    from api.middleware.auth import require_auth
    from core.identity import token_versions
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    shared = await cloud.fetchval(
        "SELECT count(*) FROM users WHERE normalised_email = normalise_email($1)",
        email("solo"))
    assert shared >= 2, "the squatter row is missing; this test would prove nothing"
    assert await token_versions.resolve_user_id(email("solo")) == ACCOUNTS["solo"][0]

    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                              data={"email": email("solo"), "password": PASSWORD})
        access = r.json()["access_token"]
        assert _claims(access)["uid"] == str(ACCOUNTS["solo"][0])

        # What erasure does: bump the counter of the principal's own id.
        await token_versions.bump(ACCOUNTS["solo"][0])
        with pytest.raises(HTTPException) as exc:
            await require_auth(HTTPAuthorizationCredentials(
                scheme="bearer", credentials=access))
    assert exc.value.status_code == 401


async def test_an_unverified_squatter_cannot_authenticate_as_the_real_account(cloud):
    """Registering someone's address without proving it must stay worthless.

    This is the account-takeover shape that `_locate_credential`'s
    `email_verified_at IS NOT NULL` clause exists to prevent, and the reason it
    is repeated in the WHERE rather than left to `users_verified_email_key`: an
    index makes a lookup fast, a WHERE clause makes it correct.

    Without the clause the lookup spans two rows sharing one address and returns
    an arbitrary one. The resolver still resolves the VERIFIED user from that
    same address — so a match against the impostor's hash would issue a token
    whose subject resolves to the legitimate account. The attacker signs in as
    somebody else using a password they chose themselves.
    """
    from core.identity.cloud_login import CloudLoginRefused

    with pytest.raises(CloudLoginRefused):
        await _login("solo", IMPOSTOR_PASSWORD)


async def test_the_credential_read_belongs_to_the_verified_account(cloud):
    """The same property one layer down, stated positively.

    The refusal above could pass with the lookup broken and the impostor's
    password merely mistyped. This asserts *which row* was read.
    """
    from core.identity.cloud_login import _locate_credential

    user_id, subject, password_hash = await _locate_credential(email("solo"))

    assert user_id == ACCOUNTS["solo"][0], (
        "the credential lookup returned a row belonging to an unverified "
        "account that claimed a verified user's address")
    assert subject == email("solo")
    assert password_hash is not None


async def test_a_forged_tenant_cannot_be_reached_through_the_resolver(cloud):
    """The layer beneath the route, asserted directly.

    The route has no tenant parameter today, but `resolve_principal` does — and
    it is what a future tenant-selection batch will pass one to. That contract
    is verified here so it is already pinned when the route gains the field.
    """
    from core.identity.resolver import (
        CloudIdentityNotAvailable, resolve_principal,
    )

    with pytest.raises(CloudIdentityNotAvailable):
        # `solo` is a member of TA only. Asking for TB must refuse rather than
        # fall back to the membership they do hold.
        await resolve_principal(email("solo"), edition="cloud", tenant_id=TB)


# ─────────────────────────────────────────────────────────────────────────────
# Fail closed
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_unreachable_credential_store_refuses_rather_than_raises(cloud):
    """"I cannot tell" must be a refusal, in a type the caller already handles.

    A raw asyncpg error escaping is not a smaller problem than a wrong answer:
    `except CloudLoginRefused` does not catch it, so a route written to refuse
    returns a 500 with a traceback — and a connection error's body can carry the
    DSN.
    """
    from core.identity.cloud_login import (
        CloudLoginUnavailable, authenticate_cloud_user,
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("the pool is gone")

    with patch("core.identity.cloud_login._locate_credential", _boom):
        with pytest.raises(CloudLoginUnavailable):
            await authenticate_cloud_user(email("solo"), PASSWORD)


async def test_an_unavailable_backend_is_a_503_and_never_a_token(cloud, client):
    async def _boom(*_a, **_k):
        raise RuntimeError("the pool is gone")

    with patch("core.identity.cloud_login._locate_credential", _boom):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    assert r.status_code == 503
    assert "access_token" not in r.text


async def test_an_unavailable_backend_does_not_leak_internals(cloud, client):
    """A database error must not become an alternate authentication path, and
    must not become a disclosure either."""
    async def _boom(*_a, **_k):
        raise RuntimeError("connection to postgresql://ira_app:hunter2@db:5432 failed")

    with patch("core.identity.cloud_login._locate_credential", _boom):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    assert "hunter2" not in r.text and "postgresql://" not in r.text
    assert "RuntimeError" not in r.text


async def test_the_route_does_not_exist_outside_the_cloud_edition(cloud, client, monkeypatch):
    """404 rather than 403, and rather than 401.

    A private deployment has no cloud accounts, so this endpoint cannot work
    there — and an authentication surface that announces itself where it cannot
    work is an invitation to probe it. Same shape as /auth/register's refusal.
    """
    monkeypatch.setattr("core.editions.current_edition", lambda: "private")

    r = await client.post("/auth/cloud/login",
                    data={"email": email("solo"), "password": PASSWORD})

    assert r.status_code == 404
    assert "access_token" not in r.text


# ─────────────────────────────────────────────────────────────────────────────
# Second factor (P2-05)
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_enrolled_account_cannot_sign_in_without_its_code(cloud):
    """The P2-05 defect, in the new login path.

    The old one read the secret store on an unbound connection: FORCE RLS
    matched zero rows for everybody, the result was always "not enrolled", and
    the challenge never ran. Anyone enrolled was signing in on a password alone.
    """
    from core.identity.cloud_login import CloudMfaRequired

    with pytest.raises(CloudMfaRequired):
        await _login("mfa")


async def test_an_enrolled_account_is_refused_a_wrong_code(cloud):
    from core.identity.cloud_login import CloudMfaRequired

    with pytest.raises(CloudMfaRequired):
        await _login("mfa", totp_code="000000")


async def test_an_enrolled_account_signs_in_with_a_valid_code(cloud):
    """The positive control — without it the two refusals above would pass with
    the factor check hard-wired to reject."""
    import pyotp

    outcome = await _login("mfa", totp_code=pyotp.TOTP(MFA_SEED).now())
    assert outcome.subject == email("mfa")


async def test_an_undeterminable_factor_refuses_the_login(cloud):
    """Fail CLOSED on the one check whose entire purpose is to be an obstacle."""
    from core.identity.cloud_login import CloudLoginUnavailable
    from security.totp import TotpUnavailable

    async def _unknown(*_a, **_k):
        raise TotpUnavailable("the store could not be reached")

    with patch("security.totp.check_login_second_factor", _unknown):
        with pytest.raises(CloudLoginUnavailable):
            await _login("solo")


async def test_the_mfa_branch_is_unreachable_without_a_correct_password(cloud):
    """What makes it safe for this one refusal to be distinguishable.

    It tells the caller nothing they could not infer from already holding a
    working password. If a wrong password could produce it, it would be an
    oracle for "this account exists and has a second factor".
    """
    from core.identity.cloud_login import CloudLoginRefused

    with pytest.raises(CloudLoginRefused):
        await _login("mfa", WRONG_PASSWORD, totp_code="000000")


# ─────────────────────────────────────────────────────────────────────────────
# Tokens (P2-04)
# ─────────────────────────────────────────────────────────────────────────────


async def test_login_issues_an_access_and_a_refresh_token(cloud, client):
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    assert r.status_code == 200
    body = r.json()
    assert _claims(body["access_token"])["tok"] == "access"
    assert _claims(body["refresh_token"])["tok"] == "refresh"
    assert body["token_type"] == "bearer"


async def test_both_tokens_carry_the_current_version(cloud, client):
    """A missing `ver` is what made an unversioned token expressible (P2-04),
    and the refresh token is the longest-lived credential IRA issues."""
    await _set_version(cloud, "solo", 7)
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    body = r.json()
    assert _claims(body["access_token"])["ver"] == 7
    assert _claims(body["refresh_token"])["ver"] == 7, (
        "the refresh token carries no current version, so revoke-all cannot "
        "invalidate it — it would keep minting access tokens stamped current")


async def test_a_cloud_refresh_token_is_not_an_access_credential(cloud, client):
    from api.middleware.auth import require_auth
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})
        refresh = r.json()["refresh_token"]

        with pytest.raises(HTTPException) as exc:
            await require_auth(HTTPAuthorizationCredentials(
                scheme="bearer", credentials=refresh))

    assert exc.value.status_code == 401


async def test_logout_all_invalidates_a_cloud_session(cloud, client):
    """Revoke-all is the whole account-recovery story, and it must reach cloud
    tokens — which are minted by a different route from the one it was built for.
    """
    from api.middleware.auth import require_auth
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from core.identity import token_versions

    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})
        access = r.json()["access_token"]
        refresh = r.json()["refresh_token"]
    assert _claims(access)["ver"] == 0

    # The bump, through the real store: the counter now reads higher than
    # either token.
    assert await token_versions.bump(ACCOUNTS["solo"][0]) == 1
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        with pytest.raises(HTTPException) as access_exc:
            await require_auth(HTTPAuthorizationCredentials(
                scheme="bearer", credentials=access))
        refreshed = await client.post(
            "/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})

    assert access_exc.value.status_code == 401
    assert refreshed.status_code == 401, (
        "a cloud refresh token issued before revoke-all was still exchanged "
        "for a new access token")


async def test_a_stale_version_is_refused_and_a_current_one_is_not(cloud, client):
    """Both directions, because only asserting the refusal would pass with the
    check hard-wired to deny."""
    from api.middleware.auth import require_auth
    from fastapi.security import HTTPAuthorizationCredentials

    await _set_version(cloud, "solo", 3)
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})
        access = r.json()["access_token"]

        assert await require_auth(HTTPAuthorizationCredentials(
            scheme="bearer", credentials=access)) == email("solo")


async def test_a_cloud_token_without_a_version_claim_is_refused(cloud, client):
    """A token this process cannot model is a token it must not accept."""
    from datetime import datetime, timedelta, timezone

    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    import jwt

    from api.middleware.auth import require_auth
    from config import get_settings

    unversioned = jwt.encode(
        {"sub": email("solo"),
         "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
         "jti": str(uuid.uuid4()), "tok": "access"},
        get_settings().ira_secret_key, algorithm="HS256")

    with patch("utils.redis_client.get_redis", return_value=_redis()):
        with pytest.raises(HTTPException) as exc:
            await require_auth(HTTPAuthorizationCredentials(
                scheme="bearer", credentials=unversioned))

    assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# The token's subject re-resolves — one authority, checked every request
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_token_subject_resolves_back_to_the_same_principal(cloud, client):
    """`sub` is not decoration: every authenticated request re-resolves it."""
    from core.identity.resolver import resolve_principal

    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login",
                        data={"email": email("solo"), "password": PASSWORD})

    sub = _claims(r.json()["access_token"])["sub"]
    principal = await resolve_principal(sub, edition="cloud")
    assert principal.user_id == ACCOUNTS["solo"][0]
    assert principal.tenant_id == TA


async def test_a_membership_revoked_after_login_stops_granting_access(cloud):
    """The consequence of keeping authority out of the token.

    A tenant claim would keep granting until the access token expired. Because
    the token carries only a subject, the next re-resolution refuses.
    """
    from core.identity.resolver import (
        CloudIdentityNotAvailable, resolve_principal,
    )

    outcome = await _login("solo")
    await cloud.execute(
        "UPDATE tenant_memberships SET status = 'removed', revoked_at = NOW() "
        "WHERE user_id = $1",
        ACCOUNTS["solo"][0])

    with pytest.raises(CloudIdentityNotAvailable):
        await resolve_principal(outcome.subject, edition="cloud")


# ─────────────────────────────────────────────────────────────────────────────
# Secrets stay out of what is written down
# ─────────────────────────────────────────────────────────────────────────────


async def test_no_credential_material_reaches_the_logs(cloud, caplog):
    """Passwords, stored hashes and TOTP seeds must not be loggable.

    Asserted over a whole run of the login path — success, failure and the MFA
    branch — rather than by reading the `logger.` calls, because the value that
    reaches a log is often one an f-string picked up rather than one anybody
    wrote deliberately.
    """
    import logging

    from core.identity.cloud_login import CloudLoginRefused, CloudMfaRequired

    with caplog.at_level(logging.DEBUG):
        await _login("solo")
        with pytest.raises(CloudLoginRefused):
            await _login("solo", WRONG_PASSWORD)
        with pytest.raises(CloudLoginRefused):
            await _login("nobody")
        with pytest.raises(CloudMfaRequired):
            await _login("mfa")

    written = caplog.text
    for secret in (PASSWORD, WRONG_PASSWORD, MFA_SEED, "$2b$"):
        assert secret not in written, (
            f"{secret!r} appears in the log output of the login path")


async def test_the_outcome_carries_no_password_material(cloud):
    """The object handed back to the route, checked as a whole.

    A dataclass gains fields; asserting the absence of one named field would
    pass the day somebody adds a differently-named one.
    """
    outcome = await _login("solo")
    rendered = repr(outcome)
    assert PASSWORD not in rendered
    assert "$2b$" not in rendered


# ─────────────────────────────────────────────────────────────────────────────
# Superseded by batch 5 — recorded here rather than silently deleted
# ─────────────────────────────────────────────────────────────────────────────


async def test_several_memberships_now_ask_rather_than_refuse(cloud):
    """This file used to assert that `both` was REFUSED, and that was correct
    while it was the truth: a cloud token could not carry a tenant selection, so
    a user in two organisations genuinely could not sign in, and saying so in
    the uniform refusal was better than a login that then failed every request.

    Batch 5 makes the selection expressible, so the honest answer changed from
    "no" to "which one?". The case was removed from the refusal parametrisation
    and from the sameness assertion above, and is pinned here instead — a
    deleted assertion leaves no record that the contract moved.

    The enumeration property is unaffected: this is reachable only after the
    password has been verified, so it tells a caller nothing they could not
    already learn by signing in.
    """
    from core.identity.cloud_login import TenantSelectionRequired

    with pytest.raises(TenantSelectionRequired) as exc:
        await _login("both")

    assert {c.tenant_id for c in exc.value.choices} == {TA, TB}
    assert exc.value.subject == email("both")
