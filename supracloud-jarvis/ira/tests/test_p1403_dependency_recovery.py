"""P14-03 — dependency loss and recovery, driven.

§3.3 of the plan: *"Recovery is exercised, not asserted. Kill the dependency,
drive the real path, prove it comes back."* And the P13-01 plant standard
applies to failure injection too — a recovery test that passes because the
failure never actually happened proves nothing, so every injection below is
first shown to have really occurred.

── What was already covered, and what was not ──────────────────────────────

`tests/readiness/test_concurrency_and_failover.py` drives a real PostgreSQL
failure: `pg_terminate_backend` against every connection, then a bounded retry
loop proving the pool comes back. It is honest about being gated behind
`IRA_TEST_ALLOW_DESTRUCTIVE` — terminating every backend puts PostgreSQL into
crash recovery and wedges anything sharing the database.

`tests/readiness/test_dependency_failures.py` pins the Redis **posture** — cloud
fails closed on revocation, private fails open — but never drives a loss and a
recovery.

So the database side was done and the Redis side was not, and re-deriving the
scope found something sharper than a missing test.

── FINDING AC — a hung Redis blocked forever, and that unreachable every fix ──

`init_redis` set `socket_connect_timeout` and no `socket_timeout`.
`socket_connect_timeout` bounds only the initial TCP connect, so an
**established** connection to a Redis that stops answering — a failover, a
partition, an instance blocked on a slow `BGSAVE` — had no deadline at all.

The consequence is not "some requests are slow". Every degraded-mode path in
this codebase is an `except` block:

* `_check_expert_rate_limit`'s conservative limiter — P14-01b finding V
* the cloud edition's fail-closed token revocation — Phase 9
* every `cache_get` / `cache_set` caller

A *dead* Redis raises promptly and all of them work. A *hung* Redis never
raises, so **none of them ever run**. P14-01b's degraded limiter is unreachable
in precisely the scenario it was written for.

And `core/runtime/timeouts.py` had declared `REDIS_OPERATION_TIMEOUT = 5.0`
since Phase 13 with nothing applying it — while `utils/db.py` wrote the argument
out in full for the database pool and got it right there.

── How the failure is injected ─────────────────────────────────────────────

With a real TCP server that accepts the connection and then never sends a byte.
Not a mock: the real `redis.asyncio` client, a real socket, a real blocked read.
A mocked `get_redis` raising `TimeoutError` would prove the `except` block runs
and would prove nothing about whether the exception ever arrives — which is the
entire finding.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


class _Blackhole:
    """A TCP server that accepts and then answers nothing, ever.

    This is what a hung Redis looks like from a client's side, and it is the
    only way to test finding AC honestly: the connect succeeds, so
    `socket_connect_timeout` is satisfied and irrelevant, and then the read
    blocks. Every layer below the assertion is real.
    """

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task] = set()
        self._writers: list = []
        self.port = 0
        self.connections = 0

    async def __aenter__(self) -> "_Blackhole":
        async def _handle(reader, writer):
            self.connections += 1
            self._writers.append(writer)
            self._handlers.add(asyncio.current_task())
            # Deliberately never write, never close. Hold the connection open
            # exactly as a wedged server does.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                writer.close()
                raise

        self._server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> bool:
        """Tear down deliberately, because the naive version hung CI.

        `await server.wait_closed()` alone passed on Python 3.11 and **hung
        forever on 3.12**, which is what CI runs. In 3.12 `wait_closed()` was
        changed to wait until every existing connection is closed — and this
        server's whole purpose is a handler parked on `asyncio.Event().wait()`
        holding a connection open, so it waits for something that by
        construction never happens.

        The suite ran green locally on 3.11 and wedged the `test` job for
        thirteen minutes until GitHub cancelled it. **An unbounded wait, in the
        fixture written to hunt unbounded waits.** So: cancel the handlers,
        close the sockets, and bound the wait anyway.
        """
        assert self._server is not None
        for writer in self._writers:
            writer.close()
        for task in self._handlers:
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=10.0)
        except asyncio.TimeoutError:
            # Reported rather than swallowed: this is the exact hang above, and
            # a fixture that hides it would hand the next person a mystery.
            raise AssertionError(
                "the blackhole server did not close within 10s — a handler is "
                "still holding a connection open. See this method's docstring.")
        return False

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"


async def _client_against(url: str, **kwargs):
    """A real redis-py asyncio client, built the way `init_redis` builds one."""
    import redis.asyncio as aioredis

    from core.runtime import timeouts

    return aioredis.from_url(
        url, encoding="utf-8", decode_responses=True,
        socket_connect_timeout=timeouts.REDIS_CONNECT_TIMEOUT,
        socket_keepalive=True, **kwargs)


# ── FINDING AC: the failure must actually arrive ────────────────────────────


async def test_a_hung_redis_raises_instead_of_blocking_forever():
    """The finding, driven against a real socket.

    Without `socket_timeout` this call never returns and the test hangs — which
    is why the assertion is wrapped in a deadline generous enough to be
    unambiguous: 15 s against a 5 s timeout. A hang here is the defect, not a
    slow machine.
    """
    from core.runtime import timeouts

    async with _Blackhole() as hung:
        client = await _client_against(
            hung.url, socket_timeout=timeouts.REDIS_OPERATION_TIMEOUT)
        try:
            started = asyncio.get_running_loop().time()
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.get("anything"), timeout=15.0)
            elapsed = asyncio.get_running_loop().time() - started
        finally:
            await client.aclose()

    assert hung.connections == 1, (
        "the client never connected, so nothing was injected and this test "
        "measured nothing")
    assert not isinstance(caught.value, asyncio.TimeoutError), (
        f"the 15 s guard fired rather than the client's own {timeouts.REDIS_OPERATION_TIMEOUT}s "
        f"socket timeout — the operation is still unbounded")
    assert elapsed < 12.0, (
        f"the command took {elapsed:.1f}s to give up against a "
        f"{timeouts.REDIS_OPERATION_TIMEOUT}s timeout")


async def test_without_the_timeout_the_same_call_does_not_return():
    """The negative control, and the one that makes the test above mean anything.

    If a client built WITHOUT `socket_timeout` also returned promptly, then
    something else — the event loop, the OS, redis-py's own defaults — would be
    bounding the read, and finding AC would not exist. This proves the blackhole
    genuinely blocks, so the fix above is what makes the difference.
    """
    async with _Blackhole() as hung:
        client = await _client_against(hung.url)   # no socket_timeout: as shipped
        try:
            # THIS is the assertion. `wait_for` raising means the client itself
            # never gave up: three seconds against a server that answers nothing
            # and the command was still waiting. That is finding AC.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(client.get("anything"), timeout=3.0)
        finally:
            # Cleanup asserts nothing. The first draft wrapped this in
            # `pytest.raises` on the assumption that closing a blackholed client
            # must also fail; it does not — `aclose` tears down the local socket
            # without needing a reply — and the test failed on its own teardown
            # while the property above had passed. A cleanup step that can fail
            # the test is a second test nobody wrote down.
            await asyncio.wait_for(client.aclose(), timeout=5.0)

    assert hung.connections == 1, "nothing connected, so nothing was injected"


async def test_the_shipped_client_applies_the_declared_timeout(monkeypatch):
    """`REDIS_OPERATION_TIMEOUT` was declared since Phase 13 and applied nowhere.

    Asserted against the connection kwargs `init_redis` actually builds, not by
    reading the source for the constant's name — a grep would pass on a
    docstring mentioning it.
    """
    from core.runtime import timeouts
    from utils import redis_client

    built: dict = {}

    def fake_from_url(url, **kwargs):
        built.setdefault("calls", []).append(kwargs)

        class _C:
            async def ping(self):
                return True

            async def aclose(self):
                return None

        return _C()

    monkeypatch.setattr(redis_client.aioredis, "from_url", fake_from_url)
    await redis_client.init_redis()
    try:
        command_kwargs, pubsub_kwargs = built["calls"]
        assert command_kwargs["socket_timeout"] == timeouts.REDIS_OPERATION_TIMEOUT, (
            "the command client does not apply the declared operation timeout")
        assert "socket_timeout" not in pubsub_kwargs, (
            "the pub/sub client carries a socket timeout, so an idle "
            "notifications channel will tear its WebSocket down every few "
            "seconds")
        assert pubsub_kwargs["health_check_interval"], (
            "the subscriber has neither a socket timeout nor a health check, "
            "so a black-holed subscription waits forever for a message that "
            "can never arrive")
    finally:
        await redis_client.close_redis()


def test_the_notifications_socket_uses_the_subscription_client():
    """The other half of the fix, and the thing a later "simplification" breaks.

    Merging the two clients back into one either re-opens finding AC or kills
    the notifications socket, depending on which timeout survives. This asserts
    the WebSocket handler takes the untimed one.
    """
    import inspect

    from api.routes import notifications

    source = inspect.getsource(notifications.ws_notifications)
    assert "get_pubsub_redis()" in source, (
        "the notifications WebSocket is back on the command client, whose "
        "socket timeout will interrupt a quiet channel")
    assert ".pubsub()" in source


# ── Recovery, driven end to end through the detector ────────────────────────


def _check(name, ok):
    from core.runtime.readiness import CheckResult, CheckStatus

    return CheckResult(name, CheckStatus.OK if ok else CheckStatus.FAILED,
                       1, True, "" if ok else "injected")


def test_a_dependency_walks_the_whole_recovery_path(monkeypatch):
    """HEALTHY → SUSPECTED → FAILED → RECOVERING → HEALTHY, driven.

    P14-02 built the detector and asserted its transitions in isolation. This
    drives them through **real readiness evaluations** — the same
    `_evaluate_uncached` a `/readyz` request runs — with an injected failure
    that is switched on and off mid-run.

    The point is not the state machine, which P14-02 covers. It is that a
    dependency going away and coming back is *observable end to end*, which is
    what makes a recovery something you can prove rather than assert.
    """
    from core.runtime import readiness
    from core.runtime.failure_detector import (DependencyHealth, detector,
                                               reset_detector)

    redis_up = {"ok": True}

    async def redis_probe(**_kw):
        return _check("redis", redis_up["ok"])

    async def other(name):
        async def _probe(**_kw):
            return _check(name, True)
        return _probe

    reset_detector()
    readiness.reset_readiness_cache()
    monkeypatch.setattr(readiness, "check_redis", redis_probe)
    for name in ("postgres", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        monkeypatch.setattr(readiness, f"check_{name}", asyncio.run(other(name)))

    def evaluate():
        return asyncio.run(readiness.evaluate_readiness(use_cache=False))

    def health():
        return detector().status("redis").health

    try:
        assert evaluate().ready is True
        assert health() is DependencyHealth.HEALTHY

        redis_up["ok"] = False
        assert evaluate().ready is False, (
            "readiness stayed ready with a required dependency injected down — "
            "the injection did not reach the real path")
        assert health() is DependencyHealth.SUSPECTED

        evaluate()
        evaluate()
        assert health() is DependencyHealth.FAILED
        assert detector().failing() == ["redis"]

        redis_up["ok"] = True
        assert evaluate().ready is True
        assert health() is DependencyHealth.RECOVERING, (
            "one successful evaluation moved a failed dependency straight back "
            "to healthy")

        evaluate()
        assert health() is DependencyHealth.HEALTHY
        assert detector().failing() == []

        outage = detector().status("redis").last_outage_ms
        assert outage is not None and outage >= 0, (
            "the recovery left no measured outage, so nothing can report on it "
            "afterwards")
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


def test_the_recovery_harness_would_notice_a_failure_that_never_happened(
        monkeypatch):
    """The P13-01 plant standard, applied to failure injection.

    A recovery test that passes because the dependency never went down is the
    failure-injection version of a plant that matched nothing. This runs the
    same harness with the injection DISABLED and asserts it does not report a
    recovery — so the test above cannot pass on a broken injector.
    """
    from core.runtime import readiness
    from core.runtime.failure_detector import (DependencyHealth, detector,
                                               reset_detector)

    async def always_up(**_kw):
        return _check("redis", True)

    async def other(name):
        async def _probe(**_kw):
            return _check(name, True)
        return _probe

    reset_detector()
    readiness.reset_readiness_cache()
    monkeypatch.setattr(readiness, "check_redis", always_up)
    for name in ("postgres", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        monkeypatch.setattr(readiness, f"check_{name}", asyncio.run(other(name)))

    try:
        for _ in range(5):
            asyncio.run(readiness.evaluate_readiness(use_cache=False))
        assert detector().status("redis").health is DependencyHealth.HEALTHY
        assert detector().status("redis").last_outage_ms is None, (
            "an outage was recorded without one being injected — the harness "
            "reports recoveries that did not happen")
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


# ── Pool saturation: the shed, and that it recovers ─────────────────────────


async def test_a_saturated_pool_sheds_rather_than_queueing():
    """`utils/db.py`'s argument, executed rather than read.

    *"A request waiting forever is an outage that reports itself as slowness:
    no error rate moves, because nothing has errored yet."* The module makes
    that case at length and sets `ACQUIRE_TIMEOUT_SECONDS`; nothing drove it.

    A fake pool is used deliberately. The property under test is `acquire`'s
    behaviour when nothing is free, and reproducing that against a real
    PostgreSQL means holding every connection open — which is the pattern that
    put the failover tests behind a destructive-only flag.
    """
    from utils import db

    class _NeverFree:
        """Behaves the way asyncpg's pool behaves, which is the whole point.

        The first draft slept past the deadline and then raised
        `AssertionError("unreachable")`, on the assumption that something
        outside would enforce the timeout. Nothing does: asyncpg implements
        `Pool.acquire(timeout=...)` INTERNALLY and raises `asyncio.TimeoutError`
        itself, which is exactly what `db.acquire` catches. So the draft failed
        and the failure said nothing about the product — a fake that does not
        match the thing it replaces tests the fake.
        """

        def get_max_size(self):
            return 20

        def get_idle_size(self):
            return 0

        async def acquire(self, timeout=None):
            await asyncio.sleep(timeout if timeout else 3600)
            raise asyncio.TimeoutError()

        async def release(self, conn):
            return None

    original, db._pool = db._pool, _NeverFree()
    original_timeout = db.ACQUIRE_TIMEOUT_SECONDS
    db.ACQUIRE_TIMEOUT_SECONDS = 0.25
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(db.PoolSaturated) as caught:
            async with db.acquire():
                pass
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        db.ACQUIRE_TIMEOUT_SECONDS = original_timeout
        db._pool = original

    assert elapsed < 3.0, (
        f"a saturated pool queued for {elapsed:.1f}s instead of shedding")
    assert "max_size=20" in str(caught.value), (
        "the refusal does not say what was saturated, so an operator tunes the "
        "query rather than the pool")


async def test_the_saturation_refusal_is_distinguishable_from_a_query_error():
    """`PoolSaturated` is not a generic error, and that is load-bearing.

    A caller that cannot tell "the database rejected this" from "we never got a
    connection" tunes the wrong thing. The class exists for that reason and its
    identity is asserted so a later refactor cannot collapse it into
    `RuntimeError` while the message still reads the same.
    """
    from utils.db import PoolSaturated

    assert issubclass(PoolSaturated, RuntimeError)
    assert PoolSaturated is not RuntimeError
    assert not issubclass(PoolSaturated, (asyncio.TimeoutError, ConnectionError))
