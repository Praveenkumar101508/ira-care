"""A delegated account stays inside its role: the holes the security pass found.

The private edition's household accounts (`security/delegated.py`) are limited by
`DelegatedScopeMiddleware`, by the routes' own gates, and by having no workspace
membership. The attack-style pass of 21 October 2026 found four ways past that,
each reproduced here against the real login routes, the real access router, the
real middleware and a migrated PostgreSQL, as `ira_app`:

  1. **Two spaces after "Bearer".** The middleware decodes `header[7:]`, which
     then starts with a space; the decode fails and the middleware lets the
     request through, assuming the route will refuse it. The route's own parser
     strips the space and accepts the token. Every route guarded only by
     `require_auth` is then open to any role.
  2. **The notifications socket.** `BaseHTTPMiddleware` passes WebSocket traffic
     straight through, so the middleware never sees `/ws/notifications`. The
     handler resolves any account, and in the private edition subscribes it to
     the one channel the owner's notifications go to.
  3. **Revoking an account leaves its tokens working.** Revocation suspends the
     membership but does not end the account's tokens, and the private
     edition's `/auth/refresh` does not ask whether the account is still active.
  4. **Another spelling of the name skips the second factor.** Login matches a
     household account case- and space-insensitively, then keys the lockout,
     the second-factor lookup and the token's `sub` on the name as typed.

Each attack test fails on `main` at 310e8c6; that run is its negative control.
The tests marked "the control" pass on both, so a failure is the defect and
not the harness.

What is replaced, as in `test_private_delegated_accounts.py`: the Redis-backed
lockout counter, `dev_mode_active` (forced off), Redis itself (a dictionary,
so a token-version bump is seen by the next check), and the Redis pub/sub
client of the socket, which yields one owner notification.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
httpx = pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.tenancy.test_private_delegated_accounts import (  # noqa: E402
    GRANTED, GRANTED_PASSWORD, _async_return, _grant, _login, _owner, _purge, _revoke,
)

#: What the owner is told and a household account must not hear.
OWNER_NOTE = {"category": "briefing", "title": "Morning briefing",
              "body": "owner-only: the 9am consultant appointment"}


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


class _DictRedis:
    """Enough of Redis for the token checks, with state: `incr` is seen by `get`."""

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

    async def exists(self, *keys):
        return sum(k in self.data for k in keys)

    async def delete(self, *keys):
        return sum(self.data.pop(k, None) is not None for k in keys)


class _FakePubSub:
    """One owner notification on whatever channel is subscribed."""

    def __init__(self, seen: list[str]):
        self.seen = seen

    async def subscribe(self, channel):
        self.seen.append(channel)

    async def unsubscribe(self, *_a):
        return None

    async def close(self):
        return None

    aclose = close

    async def listen(self):
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message", "data": json.dumps(OWNER_NOTE)}
        while True:
            await asyncio.sleep(3600)


@pytest.fixture
async def harness(monkeypatch, pool, admin):
    """The real auth routes, access router, notifications socket and scope
    middleware, with a probe guarded by `require_auth` alone."""
    from fastapi import Depends, FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from api.middleware.auth import require_auth
    from api.routes import notifications
    from api.routes.access import router as access_router
    from auth.routes import register_auth_routes
    from core.identity.provisioning import ensure_private_owner_identity
    from security import scope_enforcer

    monkeypatch.setattr("api.middleware.auth.dev_mode_active", lambda: False)
    store = _DictRedis()
    monkeypatch.setattr("utils.redis_client.get_redis", lambda: store)
    monkeypatch.setattr("utils.account_lockout.is_locked", _async_return(False))
    monkeypatch.setattr("utils.account_lockout.record_failure", _async_return((1, False)))
    monkeypatch.setattr("utils.account_lockout.clear_failures", _async_return(None))
    subscribed: list[str] = []
    fake = type("R", (), {"pubsub": lambda self: _FakePubSub(subscribed)})()
    monkeypatch.setattr(notifications, "get_pubsub_redis", lambda: fake)
    scope_enforcer.invalidate_role_cache()
    await ensure_private_owner_identity()

    app = FastAPI()
    limiter = Limiter(key_func=lambda _r: "dab", enabled=False)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    app.include_router(access_router, prefix="/api/v1")
    app.include_router(notifications.router)

    # Mapped to `settings_read`, which a trusted_user does not hold; guarded by
    # require_auth alone, as /api/v1/trust/status, /api/v1/notes and others are.
    async def _probe(username: str = Depends(require_auth)):
        return {"reached_by": username}

    app.add_api_route("/api/v1/trust/status", _probe, methods=["GET"])
    # Outside the middleware's paths: only the token itself decides here.
    app.add_api_route("/probe/whoami", _probe, methods=["GET"])
    app.add_middleware(scope_enforcer.DelegatedScopeMiddleware)
    app.state.redis = store

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dab") as c:
        yield app, c, subscribed
    scope_enforcer.invalidate_role_cache()


async def _owner_headers(client) -> dict:
    owner, password = _owner()
    r = await _login(client, owner, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _granted_tokens(client) -> tuple[dict, dict]:
    """Grant a trusted_user account through the owner's flow and log it in."""
    granted = (await _grant(client, await _owner_headers(client)))["granted"]
    r = await _login(client, GRANTED, GRANTED_PASSWORD)
    assert r.status_code == 200, r.text
    return granted, r.json()


async def _open_socket(app, path: str, token: str, timeout: float = 5.0) -> list[dict]:
    """Open a WebSocket on the ASGI app in this event loop and return what the
    server sent within `timeout`: an accept or a close, then any messages."""
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent: list[dict] = []
    proto = f"bearer.{token}"
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"dab"), (b"sec-websocket-protocol", proto.encode())],
             "subprotocols": [proto], "client": ("127.0.0.1", 50000), "server": ("dab", 80)}

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        await inbox.put({"type": "websocket.disconnect", "code": 1000})
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 - the socket's own teardown
            pass
    return sent


# ── 1. Two spaces after "Bearer" ─────────────────────────────────────────────


async def test_a_role_is_enforced_with_one_space_after_bearer(harness):
    """The control: the probe needs settings_read, which a trusted_user lacks."""
    _, client, _ = harness
    _, tokens = await _granted_tokens(client)
    r = await client.get("/api/v1/trust/status", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 403, r.text


async def test_a_role_is_enforced_with_two_spaces_after_bearer(harness):
    _, client, _ = harness
    _, tokens = await _granted_tokens(client)
    r = await client.get("/api/v1/trust/status", headers={"Authorization": f"Bearer  {tokens['access_token']}"})
    assert r.status_code == 403, f"{r.status_code}: two spaces walked past the role check: {r.text}"


# ── 2. The notifications socket ──────────────────────────────────────────────


async def test_the_owner_hears_their_notifications(harness):
    """The control: the socket works, and carries the owner's notification."""
    app, client, subscribed = harness
    owner, password = _owner()
    token = (await _login(client, owner, password)).json()["access_token"]
    sent = await _open_socket(app, "/ws/notifications", token)
    assert sent and sent[0]["type"] == "websocket.accept", sent
    assert any(OWNER_NOTE["body"] in (m.get("text") or "") for m in sent), sent


async def test_a_household_account_does_not_hear_the_owners_notifications(harness):
    app, client, subscribed = harness
    _, tokens = await _granted_tokens(client)
    sent = await _open_socket(app, "/ws/notifications", tokens["access_token"])
    leaked = [m for m in sent if OWNER_NOTE["body"] in (m.get("text") or "")]
    assert not leaked, f"a trusted_user received the owner's notification over {subscribed}: {leaked}"
    assert sent and sent[0]["type"] == "websocket.close", sent


# ── 3. Revoking an account ends its refresh token ────────────────────────────


async def test_a_revoked_accounts_refresh_token_is_refused(harness):
    _, client, _ = harness
    granted, tokens = await _granted_tokens(client)
    before = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert before.status_code == 200, f"the control: refresh works before revocation: {before.text}"

    await _revoke(client, await _owner_headers(client), granted["id"])

    after = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert after.status_code == 401, f"{after.status_code}: a revoked account minted a new token: {after.text}"


async def test_a_revoked_accounts_access_token_is_refused(harness):
    _, client, _ = harness
    granted, tokens = await _granted_tokens(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    before = await client.get("/probe/whoami", headers=headers)
    assert before.status_code == 200, f"the control: the token works before revocation: {before.text}"

    await _revoke(client, await _owner_headers(client), granted["id"])

    after = await client.get("/probe/whoami", headers=headers)
    assert after.status_code == 401, f"{after.status_code}: a revoked account's token still works: {after.text}"


async def test_a_revoked_account_stays_refused_when_redis_loses_its_counter(harness):
    """The token-version counter lives only in Redis (see
    docs/security/GLOBAL_LOGOUT_DEPENDS_ON_REDIS_2026-10-20.md). Revocation
    must not depend on it alone: the refresh asks whether the account is active."""
    app, client, _ = harness
    granted, tokens = await _granted_tokens(client)
    await _revoke(client, await _owner_headers(client), granted["id"])
    app.state.redis.data.clear()

    after = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert after.status_code == 401, f"{after.status_code}: a revoked account refreshed once Redis forgot: {after.text}"


# ── 4. Another spelling of the name skips the account's second factor ────────


async def _enrol_totp(admin, username: str) -> None:
    from core.identity.principal import PRIVATE_TENANT_ID, private_user_id

    await admin.execute("DELETE FROM totp_secrets WHERE username = $1", username)
    await admin.execute(
        "INSERT INTO totp_secrets (username, secret, enabled, user_id, tenant_id)"
        " VALUES ($1, $2, TRUE, $3, $4)",
        username, "JBSWY3DPEHPK3PXP", private_user_id(username), PRIVATE_TENANT_ID)


async def test_an_enrolled_account_needs_its_code(harness, admin):
    """The control: the account's own name, no code, is refused."""
    _, client, _ = harness
    await _granted_tokens(client)
    await _enrol_totp(admin, GRANTED)
    try:
        r = await _login(client, GRANTED, GRANTED_PASSWORD)
        assert r.status_code == 401, r.text
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username = $1", GRANTED)


@pytest.mark.parametrize("spelling", [GRANTED.upper(), f" {GRANTED}", f"{GRANTED} "],
                         ids=["upper-case", "leading-space", "trailing-space"])
async def test_another_spelling_of_the_name_still_needs_the_code(harness, admin, spelling):
    _, client, _ = harness
    await _granted_tokens(client)
    await _enrol_totp(admin, GRANTED)
    try:
        r = await _login(client, spelling, GRANTED_PASSWORD)
        assert r.status_code == 401, f"{r.status_code}: {spelling!r} signed in without the second factor"
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username = $1", GRANTED)


async def test_another_spelling_signs_in_as_the_account_itself(harness):
    """With no second factor enrolled, "  ALICE " is the account `alice`: its
    token carries the account's own name, the one revocation reaches."""
    from api.middleware.auth import decode_token

    _, client, _ = harness
    await _granted_tokens(client)
    r = await _login(client, f"  {GRANTED.upper()} ", GRANTED_PASSWORD)
    assert r.status_code == 200, r.text
    assert decode_token(r.json()["access_token"]).sub == GRANTED


async def test_every_spelling_counts_against_one_lockout(harness, monkeypatch):
    """Five wrong passwords lock an account. Counted per spelling, an attacker
    had five per spelling, and a name has as many spellings as letters allow."""
    _, client, _ = harness
    await _granted_tokens(client)
    keys: list[str] = []

    async def _record(username):
        keys.append(username)
        return 1, False

    monkeypatch.setattr("utils.account_lockout.record_failure", _record)
    for spelling in (GRANTED, GRANTED.upper(), f" {GRANTED}", f"{GRANTED} "):
        r = await _login(client, spelling, "not-the-password")
        assert r.status_code == 401, r.text
    assert keys == [GRANTED] * 4, keys
