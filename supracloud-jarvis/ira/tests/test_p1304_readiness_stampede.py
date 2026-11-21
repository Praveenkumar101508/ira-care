"""P13-04 finding AA — the readiness cache de-duplicated in time, not concurrently.

Found by **driving the HTTP API**, which is what P13-04 is for.
`scripts/staging/p13_http_load.py` measured `/readyz` at 1000 requests and
concurrency 10, three times:

    p50 ≈  9.5 ms      p95 ≈ 13–38 ms      p99 ≈ 349–586 ms

A p99 sixty times the median, reproducibly, on a path whose whole design is a
cache. The cache expires, and every request in flight at that moment runs the
entire check set — measured with eight 50 ms checks and twenty simultaneous
callers on a cold cache:

    full check runs executed : 160        (8 would be one evaluation)

That is precisely the load the module's own docstring says the cache exists to
prevent: *"a probe that adds load proportional to fleet size, which is exactly
backwards."* And it is worst at the worst possible moment — failures are cached
for 1 s against a success's 5 s, so a struggling database misses the cache five
times as often and every miss multiplies by the probes in flight.

The module opens by describing an outage in which a probe amplifies the failure
it is reporting. This is the same shape by a different mechanism, in the code
written to avoid it.
"""
from __future__ import annotations

import asyncio
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

CHECKS = ("postgres", "redis", "migrations", "database_role", "key_provider",
          "model_artefacts", "model_licences", "model_gateway")


@pytest.fixture
def readiness():
    from core.runtime import readiness as r

    r.reset_readiness_cache()
    yield r
    r.reset_readiness_cache()


def _counting_checks(monkeypatch, readiness, *, delay=0.02, ready=True):
    """Replace all eight checks with counting stubs. Returns the counter."""
    from core.runtime.readiness import CheckResult, CheckStatus

    calls = {"n": 0}
    status = CheckStatus.OK if ready else CheckStatus.FAILED

    for name in CHECKS:
        async def _check(required: bool = True, _name=name):
            calls["n"] += 1
            await asyncio.sleep(delay)
            return CheckResult(_name, status, int(delay * 1000), required)

        monkeypatch.setattr(readiness, f"check_{name}", _check)
    return calls


def test_concurrent_callers_share_one_evaluation(readiness, monkeypatch):
    """Twenty simultaneous probes on a cold cache must run the checks ONCE.

    Before: 160 runs — every caller ran all eight. The number matters more than
    the ratio: this is per-replica, so a fleet multiplies it again.
    """
    calls = _counting_checks(monkeypatch, readiness)

    async def main():
        return await asyncio.gather(
            *(readiness.evaluate_readiness() for _ in range(20)))

    reports = asyncio.run(main())

    assert calls["n"] == len(CHECKS), (
        f"{calls['n']} check runs for one cold-cache moment; {len(CHECKS)} is "
        f"a single evaluation. Every caller that arrives during a miss is "
        f"running the whole set.")
    assert len({id(r) for r in reports}) == 1, (
        "callers got different report objects, so they did not share a flight")
    assert all(r.ready for r in reports)


def test_the_cache_still_serves_the_second_wave(readiness, monkeypatch):
    """The control. Single-flight must not have replaced the cache."""
    calls = _counting_checks(monkeypatch, readiness)

    async def main():
        await asyncio.gather(*(readiness.evaluate_readiness() for _ in range(5)))
        first = calls["n"]
        await asyncio.gather(*(readiness.evaluate_readiness() for _ in range(5)))
        return first, calls["n"]

    first, total = asyncio.run(main())
    assert first == len(CHECKS)
    assert total == first, "the warm cache re-ran the checks"


def test_use_cache_false_still_measures_now(readiness, monkeypatch):
    """An explicit "measure it now" must not be served a shared flight.

    A caller asking for a fresh evaluation and receiving one that started
    before it asked is a different kind of stale, and `use_cache=False` is what
    the P13-03 tests use to force a real run.
    """
    calls = _counting_checks(monkeypatch, readiness)

    async def main():
        await asyncio.gather(
            *(readiness.evaluate_readiness(use_cache=False) for _ in range(3)))

    asyncio.run(main())
    assert calls["n"] == 3 * len(CHECKS), (
        "use_cache=False was served a shared or cached result")


def test_a_cancelled_waiter_does_not_cancel_the_flight(readiness, monkeypatch):
    """A client that hangs up must not take the evaluation with it.

    Without `shield`, the first caller's cancellation propagates into the task
    every other waiter is awaiting — so one abandoned probe turns into a failed
    probe for everybody, which on a readiness endpoint means an instance pulled
    out of the load balancer for no reason.
    """
    calls = _counting_checks(monkeypatch, readiness, delay=0.05)

    async def main():
        doomed = asyncio.create_task(readiness.evaluate_readiness())
        others = [asyncio.create_task(readiness.evaluate_readiness())
                  for _ in range(3)]
        await asyncio.sleep(0.01)          # let them all join the same flight
        doomed.cancel()
        results = await asyncio.gather(*others, return_exceptions=True)
        return results

    results = asyncio.run(main())

    assert all(not isinstance(r, BaseException) for r in results), (
        f"one caller cancelling took the shared evaluation down with it: "
        f"{results}")
    assert calls["n"] == len(CHECKS)


def test_a_failing_run_is_not_cached_as_a_flight_forever(readiness, monkeypatch):
    """After a miss completes, the next miss must start a NEW evaluation.

    A stale finished task left in `_inflight` would pin the first result
    permanently — a readiness endpoint frozen at whatever it said once, which
    is the worst possible way for this fix to fail.
    """
    calls = _counting_checks(monkeypatch, readiness, delay=0.0, ready=False)

    async def main():
        await readiness.evaluate_readiness()
        first = calls["n"]
        readiness._cache = None            # force the next call to miss
        await readiness.evaluate_readiness()
        return first, calls["n"]

    first, total = asyncio.run(main())
    assert first == len(CHECKS)
    assert total == 2 * len(CHECKS), (
        "the second miss reused a finished flight instead of evaluating again")


def test_a_flight_from_a_dead_loop_is_not_awaited(readiness, monkeypatch):
    """Tests, and worker restarts, produce more than one event loop.

    A task created in a loop that has closed can never complete, and awaiting
    it from a new loop raises. Two `asyncio.run` calls is the cheapest way to
    prove the guard, and it is not a synthetic case — every test file in this
    suite does it.
    """
    calls = _counting_checks(monkeypatch, readiness, delay=0.01)

    async def one():
        return await readiness.evaluate_readiness()

    asyncio.run(one())
    readiness._cache = None
    report = asyncio.run(one())            # a different loop entirely

    assert report is not None
    assert calls["n"] == 2 * len(CHECKS)


def test_draining_still_short_circuits(readiness, monkeypatch):
    """Draining must take effect on the very next probe — before the cache and
    before the shared flight."""
    calls = _counting_checks(monkeypatch, readiness)
    readiness.begin_draining()
    try:
        report = asyncio.run(readiness.evaluate_readiness())
    finally:
        readiness.stop_draining()

    assert report.draining is True
    assert report.ready is False
    assert calls["n"] == 0, "a draining instance ran its dependency probes"
