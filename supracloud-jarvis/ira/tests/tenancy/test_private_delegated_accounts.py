"""Granting access creates a working account, on a real migrated database.

`040_membership_grants.sql` installed `delegated_users_closed`, a trigger that
refuses every INSERT into `delegated_users` and says what to do instead:
"Create a user and a tenant_membership instead." `security/delegated.py` kept
inserting into the closed table, so on every database migrated through 040 the
owner's grant raised the trigger's error and no second person could be given
an account.

`test_delegated_access.py` did not notice, and could not have: it replaces the
database with `_FakeConn` and stubs `create_delegated_user` outright, so no
statement it checks ever reaches PostgreSQL. This file runs the same flow
against a migrated database as `ira_app`.

── The second defect, found while reproducing the first ─────────────────────

Legacy accounts were dead too. `delegated_users` has carried FORCE ROW LEVEL
SECURITY keyed on `app.tenant_id` since `035`, and every query in the store ran
on a bare `acquire()` that binds no tenant. As the runtime role the table
matched zero rows: a legacy account could not log in, resolved to no role, and
was missing from the owner's list, so the owner could not revoke it either. It
failed closed, and it was still broken. `test_a_legacy_account_*` covers it.

── What is real here ───────────────────────────────────────────────────────

The `/auth/token` route, `require_auth` and the JWTs, the access router with
its owner password and confirmation gates, `DelegatedScopeMiddleware`, the
principal resolver, the account store, PostgreSQL and its RLS. Replaced: the
Redis-backed lockout counter and token-version counter (not under test, and both
fail soft), and `dev_mode_active`, forced off because DEV_MODE skips the very
scope enforcement being measured.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
httpx = pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from config import get_settings  # noqa: E402
from core.identity.principal import PRIVATE_TENANT_ID, private_user_id  # noqa: E402
from security.roles import scopes_for  # noqa: E402

GRANTED = "pda-granted"
GRANTED_PASSWORD = "granted-own-secret-1"
GRANTED_ROLE = "trusted_user"

LEGACY = "pda-legacy"
LEGACY_PASSWORD = "legacy-own-secret-1"
LEGACY_ROLE = "family_admin"

NAMES = [GRANTED, LEGACY]


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


async def _purge(admin) -> None:
    """Remove every row this file can create.

    Deleting the `users` row cascades to `user_credentials`,
    `tenant_memberships` and `workspace_memberships`. Membership residue
    matters beyond tidiness: `rollback/092` refuses while any private account
    remains, so a leak here would fail the chain-rollback step much later.
    """
    await admin.execute("DELETE FROM delegated_users WHERE username = ANY($1::text[])", NAMES)
    # Migration 096: a user with a token-version counter cannot be deleted
    # (ON DELETE RESTRICT, by design), so the counter goes first.
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id = ANY($1::uuid[])",
                        [private_user_id(n) for n in NAMES])
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])",
                        [private_user_id(n) for n in NAMES])
    await admin.execute("DELETE FROM access_audit_log WHERE target = ANY($1::text[])", NAMES)


@pytest.fixture
async def admin():
    """A superuser connection, for seeding and inspection only. Clean either side."""
    conn = await asyncpg.connect(ADMIN_DSN)
    await _purge(conn)
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest.fixture
async def pool():
    """`utils.db._pool` pointed at the test database as `ira_app`."""
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


def _async_return(value):
    async def _f(*_a, **_k):
        return value
    return _f


def _redis():
    r = AsyncMock()
    r.exists = AsyncMock(return_value=0)    # no token is on the revocation list
    r.get = AsyncMock(return_value=None)    # token version 0
    r.incr = AsyncMock(return_value=1)
    r.setex = AsyncMock(return_value=True)
    r.set = AsyncMock(return_value=True)
    return r


@pytest.fixture
async def client(monkeypatch, pool, admin):
    """The real auth routes, access router and scope middleware, on one loop."""
    from fastapi import Depends, FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from api.middleware.auth import require_auth
    from api.routes.access import router as access_router
    from auth.routes import register_auth_routes
    from core.identity.provisioning import ensure_private_owner_identity
    from core.identity.resolver import resolve_principal
    from security import scope_enforcer

    # DEV_MODE returns the owner from require_auth without reading a token and
    # skips the scope middleware entirely; the suite may run with it on.
    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)
    monkeypatch.setattr("utils.redis_client.get_redis", lambda: _redis())
    monkeypatch.setattr("utils.account_lockout.is_locked", _async_return(False))
    monkeypatch.setattr("utils.account_lockout.record_failure", _async_return((1, False)))
    monkeypatch.setattr("utils.account_lockout.clear_failures", _async_return(None))
    scope_enforcer.invalidate_role_cache()

    # What the app does at startup. A no-op where 023/035 already seeded the
    # configured owner; it matters when the owner is not called "admin".
    await ensure_private_owner_identity()

    app = FastAPI()
    limiter = Limiter(key_func=lambda _r: "pda", enabled=False)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    app.include_router(access_router, prefix="/api/v1")

    # Probes on two mapped prefixes: one every grantable role may reach (chat),
    # one only family_admin and above may (settings_read). Each answers with the
    # principal the resolver builds, so the scopes asserted are the runtime ones.
    async def _whoami(username: str = Depends(require_auth)):
        p = await resolve_principal(username)
        return {"user_id": str(p.user_id), "roles": sorted(p.roles),
                "scopes": sorted(p.scopes), "is_owner": p.is_owner}

    app.add_api_route("/api/v1/chat/whoami", _whoami, methods=["GET"])
    app.add_api_route("/api/v1/trust/status", _whoami, methods=["GET"])
    app.add_middleware(scope_enforcer.DelegatedScopeMiddleware)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://pda") as c:
        yield c
    scope_enforcer.invalidate_role_cache()


def _owner() -> tuple[str, str]:
    s = get_settings()
    return s.ira_admin_username, s.ira_admin_password


async def _login(client, username: str, password: str):
    return await client.post("/auth/token", data={"username": username, "password": password})


async def _token(client, username: str, password: str) -> dict:
    r = await _login(client, username, password)
    assert r.status_code == 200, f"login as {username!r} failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _confirmed(client, path: str, body: dict, headers: dict) -> dict:
    """Stage an owner-gated action, then confirm it with its token."""
    draft = await client.post(path, json=body, headers=headers)
    assert draft.status_code == 200, draft.text
    assert draft.json()["status"] == "confirmation_required", draft.text
    done = await client.post(path, json={**body, "confirm_token": draft.json()["token"]},
                             headers=headers)
    assert done.status_code == 200, done.text
    return done.json()


async def _grant(client, owner_headers, *, username=GRANTED, password=GRANTED_PASSWORD,
                 role=GRANTED_ROLE) -> dict:
    _, owner_password = _owner()
    return await _confirmed(client, "/api/v1/access/grant", {
        "person_name": "Probe Person", "role": role, "new_username": username,
        "new_password": password, "owner_password": owner_password, "channel": "ui",
    }, owner_headers)


async def _revoke(client, owner_headers, user_id: str) -> dict:
    _, owner_password = _owner()
    return await _confirmed(client, "/api/v1/access/revoke", {
        "user_id": user_id, "owner_password": owner_password, "channel": "ui",
    }, owner_headers)


async def _seed_legacy(admin) -> uuid.UUID:
    """A `delegated_users` row as it existed before 040, after a start.

    `session_replication_role = replica` skips the closing trigger for this
    session only, which is how a row that predates the trigger is reproduced
    without disabling it for anybody else on a shared database.

    Then the start-up seeder runs, as every start after migration 096 does: it
    gives the legacy account the `users` row its token-version counter needs.
    Such a row can only predate a start, since the closing trigger refuses new
    ones. `test_global_logout_durable.py` covers the account before a start,
    when it is refused rather than passed.
    """
    from api.middleware.auth import hash_password

    row_id = uuid.uuid4()
    async with admin.transaction():
        await admin.execute("SET LOCAL session_replication_role = replica")
        await admin.execute(
            """INSERT INTO delegated_users
                   (id, username, password_hash, role, is_primary_owner, tenant_id)
               VALUES ($1, $2, $3, $4, FALSE, $5)""",
            row_id, LEGACY, hash_password(LEGACY_PASSWORD), LEGACY_ROLE, PRIVATE_TENANT_ID,
        )
    from core.identity import token_versions

    report = await token_versions.seed_from_redis()
    assert report.legacy_rows_created == 1, report
    return row_id


# ─────────────────────────────────────────────────────────────────────────────
# Granting access
# ─────────────────────────────────────────────────────────────────────────────


async def test_granting_access_creates_the_account_in_the_membership_model(client, admin):
    """**The defect.** On main this raised "delegated_users is closed".

    The account is a `users` row at the id the runtime principal carries, its
    own credential, and an active membership of the private tenant holding the
    private role. Nothing is written to `delegated_users`.
    """
    owner = await _token(client, *_owner())
    done = await _grant(client, owner)
    granted = done["granted"]
    user_id = private_user_id(GRANTED)

    assert granted["username"] == GRANTED
    assert granted["role"] == GRANTED_ROLE
    assert granted["id"] == str(user_id)
    assert granted["active"] is True and granted["is_primary_owner"] is False
    assert GRANTED_PASSWORD not in str(done)

    user = await admin.fetchrow(
        "SELECT external_subject, auth_provider, status FROM users WHERE id = $1", user_id)
    assert dict(user) == {"external_subject": GRANTED, "auth_provider": "password",
                          "status": "active"}

    member = await admin.fetchrow(
        """SELECT role, private_role, status, membership_grants_access
             FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2""",
        PRIVATE_TENANT_ID, user_id)
    assert dict(member) == {"role": "member", "private_role": GRANTED_ROLE,
                            "status": "active", "membership_grants_access": True}

    stored = await admin.fetchval(
        "SELECT password_hash FROM user_credentials WHERE user_id = $1", user_id)
    assert stored and stored != GRANTED_PASSWORD and stored.startswith("$2")

    assert await admin.fetchval(
        "SELECT count(*) FROM delegated_users WHERE username = $1", GRANTED) == 0
    assert "access_granted" in [r["action"] for r in await admin.fetch(
        "SELECT action FROM access_audit_log WHERE target = $1", GRANTED)]


async def test_the_new_account_logs_in_with_its_own_password(client, admin):
    owner = await _token(client, *_owner())
    await _grant(client, owner)

    assert (await _login(client, GRANTED, GRANTED_PASSWORD)).status_code == 200
    assert (await _login(client, GRANTED, "not-their-password")).status_code == 401
    # The owner's password is not this account's password.
    assert (await _login(client, GRANTED, _owner()[1])).status_code == 401


async def test_the_new_account_holds_exactly_its_roles_scopes(client, admin):
    """Through the token, the middleware and the resolver, not a direct call."""
    owner = await _token(client, *_owner())
    await _grant(client, owner)
    delegate = await _token(client, GRANTED, GRANTED_PASSWORD)

    r = await client.get("/api/v1/chat/whoami", headers=delegate)
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": str(private_user_id(GRANTED)), "roles": [GRANTED_ROLE],
                        "scopes": sorted(scopes_for(GRANTED_ROLE)), "is_owner": False}

    # A mapped route needing a scope trusted_user lacks is refused by the
    # middleware. The owner reaches it: the refusal is the role's doing.
    assert (await client.get("/api/v1/trust/status", headers=delegate)).status_code == 403
    assert (await client.get("/api/v1/trust/status", headers=owner)).status_code == 200


async def test_the_new_account_is_refused_the_owner_only_routes(client, admin):
    owner = await _token(client, *_owner())
    await _grant(client, owner)
    delegate = await _token(client, GRANTED, GRANTED_PASSWORD)
    _, owner_password = _owner()

    for method, path, body in (
        ("GET", "/api/v1/access/users", None),
        ("GET", "/api/v1/access/audit", None),
        ("POST", "/api/v1/access/grant", {
            "person_name": "X", "role": "viewer", "new_username": "pda-escalate",
            "new_password": "whatever-123", "owner_password": owner_password}),
        ("POST", "/api/v1/access/revoke", {
            "user_id": str(private_user_id(GRANTED)), "owner_password": owner_password}),
    ):
        r = await client.request(method, path, json=body, headers=delegate)
        assert r.status_code == 403, f"{method} {path} answered {r.status_code} to a delegate"

    # Control: the owner reaches the same listing and sees the new account.
    listed = await client.get("/api/v1/access/users", headers=owner)
    assert listed.status_code == 200
    assert GRANTED in [u["username"] for u in listed.json()["users"]]
    assert await admin.fetchval(
        "SELECT count(*) FROM users WHERE external_subject = 'pda-escalate'") == 0


async def test_the_owner_only_routes_refuse_a_delegate_without_the_middleware(pool, admin,
                                                                             monkeypatch):
    """The route's own gate, not only the middleware in front of it."""
    from fastapi import HTTPException

    from api.routes.access import audit_log, list_users

    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)
    for route in (list_users, audit_log):
        with pytest.raises(HTTPException) as exc:
            await (route(_user=GRANTED) if route is list_users
                   else route(limit=10, _user=GRANTED))
        assert exc.value.status_code == 403


async def test_the_grant_gives_no_workspace_access(client, admin):
    """A granted account is a member of the tenant and of no workspace.

    The private tenant's default workspace holds the owner's conversations and
    memory, and they are not yet owned per user (ADR-0009, proposed). A workspace
    membership here would hand them to the new account, so the grant creates
    none, and the compatibility path that serves the owner refuses everybody
    else.
    """
    from fastapi import HTTPException

    from api.middleware.workspace import resolve_workspace_principal

    owner = await _token(client, *_owner())
    await _grant(client, owner)

    assert await admin.fetchval(
        "SELECT count(*) FROM workspace_memberships WHERE user_id = $1",
        private_user_id(GRANTED)) == 0
    with pytest.raises(HTTPException) as exc:
        await resolve_workspace_principal(GRANTED, None)
    assert exc.value.status_code == 403
    default_ws = await admin.fetchval(
        "SELECT id FROM workspaces WHERE tenant_id = $1 AND is_default", PRIVATE_TENANT_ID)
    with pytest.raises(HTTPException) as exc:
        await resolve_workspace_principal(GRANTED, str(default_ws))
    assert exc.value.status_code == 403


async def test_a_taken_name_is_refused(client, admin):
    owner = await _token(client, *_owner())
    await _grant(client, owner)
    _, owner_password = _owner()
    again = await client.post("/api/v1/access/grant", json={
        "person_name": "Probe Person", "role": "viewer", "new_username": GRANTED,
        "new_password": "another-secret-1", "owner_password": owner_password,
    }, headers=owner)
    assert again.status_code == 409
    assert await admin.fetchval(
        "SELECT private_role FROM tenant_memberships WHERE user_id = $1",
        private_user_id(GRANTED)) == GRANTED_ROLE


async def test_the_seeded_owner_seat_cannot_be_given_a_password(pool, admin, monkeypatch):
    """`023` seeds `private_user_id("admin")` as the primary owner.

    On a deployment whose owner is called something else, "admin" is not the
    owner's name and passes validation, but its id is that seeded seat.
    Granting it would write a credential for the primary-owner membership.
    """
    from security import delegated

    # The owner is renamed to "ira" for this test. Both helpers that read the
    # owner's name have to agree, or validation refuses "admin" as the owner's
    # login before the store's own check, the subject here, is reached.
    monkeypatch.setattr(delegated, "is_primary_owner_username", lambda name: name == "ira")
    monkeypatch.setattr(delegated, "reserved_identity",
                        lambda name: "the primary owner" if name == "ira" else None)
    seat = private_user_id("admin")
    assert await admin.fetchval(
        "SELECT role FROM tenant_memberships WHERE tenant_id = $1 AND user_id = $2",
        PRIVATE_TENANT_ID, seat) == "primary_owner", "023's seeded owner seat is missing"

    with pytest.raises(delegated.AccountExists):
        await delegated.create_delegated_user(
            username="admin", password="seat-secret-123", role="viewer")
    assert await admin.fetchval(
        "SELECT count(*) FROM user_credentials WHERE user_id = $1", seat) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Revocation and role change
# ─────────────────────────────────────────────────────────────────────────────


async def test_revoking_the_new_account_ends_its_access(client, admin):
    owner = await _token(client, *_owner())
    granted = (await _grant(client, owner))["granted"]
    delegate = await _token(client, GRANTED, GRANTED_PASSWORD)
    assert (await client.get("/api/v1/chat/whoami", headers=delegate)).status_code == 200

    revoked = (await _revoke(client, owner, granted["id"]))["revoked"]
    assert revoked["active"] is False

    # The token it already holds is denied, and it cannot get another.
    assert (await client.get("/api/v1/chat/whoami", headers=delegate)).status_code == 403
    assert (await _login(client, GRANTED, GRANTED_PASSWORD)).status_code == 401
    member = await admin.fetchrow(
        "SELECT status, membership_grants_access FROM tenant_memberships WHERE user_id = $1",
        private_user_id(GRANTED))
    assert dict(member) == {"status": "suspended", "membership_grants_access": False}


async def test_changing_the_new_accounts_role_changes_its_scopes(client, admin):
    owner = await _token(client, *_owner())
    granted = (await _grant(client, owner))["granted"]
    _, owner_password = _owner()
    changed = (await _confirmed(client, "/api/v1/access/role", {
        "user_id": granted["id"], "role": "family_admin", "owner_password": owner_password,
    }, owner))["user"]
    assert changed["role"] == "family_admin"

    delegate = await _token(client, GRANTED, GRANTED_PASSWORD)
    r = await client.get("/api/v1/trust/status", headers=delegate)
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == sorted(scopes_for("family_admin"))
    assert await admin.fetchval(
        "SELECT role FROM tenant_memberships WHERE user_id = $1",
        private_user_id(GRANTED)) == "member"


# ─────────────────────────────────────────────────────────────────────────────
# Accounts that predate 040
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_legacy_account_logs_in_and_holds_its_role(client, admin):
    """On main this account could not log in: RLS hid its row from `ira_app`."""
    await _seed_legacy(admin)

    delegate = await _token(client, LEGACY, LEGACY_PASSWORD)
    r = await client.get("/api/v1/trust/status", headers=delegate)
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == [LEGACY_ROLE]
    assert r.json()["scopes"] == sorted(scopes_for(LEGACY_ROLE))
    assert (await _login(client, LEGACY, "not-their-password")).status_code == 401


async def test_a_legacy_account_is_listed_and_can_be_revoked(client, admin):
    row_id = await _seed_legacy(admin)
    owner = await _token(client, *_owner())

    listed = await client.get("/api/v1/access/users", headers=owner)
    assert str(row_id) in [u["id"] for u in listed.json()["users"]]

    delegate = await _token(client, LEGACY, LEGACY_PASSWORD)
    revoked = (await _revoke(client, owner, str(row_id)))["revoked"]
    assert revoked["active"] is False
    assert (await client.get("/api/v1/chat/whoami", headers=delegate)).status_code == 403
    assert (await _login(client, LEGACY, LEGACY_PASSWORD)).status_code == 401


async def test_a_legacy_account_is_still_refused_the_owner_only_routes(client, admin):
    await _seed_legacy(admin)
    delegate = await _token(client, LEGACY, LEGACY_PASSWORD)
    assert (await client.get("/api/v1/access/users", headers=delegate)).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# What 092 refuses, whoever writes the row
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tenant_role,private_role,detail", [
    ("member", "primary_owner", "primary_owner is never grantable"),
    ("admin", "owner_equivalent", "a private account is never a tenant admin"),
    ("owner", "trusted_user", "a private account is never a tenant owner"),
    ("member", "viewer", "the private viewer holds the tenant viewer role"),
    ("member", "superuser", "an unknown private role"),
])
async def test_the_schema_refuses_an_impossible_private_account(admin, tenant_role,
                                                                private_role, detail):
    """Checked on the superuser connection: these are constraints, not policies."""
    user_id = private_user_id(GRANTED)
    await admin.execute(
        "INSERT INTO users (id, display_name, auth_provider, external_subject) "
        "VALUES ($1, $2, 'password', $2)", user_id, GRANTED)
    with pytest.raises(asyncpg.CheckViolationError):
        await admin.execute(
            """INSERT INTO tenant_memberships (tenant_id, user_id, role, private_role, status)
               VALUES ($1, $2, $3, $4, 'active')""",
            PRIVATE_TENANT_ID, user_id, tenant_role, private_role)


async def test_the_schema_keeps_private_roles_out_of_other_tenants(admin):
    tenant = uuid.uuid4()
    user_id = private_user_id(GRANTED)
    await admin.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                        tenant, f"pda-{tenant.hex[:8]}")
    try:
        await admin.execute(
            "INSERT INTO users (id, display_name, auth_provider, external_subject) "
            "VALUES ($1, $2, 'password', $2)", user_id, GRANTED)
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                """INSERT INTO tenant_memberships (tenant_id, user_id, role, private_role, status)
                   VALUES ($1, $2, 'member', 'trusted_user', 'active')""",
                tenant, user_id)
    finally:
        await admin.execute("DELETE FROM tenant_memberships WHERE tenant_id = $1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
