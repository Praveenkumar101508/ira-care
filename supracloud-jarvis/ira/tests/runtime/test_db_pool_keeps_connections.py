"""The database pool keeps its connections and their prepared statements.

Found by the wall-clock soak's memory criterion (docs/evidence/soak/MEMORY_INVESTIGATION_ROUND9.md):
asyncpg closed every connection idle for 300 s and did not keep `min_size` after
that, so the pool shrank between bursts and reopened four or five connections at
once about every ten minutes. Each reopening cost a TLS handshake, a 256 KB read
buffer and a fresh statement cache, the closed connection's memory stayed with
glibc as free-but-kept bytes, and the process's resident memory stepped up 4 to
5 MB per burst. Prepared statements likewise expired after 300 s and were
re-prepared, leaving a reference cycle per expiry.

The first test checks the settings the pool is created with; the two database
tests show what the settings do with a real pool: an idle connection is kept
(same server process on the next acquire) and a statement, once prepared, stays
cached. Negative control: with `max_inactive_connection_lifetime=300` and
`max_cached_statement_lifetime=300` restored in `utils/db.py`,
`test_pool_is_created_with_no_idle_or_statement_expiry` fails; the
`_control_short_lifetime_closes_the_idle_connection` test shows the mechanism the
setting removes (a short lifetime: a different server process after the idle).
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")


def test_pool_is_created_with_no_idle_or_statement_expiry(monkeypatch):
    from utils import db

    seen: dict = {}

    class FakePool:
        pass

    async def fake_create_pool(**kwargs):
        seen.update(kwargs)
        return FakePool()

    class Cfg:
        database_dsn = "postgresql://ira_app:x@localhost:5432/example"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(db, "get_settings", lambda: Cfg())
    monkeypatch.setattr(db, "_pool", None)
    asyncio.run(db.init_pool())
    assert seen["dsn"] == Cfg.database_dsn
    assert seen["max_inactive_connection_lifetime"] == 0, "an idle connection must never be closed by a timer"
    assert seen["max_cached_statement_lifetime"] == 0, "a prepared statement must never expire by a timer"
    assert seen["min_size"] == 5 and seen["max_size"] == 20
    assert seen == {"dsn": Cfg.database_dsn, **db.pool_settings()}


@pytest.mark.skipif(not DSN, reason="IRA_TEST_DATABASE_DSN not set")
def test_an_idle_connection_is_kept_and_its_statements_stay_prepared():
    import asyncpg

    from utils import db

    async def run():
        settings = {**db.pool_settings(), "min_size": 1, "max_size": 1}
        pool = await asyncpg.create_pool(dsn=DSN, **settings)
        try:
            async with pool.acquire() as con:
                pid = await con.fetchval("SELECT pg_backend_pid()")
                await con.fetchval("SELECT $1::int + 1", 1)
                cached = con._con._stmt_cache.iter_statements()  # noqa: SLF001 - the cache under test
                assert any("pg_backend_pid" in s.query for s in cached)
            await asyncio.sleep(1.5)   # longer than the control's lifetime below
            async with pool.acquire() as con:
                assert await con.fetchval("SELECT pg_backend_pid()") == pid, "the idle connection was replaced"
                cache = con._con._stmt_cache  # noqa: SLF001
                assert cache._max_lifetime == 0
                assert any("pg_backend_pid" in s.query for s in cache.iter_statements()), "the statement expired"
        finally:
            await pool.close()

    asyncio.run(run())


@pytest.mark.skipif(not DSN, reason="IRA_TEST_DATABASE_DSN not set")
def test_control_short_lifetime_closes_the_idle_connection():
    """The mechanism the setting removes: with an idle lifetime, the next acquire
    after the idle is a new server process (a reconnect), and the statement
    cache starts empty."""
    import asyncpg

    async def run():
        pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=1, max_inactive_connection_lifetime=0.3,
                                         max_cached_statement_lifetime=0.3)
        try:
            async with pool.acquire() as con:
                pid = await con.fetchval("SELECT pg_backend_pid()")
            await asyncio.sleep(1.5)
            async with pool.acquire() as con:
                assert await con.fetchval("SELECT pg_backend_pid()") != pid
                assert con._con._stmt_cache._max_lifetime == 0.3  # noqa: SLF001
        finally:
            await pool.close()

    asyncio.run(run())
