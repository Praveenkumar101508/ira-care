"""R08: "log out" (one device) survives losing Redis, in the private edition.

`docs/cloud-transformation/RELEASE_READINESS.md` R08 recorded it: `revoke_token`
wrote only to Redis in the private edition, and fail-soft. A token signed out
came back to life when Redis lost the key (a restart without persistence, a
flush, a restore), and a sign-out made while Redis was down was reported as done
and never happened. The cloud edition already recorded every sign-out in
PostgreSQL first (`f1_token_revocations`, migration 091). Both editions now do,
and the request path reads PostgreSQL, failing closed.

Real PostgreSQL and Redis, as `test_global_logout_durable.py`, whose fixture this
reuses: a private-edition owner account and the real app.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tests.tenancy.test_global_logout_durable import (  # noqa: F401 - fixtures
    _access_ok, _login, _refresh, acct, admin,
)

asyncpg = pytest.importorskip("asyncpg")

REVOKED = "ira:revoked:"


async def _logout(a, token):
    return await a.client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})


def _jti(token) -> str:
    from api.middleware import auth

    return auth.decode_token(token).jti


async def _rows(a, token) -> int:
    from core.identity.f1_revocation import digest

    return await a.admin.fetchval(
        "SELECT count(*) FROM f1_token_revocations WHERE token_hash = $1", digest(_jti(token)))


async def test_s1_a_signed_out_token_stays_out_after_its_redis_key_is_deleted(acct):
    t = await _login(acct)
    assert (await _logout(acct, t.access_token)).status_code == 200
    assert await _rows(acct, t.access_token) == 1
    await acct.cache.delete(REVOKED + _jti(t.access_token))
    assert not await _access_ok(t.access_token)


async def test_s2_and_after_redis_restarts_with_no_data(acct):
    t = await _login(acct)
    assert (await _logout(acct, t.access_token)).status_code == 200
    async for key in acct.cache.scan_iter(match=f"{REVOKED}*"):
        await acct.cache.delete(key)
    assert not await _access_ok(t.access_token)


async def test_s3_a_sign_out_made_while_redis_is_down_still_happens(acct, monkeypatch):
    from utils import redis_client

    t = await _login(acct)
    dead = redis_client._redis.__class__.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr(redis_client, "_redis", dead)
    r = await _logout(acct, t.access_token)
    assert r.status_code == 200, r.text
    assert await _rows(acct, t.access_token) == 1
    # Redis still down at the next request: PostgreSQL answers, and refuses.
    assert not await _access_ok(t.access_token)
    await dead.aclose()


async def test_s4_postgres_down_at_sign_out_is_a_503_never_logged_out(acct, monkeypatch):
    import utils.db as db

    t = await _login(acct)

    @asynccontextmanager
    async def unreachable():
        raise ConnectionRefusedError("synthetic PostgreSQL outage")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "acquire", unreachable)
    r = await _logout(acct, t.access_token)
    assert r.status_code == 503, r.text
    assert "Logged out" not in r.text


async def test_s5_an_unreadable_sign_out_record_is_a_503_never_a_pass(acct, monkeypatch):
    """Only the sign-out read fails; the version check still works. So the
    refusal has to come from this check, not from the version check behind it
    (a whole-PostgreSQL outage is refused by both; test 7 of
    test_global_logout_durable.py covers that)."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from api.middleware import auth
    from core.identity import f1_revocation

    t = await _login(acct)
    assert await _access_ok(t.access_token)

    async def unreadable(jti):
        raise ConnectionRefusedError("synthetic: the sign-out record is unreachable")

    monkeypatch.setattr(f1_revocation, "is_revoked", unreadable)
    with pytest.raises(HTTPException) as exc:
        await auth.require_auth(HTTPAuthorizationCredentials(scheme="Bearer",
                                                             credentials=t.access_token))
    assert exc.value.status_code == 503


async def test_s6_a_sign_out_recorded_only_in_redis_before_r08_is_carried_over(acct):
    """An install upgraded from before R08 holds its sign-outs only in Redis.
    The start-up seeder copies them, so losing Redis afterwards revives none."""
    from app.lifespan import _seed_token_versions

    t = await _login(acct)
    jti = _jti(t.access_token)
    await acct.cache.setex(REVOKED + jti, 600, "1")         # what the old code wrote
    assert await _rows(acct, t.access_token) == 0
    await _seed_token_versions()
    assert await _rows(acct, t.access_token) == 1
    await acct.cache.delete(REVOKED + jti)
    assert not await _access_ok(t.access_token)


async def test_s7_twenty_concurrent_sign_outs_of_one_token_record_it_once(acct):
    t = await _login(acct)
    results = await asyncio.gather(*[_logout(acct, t.access_token) for _ in range(20)])
    assert {r.status_code for r in results} == {200}
    assert await _rows(acct, t.access_token) == 1
    assert not await _access_ok(t.access_token)


async def test_s8_signing_out_one_device_leaves_the_others_signed_in(acct):
    one, other = await _login(acct), await _login(acct)
    assert (await _logout(acct, one.access_token)).status_code == 200
    assert not await _access_ok(one.access_token)
    assert await _access_ok(other.access_token)
    assert (await _refresh(acct, other.refresh_token)).status_code == 200
