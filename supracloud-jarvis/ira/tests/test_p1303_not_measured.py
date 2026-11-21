""""Not measured" is not "healthy", and it is not "down" either.

`PHASE_13_EXECUTION_PLAN.md` §3.3 asks for the first half:

> **Health and readiness distinguish "healthy" from "not measured".** The same
> rule Phase 10, 11 and 12 all landed on: an absent signal must not read as a
> good one.

Executing that claim rather than repeating it found readiness already correct —
a probe raising past its own guard produces a FAILED, readiness-blocking result
and `ready=False`. What it could not do was say **which** probe: `gather`'s
results were matched to names by position, and a raised one became a check
literally called `"unknown"`.

And it found the mirror-image error on `/health`, which no requirement had
named: a probe that could not run reported the dependency `down`. That is not an
absent signal read as a good one — it is an absent signal read as a *specific
bad one*, which sends an operator to look at a database that is fine.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


# ── Readiness ───────────────────────────────────────────────────────────────


@pytest.fixture
def readiness(monkeypatch):
    from core.runtime import readiness as r

    monkeypatch.setattr(r, "_cache", None, raising=False)
    yield r
    r._cache = None


def _ok(name):
    from core.runtime.readiness import CheckResult, CheckStatus

    async def _check(required: bool = True):
        return CheckResult(name, CheckStatus.OK, 1, required)

    return _check


def _all_probes_ok(monkeypatch, readiness, *, broken=None):
    """Every readiness probe replaced with a passing stub except `broken`."""
    names = ("postgres", "redis", "migrations", "database_role", "key_provider",
             "model_artefacts", "model_licences", "model_gateway")
    for name in names:
        if name == broken:
            async def _explode(required: bool = True):
                raise RuntimeError("a probe that raised past its own guard")
            monkeypatch.setattr(readiness, f"check_{name}", _explode)
        else:
            monkeypatch.setattr(readiness, f"check_{name}", _ok(name))
    return names


def test_a_probe_that_raises_is_reported_under_its_own_name(readiness, caplog,
                                                            monkeypatch):
    """It used to become a check called "unknown".

    Failing closed was already right and is unchanged. What was missing is
    which dependency went unmeasured — and "something you cannot name is
    broken" is a poor thing to hand an operator at three in the morning.
    """
    _all_probes_ok(monkeypatch, readiness, broken="redis")

    with caplog.at_level(logging.ERROR, logger="ira.readiness"):
        report = asyncio.run(readiness.evaluate_readiness(use_cache=False))

    assert report.ready is False, "a probe that raised did not block readiness"

    names = [c.name for c in report.checks]
    assert "unknown" not in names, names
    broken = [c for c in report.checks if c.name == "redis"][0]
    assert broken.status is readiness.CheckStatus.FAILED
    assert broken.blocks_readiness is True
    assert "RuntimeError" in broken.detail

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "'redis'" in messages or '"redis"' in messages
    assert "NOT measured" in messages


def test_an_unmeasured_optional_check_still_blocks(readiness, monkeypatch):
    """`model_gateway` is optional, and a probe that RAISED did not report it
    absent — it reported nothing at all.

    An optional dependency saying "I am not here" is a measurement. A probe that
    threw is the absence of one, and treating the two alike is how an unmeasured
    system reads as a healthy one.
    """
    _all_probes_ok(monkeypatch, readiness, broken="model_gateway")

    report = asyncio.run(readiness.evaluate_readiness(use_cache=False))

    gateway = [c for c in report.checks if c.name == "model_gateway"][0]
    assert gateway.blocks_readiness is True
    assert report.ready is False


def test_every_probe_reports_and_a_healthy_run_is_ready(readiness, monkeypatch,
                                                        caplog):
    """The control. Blocking on everything would satisfy the tests above."""
    names = _all_probes_ok(monkeypatch, readiness)

    with caplog.at_level(logging.ERROR, logger="ira.readiness"):
        report = asyncio.run(readiness.evaluate_readiness(use_cache=False))

    assert report.ready is True
    assert [c.name for c in report.checks] == list(names), (
        "the report's names or their order drifted from the probe list")
    assert not caplog.records


def test_the_names_match_the_probes_that_run(readiness, monkeypatch):
    """The name list and the coroutine list are one structure, not two.

    Two lists kept in step by hand is how a report starts labelling Redis's
    result "postgres" — silently, and in the direction nobody checks.

    It reads `_evaluate_uncached`, which is where the probe list lives after
    P13-04 split single-flight out of `evaluate_readiness`. That move broke
    this assertion, which is the honest cost of a source-reading test and the
    reason the behavioural assertions above it exist: those three did not
    notice the refactor, because the property they check did not change.
    """
    import inspect

    src = inspect.getsource(readiness._evaluate_uncached)
    assert "zip(probes, results)" in src
    for name in ("postgres", "redis", "migrations", "database_role",
                 "key_provider", "model_artefacts", "model_licences",
                 "model_gateway"):
        assert f'("{name}", check_{name}(' in src, name


# ── /health ─────────────────────────────────────────────────────────────────


def test_a_probe_that_cannot_run_is_unknown_not_down(caplog, monkeypatch):
    """`down` is a claim about the dependency; `unknown` is about the measurement.

    `get_pool()` raising "not initialised" is a wiring fault in THIS process.
    Reporting `down` sends an operator to look at a PostgreSQL that is fine —
    an absent signal read as a specific bad one.
    """
    from api.routes import health

    def uninitialised():
        raise RuntimeError("Database pool not initialised. Call init_pool() first.")

    monkeypatch.setattr(health, "get_pool", uninitialised)

    with caplog.at_level(logging.ERROR, logger="ira.health"):
        status = asyncio.run(health._check_postgres())

    assert status.status == "unknown", (
        "a probe that could not run reported the dependency as down")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "could NOT RUN" in messages


def test_a_dependency_that_answers_badly_is_still_down(monkeypatch):
    """The control, and the whole distinction.

    A connection error means the probe RAN and the dependency did not answer.
    That is `down`, and turning it into `unknown` would be the opposite defect:
    a real outage reported as a measurement problem.
    """
    from api.routes import health

    class _Pool:
        async def fetchval(self, *a, **k):
            raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(health, "get_pool", lambda: _Pool())
    assert asyncio.run(health._check_postgres()).status == "down"


def test_redis_draws_the_same_distinction(monkeypatch):
    from api.routes import health

    def uninitialised():
        raise RuntimeError("Redis not initialised. Call init_redis() first.")

    monkeypatch.setattr(health, "get_redis", uninitialised)
    assert asyncio.run(health._check_redis()).status == "unknown"

    class _Redis:
        async def ping(self):
            raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(health, "get_redis", lambda: _Redis())
    assert asyncio.run(health._check_redis()).status == "down"


def test_the_uninitialised_match_still_matches_the_real_messages():
    """The match is on a message, so it is asserted against the real ones.

    `utils.db.get_pool` and `utils.redis_client.get_redis` both raise a bare
    `RuntimeError`, so there is no type to switch on. If either message is
    reworded, this fails loudly instead of silently reverting the distinction
    above to "down".
    """
    import inspect

    from api.routes.health import _UNINITIALISED
    import utils.db
    import utils.redis_client

    for module in (utils.db, utils.redis_client):
        src = inspect.getsource(module)
        assert _UNINITIALISED in src.lower(), (
            f"{module.__name__} no longer contains {_UNINITIALISED!r}; the "
            f"health probe can no longer tell 'could not run' from 'down'")


def test_a_leaked_exception_normalises_to_unknown():
    """`_norm` sees whatever `gather(..., return_exceptions=True)` produced."""
    from api.routes.health import _norm

    out = _norm(RuntimeError("boom"))
    assert out["status"] == "unknown"
    assert "RuntimeError" in out["detail"]


def test_a_real_status_still_normalises_unchanged():
    """The control."""
    from api.routes.health import ServiceStatus, _norm

    out = _norm(ServiceStatus(status="ok", latency_ms=7))
    assert out == {"status": "ok", "latency_ms": 7}
