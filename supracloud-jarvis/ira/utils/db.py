"""Async PostgreSQL connection pool via asyncpg."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    cfg = get_settings()
    _pool = await asyncpg.create_pool(
        dsn=cfg.database_dsn,
        min_size=5,
        max_size=20,
        command_timeout=30,
        max_inactive_connection_lifetime=300,
    )


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
