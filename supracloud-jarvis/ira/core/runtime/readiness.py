"""Liveness and readiness — two different questions, deliberately separated.

The existing `/health` endpoint answers a third question ("how is everything?")
and answers it well for a human looking at a dashboard. It is the wrong probe
for an orchestrator, and using it as one causes a specific, well-known outage:

    /health reports "degraded" because Redis is briefly unavailable
    → the liveness probe fails
    → the orchestrator restarts every replica
    → the restarts hammer Redis, which was recovering
    → the outage that would have lasted 20 seconds lasts 20 minutes

So:

**Liveness** (`/livez`) asks *is this process alive and its event loop
responsive?* It must **not** check dependencies. A process whose database is
down is not broken — the database is. Restarting it fixes nothing and costs the
warm caches, the model that was loaded and the in-flight requests. Liveness
fails only when the process itself is unrecoverable.

**Readiness** (`/readyz`) asks *should this instance receive traffic right
now?* It checks every dependency required to serve a normal request, and it
**fails closed**: unknown state is not-ready. An instance that is not ready is
removed from the load balancer and put back when it recovers — no restart, no
lost state.

── Why readiness is cached ──────────────────────────────────────────────────

Readiness is polled continuously by every replica. Without a cache, N replicas
polling every second means N queries per second to PostgreSQL purely to ask
whether PostgreSQL is up — a probe that adds load proportional to fleet size,
which is exactly backwards. Results are cached for a short TTL.

Failures are cached for a *shorter* TTL than successes, so recovery is detected
quickly while steady-state cost stays low.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.runtime import timeouts

logger = logging.getLogger("ira.readiness")

#: Success cache. Short enough that a dependency failure is noticed within a
#: couple of probes, long enough that probing is not itself load.
READY_CACHE_TTL = 5.0
#: Failure cache — shorter, so recovery is seen quickly.
UNREADY_CACHE_TTL = 1.0


class CheckStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    latency_ms: int
    required: bool
    #: Safe, non-sensitive reason. Never an exception message — those carry
    #: DSNs, hostnames and occasionally credentials.
    detail: str = ""

    @property
    def blocks_readiness(self) -> bool:
        return self.required and self.status is CheckStatus.FAILED


@dataclass
class ReadinessReport:
    ready: bool
    checks: list[CheckResult] = field(default_factory=list)
    #: True while shutting down. A draining instance is never ready, even
    #: though every dependency is fine — the point is to stop new traffic.
    draining: bool = False

    def as_dict(self, *, verbose: bool = False) -> dict:
        body: dict = {"status": "ready" if self.ready else "not_ready"}
        if self.draining:
            body["draining"] = True
        if verbose:
            body["checks"] = [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "latency_ms": c.latency_ms,
                    "required": c.required,
                    **({"detail": c.detail} if c.detail else {}),
                }
                for c in self.checks
            ]
        else:
            failed = [c.name for c in self.checks if c.blocks_readiness]
            if failed:
                body["failed"] = failed
        return body


# ── Draining ─────────────────────────────────────────────────────────────────

_draining = False


def begin_draining() -> None:
    """Mark the instance not-ready. Called first thing on shutdown.

    Ordering is the point: an orchestrator needs a window between "stop sending
    traffic" and "process exits", or in-flight requests are cut. Setting this
    before any resource is closed gives the load balancer time to notice.
    """
    global _draining
    _draining = True


def stop_draining() -> None:
    global _draining
    _draining = False


def is_draining() -> bool:
    return _draining


# ── Individual checks ────────────────────────────────────────────────────────
#
# Each returns a CheckResult and never raises. A readiness probe that throws is
# a readiness probe that reports "unhealthy" for the wrong reason.

async def _timed(name: str, required: bool, coro, timeout: float) -> CheckResult:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        return CheckResult(name, CheckStatus.OK,
                           int((time.perf_counter() - started) * 1000), required)
    except asyncio.TimeoutError:
        return CheckResult(name, CheckStatus.FAILED,
                           int((time.perf_counter() - started) * 1000), required,
                           detail="timeout")
    except Exception as exc:  # noqa: BLE001
        # The exception *type* only. The message can contain a DSN.
        logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
        return CheckResult(name, CheckStatus.FAILED,
                           int((time.perf_counter() - started) * 1000), required,
                           detail=type(exc).__name__)


async def check_postgres(required: bool = True) -> CheckResult:
    async def _probe():
        from utils.db import get_pool
        await get_pool().fetchval("SELECT 1")
    return await _timed("postgres", required, _probe(), timeouts.DB_CONNECT_TIMEOUT)


async def check_redis(required: bool = True) -> CheckResult:
    async def _probe():
        from utils.redis_client import get_redis
        await get_redis().ping()
    return await _timed("redis", required, _probe(), timeouts.REDIS_CONNECT_TIMEOUT)


async def check_migrations(required: bool = True) -> CheckResult:
    """Verify the schema is at or beyond the level this code expects.

    An instance running new code against an un-migrated database will fail in
    request handlers rather than at startup, which is the worst place to find
    out. `023_tenancy.sql` created `tenants`, and every tenant-scoped query
    depends on the RLS it installed — so its presence is the cheapest proxy for
    "the tenancy layer exists".
    """
    async def _probe():
        from utils.db import get_pool
        from utils.migrations import migration_status

        pool = get_pool()
        exists = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='tenants')"
        )
        if not exists:
            raise RuntimeError("schema below required migration level")

        # The application no longer applies migrations (D-18), so "behind" is a
        # state this instance CANNOT fix by starting. Reporting not-ready is the
        # whole point: a rollout that skipped its migration step stalls loudly
        # here instead of serving requests against a schema its code does not
        # match. The count is reported, never the file contents.
        status = await migration_status(pool)
        if not status["up_to_date"]:
            raise RuntimeError(
                f"{len(status['pending'])} migration(s) pending — run the "
                f"migration deployment step; this process will not apply them"
            )
    return await _timed("migrations", required, _probe(), timeouts.DB_CONNECT_TIMEOUT)


async def check_database_role(required: bool = True) -> CheckResult:
    """The runtime's own database privileges are what it expects.

    A cluster restored from an old backup, a hand-provisioned staging box, or a
    rollback of `042_role_ownership_separation.sql` all produce a runtime that
    owns its tables again — and an owner can disable the row-level security
    every isolation claim rests on. That state is invisible from inside a
    request, so it is checked here.

    Only the failing check NAMES are published. Enumerating exactly which
    privileges the role holds through a health endpoint would tell an attacker
    which controls are already missing; the detail goes to the process log.
    """
    async def _probe():
        from core.runtime.db_identity import inspect
        from utils.db import get_pool

        report = await inspect(get_pool())
        if not report.ok:
            raise RuntimeError(report.summary())
    return await _timed("database_role", required, _probe(), timeouts.DB_CONNECT_TIMEOUT)


def _production_like() -> bool:
    """Staging and production. A development box has no allowlist table."""
    try:
        from core.runtime.environment import get_environment

        return bool(get_environment().is_production_like)
    except Exception:  # noqa: BLE001
        return False


async def check_model_artefacts(required: bool = True) -> CheckResult:
    """Every pinned model artefact is on the pull allowlist (P5-03, MOD-02).

    Not required in development, and the reason is not convenience: a developer
    who has not run the migration has no `model_pull_allowlist` table at all,
    and a readiness probe that failed for that would make the check the first
    thing anybody deleted. In a production-like environment it is required,
    because that is where an unallowlisted fetch actually reaches the internet.

    Why readiness rather than the loader: the loaders are synchronous and run
    lazily inside a thread. Checking here means a pin that has drifted from the
    allowlist stops the process from coming up, instead of failing on the first
    voice request three hours later.
    """
    async def _probe() -> None:
        from core.models.artefacts import verify_against_allowlist

        problems = await verify_against_allowlist()
        if problems:
            # The problems name identifiers and revisions — no secret material —
            # so they are safe to log and are the whole value of the check.
            logger.warning("model artefacts not allowlisted: %s", "; ".join(problems))
            raise RuntimeError(f"{len(problems)} model artefact(s) not allowlisted")

    return await _timed("model_artefacts", required, _probe(), 5.0)


async def check_model_licences(required: bool = True) -> CheckResult:
    """No model is serving without an approved licence (P6-03, MOD-01).

    Three mechanisms enforce this, and they fail in different ways:

      * `068`'s trigger refuses the WRITE that would make a version serve.
      * `require_approved_licence` refuses the REQUEST at resolution time.
      * this refuses to come up at all if the database is already in that state.

    The third is the one that catches drift the other two structurally cannot: a
    row that reached `serving` before `068` applied, a trigger somebody
    disabled with `ALTER TABLE ... DISABLE TRIGGER` (which changes one character
    in `pg_trigger` and nothing a schema diff would show), or a licence approval
    withdrawn while the withdrawal trigger was missing.

    Required in every environment, unlike `check_model_artefacts`. That check is
    development-optional because a developer who has not migrated has no
    allowlist table and a failing probe would be the first thing deleted. This
    one is written so the same situation passes: **no `model_registry` table
    means nothing is serving**, which is true, and is a pass rather than an
    error. What it will not do is pass because it could not tell.
    """
    async def _probe() -> None:
        from utils.db import acquire

        async with acquire() as conn:
            present = await conn.fetchval(
                "SELECT to_regclass('public.model_registry') IS NOT NULL "
                "   AND to_regclass('public.model_licence_register') IS NOT NULL")
            if not present:
                return          # nothing can be serving without the tables

            rows = await conn.fetch(
                "SELECT r.model_key, r.version, "
                "       COALESCE(l.classification, 'no licence row') AS state "
                "  FROM model_registry r "
                "  LEFT JOIN model_licence_register l ON l.id = r.licence_id "
                " WHERE r.status = 'serving' "
                "   AND (l.classification IS DISTINCT FROM 'known_approved')")

        if rows:
            # Model keys and classification names only — no licence text, no
            # secret material — so this is safe to log and is the whole value.
            detail = ", ".join(f"{r['model_key']}@{r['version']} ({r['state']})"
                               for r in rows)
            logger.warning("models serving without an approved licence: %s", detail)
            raise RuntimeError(
                f"{len(rows)} model version(s) serving without an approved "
                f"licence")

    return await _timed("model_licences", required, _probe(), 5.0)


async def check_model_gateway(required: bool = False) -> CheckResult:
    """At least one approved inference route is reachable.

    Not required by default. Whether inference is mandatory for readiness is a
    deployment decision: an API instance that serves memory, files and settings
    is useful with the model runtime down, and marking it not-ready would take
    the whole product offline because one subsystem is degraded. Set
    ``IRA_READINESS_REQUIRE_MODEL=true`` where inference *is* the service.
    """
    async def _probe():
        import httpx
        from config import get_settings
        cfg = get_settings()
        base = getattr(cfg, "ollama_base_url", "") or "http://localhost:11434/v1"
        root = base.removesuffix("/v1")
        async with httpx.AsyncClient(timeout=timeouts.OLLAMA_HEALTH_TIMEOUT) as client:
            r = await client.get(f"{root}/api/tags")
        if r.status_code != 200:
            raise RuntimeError("model runtime not ready")
    return await _timed("model_gateway", required, _probe(), timeouts.OLLAMA_HEALTH_TIMEOUT + 1)


async def check_key_provider(required: bool = True) -> CheckResult:
    """The key-encryption key can actually be obtained (P4-10).

    Required only when a MANAGED provider is selected. On the development
    provider the KEK is an environment variable, so there is nothing to be
    unavailable and a check would report health about a `getenv`.

    With `aws-kms` it is the opposite: the KEK arrives over the network, and an
    instance that cannot reach KMS cannot open a single second factor or voice
    template. It will accept requests and fail every authentication — which
    looks like a corrupted database rather than a missing dependency, and sends
    everyone in the wrong direction.

    The probe does a real DECRYPT. Permission to describe a key is not
    permission to use it, and a readiness check that passed while decryption was
    denied would be reporting the wrong thing.
    """
    async def _probe():
        from core.crypto import key_provider

        if key_provider.configured() != key_provider.AWS_KMS:
            return
        from core.crypto import aws_kms

        ok, reason = await asyncio.to_thread(aws_kms.health)
        if not ok:
            raise RuntimeError(reason)
    return await _timed("key_provider", required, _probe(),
                        timeouts.DB_CONNECT_TIMEOUT)


# ── Aggregate ────────────────────────────────────────────────────────────────

_cache: tuple[float, ReadinessReport] | None = None

#: P13-04 finding AA. The single evaluation currently in flight, with the loop
#: it belongs to. Concurrent callers on a cache miss await this instead of each
#: running the whole check set.
#:
#: The loop is stored because a task created in a previous event loop — which
#: happens across `asyncio.run` calls in tests, and could happen after a worker
#: restart — must never be awaited from a new one.
_inflight: tuple[Any, "asyncio.Task[ReadinessReport]"] | None = None


def _require_model() -> bool:
    import os
    return (os.getenv("IRA_READINESS_REQUIRE_MODEL") or "").strip().lower() in (
        "1", "true", "yes", "on")


async def evaluate_readiness(*, use_cache: bool = True) -> ReadinessReport:
    """Run every readiness check. Fails closed.

    P13-04 finding AA. The cache above de-duplicates probes in **time** and did
    nothing about them **concurrently**: every request that arrived during a
    miss ran the whole check set. Measured, with eight 50 ms checks and twenty
    simultaneous callers on a cold cache:

        full check runs executed : 160     (8 would be one evaluation)

    That is the exact load this module's own docstring says the cache exists to
    prevent — *"a probe that adds load proportional to fleet size, which is
    exactly backwards"* — and it is worst at the worst moment, because failures
    are cached for a **shorter** TTL than successes (1 s against 5 s). A
    struggling database therefore misses the cache five times as often, and
    each miss multiplies by however many probes are in flight.

    Single-flight fixes it: the first caller through a miss starts the
    evaluation and every other caller awaits **that same task**. No lock is
    needed — the check-and-set below has no `await` between the test and the
    assignment, so it is atomic on a single-threaded event loop.
    """
    if is_draining():
        # Not cached: draining must take effect on the very next probe.
        return ReadinessReport(ready=False, draining=True)

    now = time.monotonic()
    if use_cache and _cache is not None:
        cached_at, report = _cache
        ttl = READY_CACHE_TTL if report.ready else UNREADY_CACHE_TTL
        if now - cached_at < ttl:
            return report

    if not use_cache:
        # An explicit "measure it now" bypasses the shared flight as well as
        # the cache. A caller asking for a fresh evaluation and receiving one
        # that started before it asked would be a different kind of stale.
        return await _evaluate_uncached()

    global _inflight
    loop = asyncio.get_running_loop()
    if _inflight is not None:
        in_loop, task = _inflight
        if in_loop is not loop or task.done():
            _inflight = None
    if _inflight is None:
        _inflight = (loop, loop.create_task(_evaluate_uncached()))
    _loop, task = _inflight

    try:
        # `shield` so that one caller being cancelled — a client that hung up,
        # a probe timing out — does not cancel the evaluation every other
        # waiter is depending on.
        return await asyncio.shield(task)
    finally:
        if task.done() and _inflight is not None and _inflight[1] is task:
            _inflight = None


async def _evaluate_uncached() -> ReadinessReport:
    """One full pass over every check. The only place the cache is written."""
    global _cache

    now = time.monotonic()
    require_model = _require_model()
    # P13-03. Names live beside their coroutines rather than being implied by
    # gather's ordering, so a probe that raises can be reported under its OWN
    # name. It used to become a check called "unknown": the report failed
    # closed, correctly, but it could not say WHICH dependency had not been
    # measured — and "something you cannot name is broken" is a poor thing to
    # hand an operator at three in the morning.
    probes: tuple[tuple[str, Any], ...] = (
        ("postgres", check_postgres(required=True)),
        ("redis", check_redis(required=True)),
        ("migrations", check_migrations(required=True)),
        ("database_role", check_database_role(required=True)),
        ("key_provider", check_key_provider(required=True)),
        ("model_artefacts", check_model_artefacts(required=_production_like())),
        ("model_licences", check_model_licences(required=True)),
        ("model_gateway", check_model_gateway(required=require_model)),
    )
    results = await asyncio.gather(*(coro for _name, coro in probes),
                                   return_exceptions=True)

    checks: list[CheckResult] = []
    for (name, _coro), item in zip(probes, results):
        if isinstance(item, CheckResult):
            checks.append(item)
        else:
            # A check that raised despite its own guard: fail closed rather than
            # dropping it, or a broken probe would silently become "ready".
            #
            # `blocks_readiness=True` regardless of whether the check was
            # REQUIRED. A probe that raises has not reported "this optional
            # dependency is absent" — it has reported nothing at all, and an
            # unmeasured optional check is not the same as a passed one.
            logger.error(
                "readiness probe %r raised past its own guard (%s: %s) — this "
                "dependency was NOT measured, and readiness is refused rather "
                "than assumed", name, type(item).__name__, item)
            checks.append(CheckResult(
                name, CheckStatus.FAILED, 0, True,
                f"probe_error: {type(item).__name__}"))

    # P14-02. Every evaluation feeds the failure detector, which is what turns
    # this sequence of single-moment answers into a state something can act on.
    #
    # Here rather than in `evaluate_readiness` deliberately: this function is
    # the ONLY place a probe actually runs. Observing in the caller would count
    # cache hits and single-flight waiters as fresh observations, so a
    # dependency that failed once would be re-observed as failing by every
    # concurrent waiter and cross the threshold without three probes ever
    # happening. Detection would then fire on one failure while looking as
    # though it required three.
    #
    # SKIPPED is not observed at all. A check that was skipped produced no
    # evidence either way, and feeding it as a success is the false-clean shape
    # this phase exists to remove.
    from core.runtime.failure_detector import detector as _detector

    _fd = _detector()
    for check in checks:
        if check.status is CheckStatus.SKIPPED:
            continue
        _fd.observe(check.name, check.status is CheckStatus.OK, now=now)

    report = ReadinessReport(
        ready=not any(c.blocks_readiness for c in checks),
        checks=checks,
    )
    _cache = (now, report)
    return report


def reset_readiness_cache() -> None:
    """Drop the cache. For tests."""
    global _cache, _inflight
    _cache = None
    _inflight = None
