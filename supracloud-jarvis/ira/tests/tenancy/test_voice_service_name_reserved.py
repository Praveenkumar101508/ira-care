"""No household account may hold the voice service's name, on a real database.

`api/routes/chat.py` decides owner authority as

    owner = _is_owner(_user) or (_is_voice_service(_user) and req.is_voice_owner)

and `is_voice_service` is a comparison of the signed-in name with
`ira_voice_service_username` (default `ira-voice`). For that one name the
request body's `is_voice_owner` is believed, and `owner` is what `run_graph`,
`stage_action` and `run_coding_task` receive as `is_owner=`. 3C-08 made the
comparison refuse in cloud. In the private edition it still holds, which is
right for the voice service and wrong for anybody else holding the name.

`security/delegated.validate_new_username` refused the owner's login and the
reserved memory namespaces, and not this name. So the owner could grant a
household account called `ira-voice`; it logged in with its own password, its
token's `sub` was the voice service's, and every chat request it sent with
`{"is_voice_owner": true}` would carry owner tool authority.

On `main` at the time of writing that chat request is refused one step earlier,
because a granted account is a member of no workspace
(`test_private_delegated_accounts.test_the_grant_gives_no_workspace_access`).
The day household accounts get a workspace of their own, the name is the whole
gate. So the name is refused now: at the grant, at login, and in the role
lookup, the way a reserved memory namespace already is.

── What is real here ───────────────────────────────────────────────────────

The `/auth/token` route and its JWTs, the access router with its owner password
and confirmation gates, `DelegatedScopeMiddleware`, the principal resolver, the
account store, PostgreSQL and its RLS. Replaced: the Redis-backed lockout and
token-version counters (not under test; both fail soft) and `dev_mode_active`,
forced off because DEV_MODE skips the scope enforcement being measured.
"""
from __future__ import annotations

import os
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
from core.identity.principal import private_user_id  # noqa: E402

VOICE = get_settings().ira_voice_service_username
PASSWORD = "household-own-secret-1"
ROLE = "trusted_user"

#: A name with nothing special about it. Granted in the positive control, so a
#: 422 for the voice name cannot come from a harness that refuses every grant.
CONTROL = "vsn-control"

NAMES = [VOICE, CONTROL]


# ─────────────────────────────────────────────────────────────────────────────
# Harness (the one `test_private_delegated_accounts.py` uses, trimmed)
# ─────────────────────────────────────────────────────────────────────────────


async def _purge(admin) -> None:
    """Remove every row this file can create. The `users` row cascades."""
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


def _async_return(value):
    async def _f(*_a, **_k):
        return value
    return _f


def _redis():
    r = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.setex = AsyncMock(return_value=True)
    r.set = AsyncMock(return_value=True)
    return r


@pytest.fixture
async def client(monkeypatch, pool, admin):
    from fastapi import Depends, FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from api.middleware.auth import is_owner, is_voice_service, require_auth
    from api.routes.access import router as access_router
    from auth.routes import register_auth_routes
    from core.identity.provisioning import ensure_private_owner_identity
    from core.identity.resolver import resolve_principal
    from security import scope_enforcer

    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)
    monkeypatch.setattr("utils.redis_client.get_redis", lambda: _redis())
    monkeypatch.setattr("utils.account_lockout.is_locked", _async_return(False))
    monkeypatch.setattr("utils.account_lockout.record_failure", _async_return((1, False)))
    monkeypatch.setattr("utils.account_lockout.clear_failures", _async_return(None))
    scope_enforcer.invalidate_role_cache()
    await ensure_private_owner_identity()

    app = FastAPI()
    limiter = Limiter(key_func=lambda _r: "vsn", enabled=False)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    app.include_router(access_router, prefix="/api/v1")

    # A probe under /api/v1/chat, which every grantable role may reach through
    # the middleware. It answers with what chat.py would compute for a request
    # whose body says `is_voice_owner: true`.
    async def _chat_probe(username: str = Depends(require_auth)):
        p = await resolve_principal(username)
        return {"roles": sorted(p.roles),
                "owner_if_body_claims_it": is_owner(username) or is_voice_service(username)}

    app.add_api_route("/api/v1/chat/probe", _chat_probe, methods=["GET"])
    app.add_middleware(scope_enforcer.DelegatedScopeMiddleware)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://vsn") as c:
        yield c
    scope_enforcer.invalidate_role_cache()


def _owner() -> tuple[str, str]:
    s = get_settings()
    return s.ira_admin_username, s.ira_admin_password


async def _login(client, username: str, password: str):
    return await client.post("/auth/token", data={"username": username, "password": password})


async def _owner_headers(client) -> dict:
    r = await _login(client, *_owner())
    assert r.status_code == 200, f"owner login failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _grant_body(username: str) -> dict:
    return {"person_name": "Probe Person", "role": ROLE, "new_username": username,
            "new_password": PASSWORD, "owner_password": _owner()[1], "channel": "ui"}


async def _grant(client, headers, username: str):
    """Stage the grant; confirm it if it was staged. Returns the last response."""
    draft = await client.post("/api/v1/access/grant", json=_grant_body(username),
                              headers=headers)
    if draft.status_code != 200 or draft.json().get("status") != "confirmation_required":
        return draft
    return await client.post(
        "/api/v1/access/grant",
        json={**_grant_body(username), "confirm_token": draft.json()["token"]},
        headers=headers)


async def _seed_account_with_the_name(client, monkeypatch, headers) -> None:
    """An account that took the name before the rule existed.

    Granted through the real route with the name check stood down for the one
    call, so the rows are exactly the ones a grant writes. The check is back in
    force before anything is asserted.
    """
    from security import delegated

    with monkeypatch.context() as m:
        m.setattr(delegated, "validate_new_username", lambda u: (u or "").strip().lower())
        done = await _grant(client, headers, VOICE)
    assert done.status_code == 200, done.text
    assert done.json()["granted"]["username"] == VOICE


def _token_for(username: str) -> dict:
    """An access token as `/auth/token` mints one, for an account that may no
    longer log in: a token issued before the rule existed."""
    from api.middleware.auth import _make_access_token

    token, _jti, _exp = _make_access_token(username, ver=0)
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# New grants
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_voice_service_name_cannot_be_granted(client, admin):
    """**The defect.** On `main` this grant was staged, confirmed and created."""
    owner = await _owner_headers(client)

    r = await _grant(client, owner, VOICE)

    assert r.status_code == 422, f"{VOICE!r} was granted: {r.status_code} {r.text}"
    assert "voice service" in r.json()["detail"]
    assert await admin.fetchval(
        "SELECT count(*) FROM users WHERE id = $1", private_user_id(VOICE)) == 0


async def test_the_voice_service_name_is_refused_in_any_case(client, admin):
    owner = await _owner_headers(client)

    r = await _grant(client, owner, VOICE.upper())

    assert r.status_code == 422, r.text
    assert await admin.fetchval(
        "SELECT count(*) FROM users WHERE id = $1", private_user_id(VOICE)) == 0


async def test_an_ordinary_name_is_still_granted(client, admin):
    """Positive control: the refusal above is the name's doing."""
    owner = await _owner_headers(client)

    r = await _grant(client, owner, CONTROL)

    assert r.status_code == 200, r.text
    assert r.json()["granted"]["username"] == CONTROL
    assert (await _login(client, CONTROL, PASSWORD)).status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# An account that already holds the name
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_account_holding_the_name_cannot_log_in(client, admin, monkeypatch):
    """On `main` this login succeeded, and the token's `sub` was the voice
    service's."""
    owner = await _owner_headers(client)
    await _seed_account_with_the_name(client, monkeypatch, owner)

    r = await _login(client, VOICE, PASSWORD)

    assert r.status_code == 401, f"{VOICE!r} logged in: {r.status_code} {r.text}"


async def test_a_token_it_already_holds_reaches_no_chat(client, admin, monkeypatch):
    """Refusing login is not enough while a token issued earlier still works.

    With no role, the scope middleware refuses the account everywhere it maps,
    chat included, so the body's `is_voice_owner` is never read. On `main` the
    probe answered 200 with `owner_if_body_claims_it: true`.
    """
    owner = await _owner_headers(client)
    await _seed_account_with_the_name(client, monkeypatch, owner)
    from security import delegated, scope_enforcer
    scope_enforcer.invalidate_role_cache()

    r = await client.get("/api/v1/chat/probe", headers=_token_for(VOICE))
    assert r.status_code == 403, f"the account reached chat: {r.status_code} {r.text}"
    assert await delegated.get_delegated_role(VOICE) is None


async def test_the_owner_sees_it_flagged_and_can_revoke_it(client, admin, monkeypatch):
    """The account is never renamed silently. The owner is told and acts."""
    owner = await _owner_headers(client)
    await _seed_account_with_the_name(client, monkeypatch, owner)

    listed = await client.get("/api/v1/access/users", headers=owner)
    assert listed.status_code == 200, listed.text
    entry = next(u for u in listed.json()["users"] if u["username"] == VOICE)
    assert entry["reserved_name"] is True

    _, owner_password = _owner()
    body = {"user_id": entry["id"], "owner_password": owner_password, "channel": "ui"}
    draft = await client.post("/api/v1/access/revoke", json=body, headers=owner)
    assert draft.status_code == 200 and draft.json()["status"] == "confirmation_required", \
        draft.text
    done = await client.post("/api/v1/access/revoke",
                             json={**body, "confirm_token": draft.json()["token"]},
                             headers=owner)
    assert done.status_code == 200, done.text
    assert await admin.fetchval(
        "SELECT membership_grants_access FROM tenant_memberships WHERE user_id = $1",
        private_user_id(VOICE)) is False
