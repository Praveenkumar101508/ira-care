"""P2-06 — a WebSocket must not outlive the credential that opened it.

Two sockets stream indefinitely: `/ws/notifications` forwards a tenant's
notification feed, and `/ws/brain` carries the deployment's own inner monologue.
Both authenticate like this:

    claims = decode_token(token)      # signature + expiry
    ...resolve a Principal...
    await websocket.accept()
    # …and then nothing, for as long as the socket stays open

`require_auth` — the dependency every HTTP route uses — does four things this
does not:

    1. refuses a **refresh** token for API access
    2. refuses a token whose `jti` is in the revocation list  (logout)
    3. refuses a token whose `ver` is below the current one   (logout/all)
    4. runs again on every single request

So the finding as recorded, "sessions are resolved at connect and never
re-validated", is the second half of the problem. The first half is that the
connect-time check was already weaker than the one every HTTP route gets: a
refresh token opened a socket, and so did a token that had been logged out
before the socket was ever created.

Which makes revocation not merely delayed but absent. `POST /auth/logout` and
`POST /auth/logout/all` are the two controls a person has when a token leaks,
and neither reached a socket at all — a stream opened with a stolen token kept
delivering someone's notification titles and bodies until the *attacker* chose
to disconnect.

── Why re-validation is a loop and not a one-off ───────────────────────────

An access token lives 30 minutes. A socket can live for days. Checking once at
connect would still leave a revoked credential streaming for the rest of the
socket's life, so the credential is re-checked on a timer and the socket is
closed when it dies — including when it simply expires, which is the common case
and not an attack at all.
"""
from __future__ import annotations

import asyncio
import os
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from contextlib import contextmanager

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


def _redis(*, version="0", revoked=0):
    """Redis, for the per-token (jti) check. The version is not read from it."""
    r = AsyncMock()
    r.get = AsyncMock(return_value=version)
    r.exists = AsyncMock(return_value=revoked)
    return r


@contextmanager
def _backend(*, version=0, revoked=0, redis=None):
    """Redis for the jti check, and the token-version store (PostgreSQL since
    migration 096, stood in by tests/token_version_store.py) holding `version`."""
    from tests.token_version_store import token_version_store

    with patch("utils.redis_client.get_redis", return_value=redis or _redis(revoked=revoked)), \
            token_version_store(version) as store:
        yield store


@pytest.fixture(autouse=True)
def _settings():
    from config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# The connect-time gate must be the same gate HTTP gets
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_access_token_is_authorised():
    """Positive control. A fix that refused everything would satisfy every
    negative assertion in this file."""
    from api.middleware.auth import _make_access_token, authorise_socket_token

    token, _, _ = _make_access_token("alice", ver=0)
    with _backend():
        payload = await authorise_socket_token(token)
    assert payload.sub == "alice"


@pytest.mark.asyncio
async def test_a_refresh_token_cannot_open_a_socket():
    """`require_auth` refuses these for API access. The sockets did not ask.

    A refresh token lives seven days rather than thirty minutes, so accepting
    one here turned the longest-lived credential IRA issues into a stream key.
    """
    from fastapi import HTTPException

    from api.middleware.auth import _make_refresh_token, authorise_socket_token

    token, _, _ = _make_refresh_token("alice", ver=0)
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        with pytest.raises(HTTPException) as exc:
            await authorise_socket_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_logged_out_token_cannot_open_a_socket():
    """`POST /auth/logout` puts the jti in the revocation list. The sockets
    never read it, so logging out did not stop a new socket being opened with
    the token you had just discarded."""
    from fastapi import HTTPException

    from api.middleware.auth import _make_access_token, authorise_socket_token

    token, _, _ = _make_access_token("alice", ver=0)
    with _backend(revoked=1):
        with pytest.raises(HTTPException) as exc:
            await authorise_socket_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_all_token_cannot_open_a_socket():
    """`POST /auth/logout/all` bumps the version. Same story."""
    from fastapi import HTTPException

    from api.middleware.auth import _make_access_token, authorise_socket_token

    token, _, _ = _make_access_token("alice", ver=0)
    with _backend(version=1):
        with pytest.raises(HTTPException) as exc:
            await authorise_socket_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_token_cannot_open_a_socket():
    import uuid

    from fastapi import HTTPException
    import jwt

    from api.middleware.auth import authorise_socket_token
    from config import get_settings

    expired = jwt.encode(
        {"sub": "alice", "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
         "jti": str(uuid.uuid4()), "tok": "access", "ver": 0},
        get_settings().ira_secret_key, algorithm="HS256")
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        with pytest.raises(HTTPException) as exc:
            await authorise_socket_token(expired)
    assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# …and it must keep being the gate, for as long as the socket lives
# ─────────────────────────────────────────────────────────────────────────────


class _Socket:
    """Enough of a WebSocket to observe whether it was closed, and why."""

    def __init__(self):
        self.closed_with: int | None = None

    async def close(self, code: int = 1000):
        self.closed_with = code


@pytest.mark.asyncio
async def test_the_watchdog_closes_a_socket_whose_token_is_revoked_mid_stream():
    """The finding itself. The socket was authorised; then the credential died."""
    from api.middleware.auth import (
        SOCKET_CREDENTIAL_REVOKED, _make_access_token, decode_token,
        watch_socket_credential,
    )

    token, _, _ = _make_access_token("alice", ver=0)
    payload = decode_token(token)
    sock = _Socket()

    with _backend() as store:          # healthy at first
        task = asyncio.create_task(
            watch_socket_credential(sock, payload, interval=0.01))
        await asyncio.sleep(0.05)
        assert sock.closed_with is None, "closed a socket whose token is still good"

        # Somebody calls /auth/logout/all: the stored version goes up.
        store["version"] = 1
        await asyncio.wait_for(task, timeout=2)

    assert sock.closed_with == SOCKET_CREDENTIAL_REVOKED, (
        "the socket kept streaming after the credential that opened it was "
        "revoked — /auth/logout/all does not reach an open socket")


@pytest.mark.asyncio
async def test_the_watchdog_closes_a_socket_whose_token_is_logged_out():
    from api.middleware.auth import (
        SOCKET_CREDENTIAL_REVOKED, _make_access_token, decode_token,
        watch_socket_credential,
    )

    token, _, _ = _make_access_token("alice", ver=0)
    payload = decode_token(token)
    sock = _Socket()

    live = _redis()
    with _backend(redis=live):
        task = asyncio.create_task(
            watch_socket_credential(sock, payload, interval=0.01))
        await asyncio.sleep(0.05)
        assert sock.closed_with is None
        live.exists = AsyncMock(return_value=1)      # POST /auth/logout
        await asyncio.wait_for(task, timeout=2)

    assert sock.closed_with == SOCKET_CREDENTIAL_REVOKED


@pytest.mark.asyncio
async def test_the_watchdog_closes_a_socket_whose_token_simply_expires():
    """Not an attack — the ordinary case. An access token lives 30 minutes and a
    socket can live for days, so expiry has to end the stream or the 30 minutes
    means nothing to anyone holding a socket."""
    import uuid

    import jwt

    from api.middleware.auth import (
        SOCKET_CREDENTIAL_REVOKED, TokenPayload, watch_socket_credential,
    )
    from config import get_settings

    # Decoded by hand: decode_token would refuse an already-expired token, and
    # the case under test is one that expires while the socket is open.
    soon = datetime.now(timezone.utc) + timedelta(milliseconds=30)
    jwt.encode({"sub": "alice", "exp": soon, "jti": str(uuid.uuid4()),
                "tok": "access", "ver": 0},
               get_settings().ira_secret_key, algorithm="HS256")
    payload = TokenPayload(sub="alice", exp=soon, jti=str(uuid.uuid4()),
                           tok="access", ver=0)

    sock = _Socket()
    with _backend():
        await asyncio.wait_for(
            watch_socket_credential(sock, payload, interval=0.01), timeout=2)

    assert sock.closed_with == SOCKET_CREDENTIAL_REVOKED, (
        "a socket outlived its own access token's expiry")


@pytest.mark.asyncio
async def test_the_watchdog_leaves_a_healthy_socket_alone():
    """Positive control for the three above. A watchdog that closed everything
    would pass all of them and break every real client."""
    from api.middleware.auth import (
        _make_access_token, decode_token, watch_socket_credential,
    )

    token, _, _ = _make_access_token("alice", ver=0)
    payload = decode_token(token)
    sock = _Socket()

    with _backend():
        task = asyncio.create_task(
            watch_socket_credential(sock, payload, interval=0.01))
        await asyncio.sleep(0.1)          # ~10 re-checks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sock.closed_with is None


@pytest.mark.asyncio
async def test_a_redis_outage_does_not_close_every_socket_at_once():
    """A Redis outage does not disconnect every connected client at once.

    Since migration 096 the version is read from PostgreSQL, so Redis serves
    only the per-token (jti) check, which `_auth_fail_closed()` governs and
    which fails open by default in the private edition. The tests below cover
    the store that now decides: one failed re-check keeps the socket, and two
    in a row close it.
    """
    from api.middleware.auth import (
        _make_access_token, decode_token, watch_socket_credential,
    )

    token, _, _ = _make_access_token("alice", ver=0)
    payload = decode_token(token)
    sock = _Socket()

    broken = AsyncMock()
    broken.get = AsyncMock(side_effect=ConnectionError("redis is down"))
    broken.exists = AsyncMock(side_effect=ConnectionError("redis is down"))

    with _backend(redis=broken):
        task = asyncio.create_task(
            watch_socket_credential(sock, payload, interval=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sock.closed_with is None


@pytest.mark.asyncio
async def test_one_failed_version_check_keeps_the_socket_and_two_in_a_row_close_it():
    """The watchdog no longer fails open (migration 096).

    A re-check that cannot reach the token-version store says nothing about the
    credential, so it is never counted as "still valid". One is tolerated, so a
    blip does not disconnect everyone; `max_errors` in a row close the socket.
    """
    from api.middleware.auth import (
        SOCKET_CREDENTIAL_REVOKED, _make_access_token, decode_token,
        watch_socket_credential,
    )
    from core.identity import token_versions

    payload = decode_token(_make_access_token("alice", ver=0)[0])
    failures = {"left": 0}
    real_current = None

    async def flaky(user_id):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise token_versions.TokenVersionUnavailable("synthetic blip")
        return await real_current(user_id)

    with _backend():
        real_current = token_versions.current
        with patch.object(token_versions, "current", flaky):
            sock = _Socket()
            failures["left"] = 1                      # one blip, then healthy
            task = asyncio.create_task(
                watch_socket_credential(sock, payload, interval=0.01, max_errors=2))
            await asyncio.sleep(0.1)
            assert sock.closed_with is None, "one failed re-check closed the socket"
            failures["left"] = 10**6                  # a lasting outage
            await asyncio.wait_for(task, timeout=2)
    assert sock.closed_with == SOCKET_CREDENTIAL_REVOKED, \
        "a socket kept streaming while its credential could not be checked"


async def test_an_unexpected_error_in_a_recheck_is_unknown_never_alive():
    """Not every failure arrives as the store's 503. Anything else a re-check
    raises (here, from the jti check) says nothing about the credential
    either, and must be counted as unknown, never as still valid."""
    from api.middleware import auth as auth_module
    from api.middleware.auth import (
        SOCKET_UNKNOWN, _make_access_token, _socket_credential_state, decode_token,
    )

    payload = decode_token(_make_access_token("alice", ver=0)[0])
    with _backend(), patch.object(auth_module, "_is_revoked",
                                  AsyncMock(side_effect=RuntimeError("synthetic"))):
        assert await _socket_credential_state(payload) == SOCKET_UNKNOWN


def test_the_watchdog_defaults_are_ten_seconds_and_two_errors(monkeypatch):
    from api.middleware import auth

    monkeypatch.delenv(auth.SOCKET_REVALIDATION_ENV, raising=False)
    monkeypatch.delenv(auth.SOCKET_REVALIDATION_ERRORS_ENV, raising=False)
    assert auth.socket_revalidation_seconds() == 10.0
    assert auth.socket_revalidation_errors() == 2
    monkeypatch.setenv(auth.SOCKET_REVALIDATION_ERRORS_ENV, "not-a-number")
    assert auth.socket_revalidation_errors() == 2


# ─────────────────────────────────────────────────────────────────────────────
# Both sockets, not just the one the finding named
# ─────────────────────────────────────────────────────────────────────────────

#: Every `@router.websocket` endpoint in the tree must go through the shared
#: gate. 3C-03 fixed `/ws/notifications` and 3C-05 then found the identical
#: defect in `/ws/brain`, one file over — fixing an instance is not fixing a
#: class, and this guard is the class.
SOCKET_MODULES = ["api/routes/notifications.py", "api/routes/brain.py"]


@pytest.mark.parametrize("rel", SOCKET_MODULES)
def test_each_socket_route_uses_the_shared_gate_and_starts_the_watchdog(rel):
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1].joinpath(rel).read_text(
        encoding="utf-8")

    assert "authorise_socket_token" in src, (
        f"{rel} authenticates its socket by hand instead of through the shared "
        f"gate, so it will drift from what require_auth enforces — which is "
        f"exactly how a refresh token came to open a stream")
    assert "watch_socket_credential" in src, (
        f"{rel} never re-checks the credential after accept(), so a logout "
        f"cannot reach an open socket")


def test_no_socket_route_is_missing_from_the_guard():
    """The list above is only worth having if it is complete.

    A new `@router.websocket` in a module nobody added here would inherit
    neither the gate nor the watchdog, silently.
    """
    import pathlib
    import re

    ira = pathlib.Path(__file__).resolve().parents[1]
    found = set()
    for path in sorted(ira.rglob("*.py")):
        rel = path.relative_to(ira).as_posix()
        if rel.startswith(("tests/", "scripts/", ".venv/")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"@\w+\.websocket\(", text):
            found.add(rel)

    missing = found - set(SOCKET_MODULES)
    assert not missing, (
        f"these modules declare a WebSocket route but are not covered by "
        f"SOCKET_MODULES: {sorted(missing)}. Add them — and make them use "
        f"authorise_socket_token and watch_socket_credential first.")


def test_the_guard_can_see_the_shape_it_forbids():
    """A regex that matched nothing would leave the guard permanently green."""
    import re

    assert re.search(r"@\w+\.websocket\(",
                     '@router.websocket("/ws/whatever")\nasync def f(): ...')
