"""Async PostgreSQL connection pool via asyncpg."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from config import get_settings

_pool: asyncpg.Pool | None = None


#: Pool settings that keep the process's memory flat (docs/evidence/soak/MEMORY_INVESTIGATION_ROUND9.md).
#:
#: asyncpg closes any connection idle for `max_inactive_connection_lifetime` seconds (its
#: default is 300) and does not keep `min_size` connections after that, so under a steady
#: load with bursts every few minutes (the care jobs run each minute and watches report
#: every few seconds) the pool shrank to one or two connections and then opened four or
#: five at once, about every ten minutes. Every reopening is a TLS handshake, a 256 KB
#: read buffer and a fresh statement cache; the closed connection's memory went back to
#: glibc, which kept it (bytes free but kept grew 3.9 to 11.7 MB in twenty minutes), and
#: resident memory stepped up 4 to 5 MB at each burst. With no idle lifetime a connection
#: is opened once and reused for the life of the process, bounded by `max_size`; asyncpg
#: still reconnects on acquire when a connection has been lost.
#:
#: Each connection's prepared statements expired after `max_cached_statement_lifetime`
#: seconds (default 300) and were re-prepared on next use; each expiry also left a small
#: reference cycle (the cache entry and its timer) for the collector, which was most of
#: what a forced full collection found. The server's SQL is a fixed set of statement
#: texts, so they are kept until the cache's LRU bound (`statement_cache_size`) evicts them.
POOL_MIN_SIZE = 5
POOL_MAX_SIZE = 20
POOL_MAX_INACTIVE_CONNECTION_LIFETIME = 0   # seconds; 0 = never close an idle connection
POOL_MAX_CACHED_STATEMENT_LIFETIME = 0      # seconds; 0 = a prepared statement never expires


def pool_settings() -> dict:
    """The keyword arguments the pool is created with (one place, so a test can check them)."""
    return {
        "min_size": POOL_MIN_SIZE,
        "max_size": POOL_MAX_SIZE,
        "command_timeout": 30,
        "max_inactive_connection_lifetime": POOL_MAX_INACTIVE_CONNECTION_LIFETIME,
        "max_cached_statement_lifetime": POOL_MAX_CACHED_STATEMENT_LIFETIME,
    }


async def init_pool() -> None:
    global _pool
    cfg = get_settings()
    _pool = await asyncpg.create_pool(dsn=cfg.database_dsn, **pool_settings())


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call init_pool() first.")
    return _pool


#: Seconds to wait for a free connection before giving up.
#:
#: asyncpg's `Pool.acquire()` has **no default timeout**. With every connection
#: busy, callers queue indefinitely — and a request waiting forever is an outage
#: that reports itself as slowness: no error rate moves, because nothing has
#: errored yet, while each waiter holds its own worker and task the whole time.
#: Saturation then compounds instead of settling.
#:
#: Ten seconds is longer than any healthy query here (`command_timeout` is 30s
#: for the query itself) and short enough that a saturated process sheds load
#: while something is still watching. Shedding is recoverable; queueing without
#: bound is not.
ACQUIRE_TIMEOUT_SECONDS = 10.0


class PoolSaturated(RuntimeError):
    """No connection became free in time. Deliberately distinct from a query error.

    A caller that cannot tell "the database rejected this" from "we never got a
    connection" will tune the wrong thing — the query, rather than the pool size
    or the concurrency upstream of it.
    """


@asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a connection, or fail. Never waits without bound.

    See `ACQUIRE_TIMEOUT_SECONDS` for why the timeout exists at all.
    """
    pool = get_pool()
    try:
        conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise PoolSaturated(
            f"no database connection became free within "
            f"{ACQUIRE_TIMEOUT_SECONDS}s (pool max_size={pool.get_max_size()}, "
            f"idle={pool.get_idle_size()}). Shedding this request rather than "
            f"queueing behind a saturated pool."
        ) from exc
    try:
        yield conn
    finally:
        await pool.release(conn)
