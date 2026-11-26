"""Signing out with the refresh token ends the device's session (cloud edition).

The phone app signs out by posting its refresh token to `/auth/logout`. Before
this, that blocklisted the token's jti and nothing else: `/auth/refresh` was
refused, but the session row the login had recorded stayed live, so the person's
session list kept showing a sign-in that no longer existed. Now the row is ended
too, with the reason "signed out", which is what the phone-emulator journey reads
back from `/auth/cloud/sessions` to show that a sign-out reached the server.

Real PostgreSQL; the cloud accounts and the auth-only ASGI app come from
`test_p2_cloud_login.py`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.tenancy.test_p2_cloud_login import (  # noqa: F401 - fixtures
    PASSWORD, _claims, _redis, client, cloud, email, pool,
)

asyncpg = pytest.importorskip("asyncpg")


async def _login(client):
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.post("/auth/cloud/login", data={"email": email("solo"), "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


async def _sessions(client, access: str) -> list[dict]:
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        r = await client.get("/auth/cloud/sessions", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    return r.json()["sessions"]


async def _logout(client, token: str):
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        return await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})


async def test_logout_with_the_refresh_token_ends_the_session_row(cloud, client):
    tokens = await _login(client)
    before = await _sessions(client, tokens["access_token"])
    assert len(before) == 1 and before[0]["revoked_at"] is None

    r = await _logout(client, tokens["refresh_token"])
    assert r.status_code == 200, r.text

    after = await _sessions(client, tokens["access_token"])
    assert len(after) == 1
    assert after[0]["revoked_at"] is not None
    assert after[0]["revoked_reason"] == "signed out"

    # The refresh path reads the row, so the token is refused there as well.
    with patch("utils.redis_client.get_redis", return_value=_redis()):
        refreshed = await client.post("/auth/refresh", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert refreshed.status_code == 401


async def test_negative_control_logout_with_the_access_token_leaves_the_session_live(cloud, client):
    """Signing out one access token is not ending the device's sign-in: the refresh token is
    still the device's credential and its row stays live (as before this change)."""
    from core.identity.resolver import resolve_principal
    from core.identity.sessions import list_sessions

    tokens = await _login(client)
    r = await _logout(client, tokens["access_token"])
    assert r.status_code == 200, r.text
    # The access token is now dead, so the rows are read from the store itself.
    after = await list_sessions(await resolve_principal(email("solo"), edition="cloud"))
    assert len(after) == 1 and after[0].revoked_at is None


async def test_negative_control_an_ended_session_is_not_ended_twice(cloud):
    """The write is conditional on the row still being live: the second call reports False
    and leaves the first reason in place."""
    from core.identity.resolver import resolve_principal
    from core.identity.sessions import list_sessions, record_session, revoke_session_by_jti
    import datetime as dt

    principal = await resolve_principal(email("solo"), edition="cloud")
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=7)
    await record_session(principal, refresh_jti="phone-journey-jti", expires_at=expires, ip_address="127.0.0.1", user_agent="test")
    assert await revoke_session_by_jti(principal, "phone-journey-jti", reason="first") is True
    assert await revoke_session_by_jti(principal, "phone-journey-jti", reason="second") is False
    assert await revoke_session_by_jti(principal, "no-such-jti") is False
    rows = [s for s in await list_sessions(principal) if s.revoked_reason in ("first", "second")]
    assert [s.revoked_reason for s in rows] == ["first"]
