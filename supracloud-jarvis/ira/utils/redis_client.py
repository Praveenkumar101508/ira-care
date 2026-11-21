"""Async Redis client — caching, rate limiting, pub/sub.

── P14-03, FINDING AC: a hung Redis used to block forever ──────────────────

`socket_connect_timeout` bounds only the initial TCP connect. An **established**
connection to a Redis that stops answering — a failover, a network partition, an
instance blocked on a slow `BGSAVE` — had no deadline at all, so every awaiting
command waited indefinitely.

That is worse than it first sounds, because it is not merely slow: **it makes
every degraded-mode path in this codebase unreachable in the exact scenario
those paths were written for.** All of them are `except` blocks:

* `_check_expert_rate_limit`'s conservative limiter (P14-01b finding V)
* the fail-closed token-revocation posture in the cloud edition (Phase 9)
* every `cache_get` / `cache_set` caller

A *dead* Redis raises promptly — connection refused — and all of those work. A
*hung* Redis never raises, so the `except` never runs and the request hangs
instead of degrading. Readiness, meanwhile, IS bounded (`_timed` wraps its probe
in `asyncio.wait_for`), so `/readyz` would correctly report Redis down while
handlers hung: a system that looks diagnosable and is unusable.

`core/runtime/timeouts.py` had declared `REDIS_OPERATION_TIMEOUT = 5.0` since
Phase 13 and **nothing applied it.** The same module's `DB_POOL_ACQUIRE_TIMEOUT`
essay in `utils/db.py` makes the argument in full — *"a request waiting forever
is an outage that reports itself as slowness: no error rate moves, because
nothing has errored yet"* — and Redis simply never got the same treatment.

── Why there are two clients ───────────────────────────────────────────────

A socket timeout is correct for a command that should already have returned and
**wrong** for a subscription deliberately waiting for the next message.
`/ws/notifications` holds `pubsub.listen()` open for the life of a WebSocket; a
5-second socket timeout would tear it down every 5 seconds of quiet.

So the distinction is made explicit rather than split the difference:

* :func:`get_redis` — ordinary commands. Bounded by `REDIS_OPERATION_TIMEOUT`.
* :func:`get_pubsub_redis` — subscriptions only. No socket timeout, because an
  idle channel is not a stuck read. This is the same exemption
  `timeouts.API_STREAMING_TIMEOUT` already makes for streaming responses: *"a
  long generation is not a stuck request."*

The subscriber is not therefore unbounded in the way finding AC describes:
`health_check_interval` makes redis-py PING an idle connection, so a
black-holed subscriber is detected rather than waiting forever for a message
that can never arrive. Both clients set it.

**Do not merge these two back into one.** Doing so either re-opens finding AC or
breaks the notifications socket, depending on which timeout survives.
"""

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from config import get_settings
from core.runtime import timeouts

logger = logging.getLogger("ira.redis")

#: How often redis-py PINGs an otherwise idle connection. Bounds how long a
#: black-holed connection can look alive — for the subscriber, this is the only
#: thing that does.
HEALTH_CHECK_INTERVAL = 30

_redis: aioredis.Redis | None = None
_pubsub_redis: aioredis.Redis | None = None


async def init_redis() -> None:
    global _redis, _pubsub_redis
    cfg = get_settings()
    _redis = aioredis.from_url(
        cfg.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=timeouts.REDIS_CONNECT_TIMEOUT,
        # FINDING AC. Without this an established connection to a Redis that
        # stops answering blocks its caller forever, and every degraded-mode
        # `except` in this codebase becomes unreachable.
        socket_timeout=timeouts.REDIS_OPERATION_TIMEOUT,
        socket_keepalive=True,
        health_check_interval=HEALTH_CHECK_INTERVAL,
    )
    _pubsub_redis = aioredis.from_url(
        cfg.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=timeouts.REDIS_CONNECT_TIMEOUT,
        # Deliberately absent: see the module docstring. A subscription waiting
        # for the next message has not stalled.
        socket_keepalive=True,
        health_check_interval=HEALTH_CHECK_INTERVAL,
    )
    await _redis.ping()


async def close_redis() -> None:
    global _redis, _pubsub_redis
    if _redis:
        await _redis.aclose()
        _redis = None
    if _pubsub_redis:
        await _pubsub_redis.aclose()
        _pubsub_redis = None


def get_redis() -> aioredis.Redis:
    """The command client. Every operation is bounded — see FINDING AC."""
    if _redis is None:
        raise RuntimeError("Redis not initialised. Call init_redis() first.")
    return _redis


def get_pubsub_redis() -> aioredis.Redis:
    """The subscription client. Use ONLY for `pubsub()`.

    Falls back to the command client if this is somehow uninitialised — a
    subscriber that works and times out on idle is better than one that raises
    on startup, and the fallback is logged rather than silent.
    """
    if _pubsub_redis is None:
        if _redis is None:
            raise RuntimeError("Redis not initialised. Call init_redis() first.")
        logger.error(
            "the pub/sub Redis client is not initialised; falling back to the "
            "COMMAND client, whose %.1fs socket timeout will interrupt an idle "
            "subscription. Something initialised Redis without init_redis().",
            timeouts.REDIS_OPERATION_TIMEOUT)
        return _redis
    return _pubsub_redis


async def cache_get(key: str) -> Any | None:
    value = await get_redis().get(key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


async def cache_set(key: str, value: Any, ttl: int = 300) -> None:
    serialised = json.dumps(value) if not isinstance(value, str) else value
    await get_redis().setex(key, ttl, serialised)


async def cache_delete(key: str) -> None:
    await get_redis().delete(key)


async def publish_event(channel: str, payload: dict) -> None:
    """Publish a proactive event (security alert, briefing, etc.)."""
    await get_redis().publish(channel, json.dumps(payload))
