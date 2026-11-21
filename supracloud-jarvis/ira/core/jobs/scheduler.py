"""The thing that actually runs retention (Phase 4 batch 11).

P4-06 made retention correct and gave it an HTTP caller. A user's retention
window still only applied when somebody pressed a button, which is not a
retention policy — it is a retention feature.

This is the caller that runs without being asked.

── Why PostgreSQL and not Celery, APScheduler or a cron container ─────────

Every one of those is a new runtime dependency to deploy, monitor and fail. The
database is already required, already highly available in any deployment that
has one, and already the thing the job writes to.

`pg_try_advisory_lock` gives mutual exclusion across an entire fleet in one
round trip, and releases automatically when the holding connection dies — which
is the exact behaviour a scheduler lock needs, and the part that hand-rolled
"UPDATE a lock row" implementations get wrong when a process is killed.

── The four properties that make it safe to run unattended ────────────────

**One at a time, fleet-wide.** Every instance ticks. Only the one that takes the
advisory lock does the work; the rest record `skipped` and move on. Two
instances expiring the same rows would not corrupt anything — the work is
idempotent — but it would double the load and make the run history a lie.

**Idempotent.** Retention deletes rows past a window. Running it twice deletes
nothing the second time. This is what makes crash recovery trivial: there is no
resume protocol, because re-running from the start is correct.

**Bounded.** A tenant whose pass raises does not stop the others; the failure is
recorded against that tenant and the loop continues. The alternative — one bad
tenant stopping the fleet's retention indefinitely — is how a compliance
obligation quietly stops being met.

**Observed.** Every tick writes a `job_runs` row before it does anything, and
finishes it afterwards. A row with no `finished_at` is a pass that died, and the
next start says so rather than leaving an absence to be interpreted.

── Enumeration is tenant-BOUND, one tenant at a time ──────────────────────

Retention runs per (tenant, user, workspace), so something has to enumerate
them. The obvious shape — one unbound query across `retention_settings` — is
wrong twice over: that table is FORCE RLS keyed on `app.tenant_id`, so an
unbound connection matches **zero rows silently** (the P2-05 shape), and a
connection that could see every tenant's rows at once is the authority boundary
Phase 2 spent eleven batches establishing.

So the enumeration is two steps:

  1. `tenants` carries no RLS — it is the registry, not tenant-scoped data — so
     the list of tenant ids is read on an ordinary connection.
  2. For each id, `scheduler_connection` binds `app.tenant_id` — and only
     that — so the tenant's settings are read **inside its own boundary**
     while every user-scoped policy still matches nothing.

The first draft of this module used `owner_connection`, which binds the PRIVATE
tenant and is forbidden for cloud-scoped data. It appeared to work because the
test harness ran as a superuser, so RLS was not enforced at all and the query
saw every tenant. Two defects, each hiding the other. The tests now run as the
application role for exactly this reason.

Each tenant's actual deletion then runs through `workspace_connection`, bound to
that principal, so the work is scoped by RLS exactly as when a user triggers it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ira.jobs.scheduler")

#: Job name. Recorded in `job_runs.job` and hashed into the advisory lock key,
#: so renaming it would let an old and a new deployment run concurrently.
RETENTION_JOB = "retention"

#: How often each instance wakes. Not how often the work happens — that is
#: decided by the lock and by `MIN_INTERVAL_SECONDS`.
TICK_ENV = "IRA_SCHEDULER_TICK_SECONDS"
DEFAULT_TICK = 300.0

#: The floor between two real passes, fleet-wide. Enforced from `job_runs`
#: rather than from a timer, because a timer is per process and a restart loop
#: would otherwise run retention continuously.
INTERVAL_ENV = "IRA_RETENTION_INTERVAL_SECONDS"
DEFAULT_INTERVAL = 86400.0

ENABLED_ENV = "IRA_SCHEDULER_ENABLED"

#: Whether the API process ALSO runs the loop. A dedicated worker sets this
#: false so the API does not compete with it.
#:
#: Default true, and deliberately so: today the API's lifespan is the only place
#: the loop is started, so defaulting to false would quietly stop retention
#: everywhere the moment this shipped. The flag exists to let a deployment that
#: HAS a worker say so, not to change what a deployment without one does.
IN_API_ENV = "IRA_SCHEDULER_IN_API"

#: A run still unfinished after this is treated as dead. Generous: a slow pass
#: over many tenants is not a crash, and declaring one prematurely would let a
#: second instance start while the first is still deleting.
STALE_AFTER_SECONDS = 3600.0


def _runner_id() -> str:
    """Which process claimed the run. `host/pid` — an operator chasing a stuck
    pass needs somewhere to look, and "the scheduler" is not an answer."""
    return f"{socket.gethostname()}/{os.getpid()}"


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return value if value > 0 else default


def enabled() -> bool:
    """Off unless switched on.

    A background job that starts by default would begin deleting data the first
    time somebody runs the application locally against a copy of production —
    which is a thing people do.
    """
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def enabled_in_api() -> bool:
    """Should the API process run the loop itself?

    Two processes running it is *safe* — the advisory lock lets exactly one pass
    proceed and the other logs "another instance holds the retention lock". What
    it is not is *provable*: with both running, a `job_runs` row may carry the
    API's pid or the worker's, so "the worker did the work" stops being a claim
    the evidence supports. Staging found this the honest way — a run recorded
    against the uvicorn pid while a dedicated worker was up.
    """
    if not enabled():
        return False
    raw = (os.getenv(IN_API_ENV) or "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


@dataclass
class RunOutcome:
    """What one pass did. Mirrors the `job_runs` row it produces."""

    run_id: Optional[uuid.UUID] = None
    status: str = "skipped"
    tenants_seen: int = 0
    items_removed: int = 0
    failures: dict[str, str] = field(default_factory=dict)
    #: Whether the interval check was skipped. Recorded on the row so the
    #: history can distinguish a broken schedule from an operator pressing
    #: "run a pass now".
    forced: bool = False
    detail: dict[str, dict] = field(default_factory=dict)


async def recover_interrupted_runs() -> int:
    """Close out runs that died, at startup. Returns how many.

    Called before the first tick. A `running` row with no `finished_at` after a
    restart means the process holding it is gone — the advisory lock died with
    its connection, so nothing is actually locked, but the row would sit there
    forever claiming a pass was in progress.
    """
    from utils.db import get_pool

    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "UPDATE job_runs SET status = 'interrupted', finished_at = NOW() "
            " WHERE finished_at IS NULL "
            "   AND started_at < NOW() - ($1 || ' seconds')::interval",
            str(int(STALE_AFTER_SECONDS)))
    try:
        count = int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover - driver dependent
        count = 0
    if count:
        logger.warning("marked %d interrupted job run(s) at startup", count)
    return count


async def _due(conn, job: str, interval: float) -> bool:
    """Whether enough time has passed since the last SUCCESSFUL pass.

    Read from the table, not from a process timer. A crash loop restarting every
    thirty seconds would otherwise run retention every thirty seconds, which is
    both wasteful and a good way to notice a scheduler only after it has deleted
    a lot of things quickly.
    """
    last = await conn.fetchval(
        "SELECT max(started_at) FROM job_runs "
        " WHERE job = $1 AND status = 'succeeded'", job)
    if last is None:
        return True
    gap = await conn.fetchval(
        "SELECT EXTRACT(EPOCH FROM (NOW() - $1::timestamptz))", last)
    return float(gap or 0) >= interval


async def _tenant_ids(conn) -> list:
    """Every active tenant. `tenants` carries no RLS — it is the registry, not
    tenant-scoped data — so this is the one query that legitimately spans
    them."""
    rows = await conn.fetch(
        "SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
    return [r["id"] for r in rows]


async def _principals_for(tenant_id) -> list[tuple]:
    """(user_id, workspace_id) for that tenant's users who have a window set.

    Read INSIDE the tenant's own boundary: `provisioning_connection` binds
    `app.tenant_id`, so `retention_settings`' policy is satisfied rather than
    bypassed. An unbound connection here would match zero rows and the pass
    would report success having enumerated nothing.

    Only rows with a window: a user who has never opened the Privacy Centre has
    nothing to expire, and enumerating them would make every pass proportional
    to the user table rather than to the work.

    The workspace is the DEFAULT one. `enforce_for` covers the workspace it is
    bound to and REPORTS how many it did not, so the rest are recorded rather
    than silently skipped. Extending to every workspace is a loop over the same
    call, and belongs with a decision about ordering and cost.
    """
    from core.identity.db_context import scheduler_connection

    async with scheduler_connection(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT r.user_id, w.id AS workspace_id "
            "  FROM retention_settings r "
            "  JOIN workspaces w ON w.tenant_id = r.tenant_id AND w.is_default "
            " WHERE (COALESCE(r.conversation_retention_days, 0) > 0 "
            "     OR COALESCE(r.memory_retention_days, 0) > 0) "
            " ORDER BY r.user_id")
    return [(r["user_id"], r["workspace_id"]) for r in rows]


async def run_retention_once(
    *, force: bool = False, stop: Optional[asyncio.Event] = None,
) -> RunOutcome:
    """One pass. Returns what it did.

    `force` skips the interval check, not the lock. Operator tooling and tests
    need to run a pass now; neither needs two passes at once.

    `stop`, when given and set, ends the pass at the next tenant or principal:
    the one in hand finishes, the rest wait for the next pass. The row is
    closed as `interrupted` and the lock released, so a shutdown neither waits
    for a whole pass nor leaves anything to clean up. The pass is not counted
    as done, so the next one is due at once.
    """
    from core.identity.principal import Principal
    from core.privacy import retention as retention_module
    from utils.db import get_pool

    interval = _float_env(INTERVAL_ENV, DEFAULT_INTERVAL)
    outcome = RunOutcome()

    # The lock is held on THIS connection for the whole pass. If the process
    # dies, PostgreSQL drops the connection and releases it — no lease, no
    # heartbeat, no stuck lock to clear by hand.
    #
    # A plain connection: `job_runs` is not tenant-scoped and `tenants` carries
    # no RLS, so nothing read or written here needs a bound tenant. Everything
    # that does is bound below, per tenant.
    async with get_pool().acquire() as conn:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
        if not locked:
            logger.info("another instance holds the retention lock; skipping")
            return outcome

        try:
            if not force and not await _due(conn, RETENTION_JOB, interval):
                logger.debug("retention is not due yet")
                return outcome

            # `forced` is recorded, not inferred. Without it the history cannot
            # answer the question an operator actually asks of it: two passes
            # 0.1s apart with a 24-hour floor is either a broken schedule or
            # somebody pressing "run a pass now" twice, and those need opposite
            # responses.
            #
            # Found by P4-12's own acceptance check, which measured the minimum
            # gap between passes and reported a floor violation that was really
            # two forced runs from the test suite. A record that cannot
            # distinguish them makes every such reading ambiguous.
            run_id = await conn.fetchval(
                "INSERT INTO job_runs (job, runner, detail) "
                "VALUES ($1, $2, $3::jsonb) RETURNING id",
                RETENTION_JOB, _runner_id(),
                '{"forced": true}' if force else '{"forced": false}')
            outcome.run_id = run_id
            outcome.forced = bool(force)

            def _stopping() -> bool:
                return stop is not None and stop.is_set()

            work: list[tuple] = []
            for tenant_id in await _tenant_ids(conn):
                if _stopping():
                    break
                for user_id, workspace_id in await _principals_for(tenant_id):
                    work.append((tenant_id, user_id, workspace_id))
            outcome.tenants_seen = len(work)

            for tenant_id, user_id, workspace_id in work:
                if _stopping():
                    break
                key = f"{tenant_id}:{user_id}"
                principal = Principal(
                    user_id=user_id, tenant_id=tenant_id,
                    workspace_id=workspace_id, session_id=uuid.uuid4(),
                    edition="cloud", username="", is_owner=False)
                try:
                    result = await retention_module.enforce_for(principal)
                except Exception as exc:  # noqa: BLE001
                    # Bounded: one tenant's failure must not stop the rest.
                    # The TYPE only — an exception message can carry a row id or
                    # a fragment of content, and this table is read by operators
                    # who are not that tenant.
                    outcome.failures[key] = type(exc).__name__
                    logger.warning("retention failed for one principal: %s",
                                   type(exc).__name__)
                    continue

                removed = (result.conversations_deleted + result.messages_deleted
                           + result.memories_deleted + result.memories_expired)
                outcome.items_removed += removed
                if removed or result.workspaces_not_covered:
                    outcome.detail[key] = {
                        "conversations": result.conversations_deleted,
                        "messages": result.messages_deleted,
                        "memories": result.memories_deleted,
                        "expired": result.memories_expired,
                        "workspaces_not_covered": result.workspaces_not_covered,
                    }

            if _stopping():
                outcome.status = "interrupted"
            else:
                outcome.status = "failed" if outcome.failures else "succeeded"
            await _finish(conn, outcome)
            logger.info("retention pass %s: %d principal(s), %d item(s) removed",
                        outcome.status, outcome.tenants_seen, outcome.items_removed)
            return outcome
        except Exception:
            # The row must not be left `running` by an error above. Recorded as
            # failed here rather than swept up an hour later by the stale check.
            if outcome.run_id is not None:
                outcome.status = "failed"
                try:
                    await _finish(conn, outcome)
                except Exception:  # noqa: BLE001 - pragma: no cover
                    logger.exception("could not close the job_runs row")
            raise
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtext($1))", RETENTION_JOB)


async def _finish(conn, outcome: RunOutcome) -> None:
    import json

    detail = dict(outcome.detail)
    # Carried through: `_finish` replaces `detail` wholesale, so a flag set
    # at INSERT time would otherwise vanish the moment the pass completed —
    # present on every running row and absent on every finished one, which
    # is the worst of both.
    detail["forced"] = bool(outcome.forced)
    if outcome.failures:
        detail["_failures"] = outcome.failures
    await conn.execute(
        "UPDATE job_runs SET status = $2, finished_at = NOW(), "
        "       tenants_seen = $3, items_removed = $4, detail = $5::jsonb "
        " WHERE id = $1",
        outcome.run_id, outcome.status, outcome.tenants_seen,
        outcome.items_removed, json.dumps(detail))


async def run_forever(stop: Optional[asyncio.Event] = None) -> None:
    """The loop. Started by the application lifespan when enabled.

    Cancellation is respected promptly: the wait is on an Event rather than a
    bare `sleep`, so shutdown does not hang for a tick interval. A pass already
    in flight stops at the next tenant or principal (`run_retention_once`'s
    `stop`): the one in hand finishes, the row is closed as `interrupted`, the
    lock is released. It used to run to the end, so shutdown waited for a whole
    pass: 3 seconds on a test database left with 2,048 tenants, longer on a large
    install. Killing it mid-delete instead would be safe (the work is
    idempotent) but would leave a `running` row for the stale check, and a clean
    shutdown should not need cleaning up after.
    """
    stop = stop or asyncio.Event()
    tick = _float_env(TICK_ENV, DEFAULT_TICK)

    try:
        await recover_interrupted_runs()
    except Exception:  # noqa: BLE001
        # A recovery failure must not stop the scheduler from starting: the
        # stale rows are cosmetic until somebody reads them, and refusing to run
        # retention because of them would be the worse outcome.
        logger.exception("could not recover interrupted runs; continuing")

    logger.info("retention scheduler started (tick %.0fs)", tick)
    while not stop.is_set():
        try:
            await run_retention_once(stop=stop)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("retention pass raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    logger.info("retention scheduler stopped")


__all__ = ["RETENTION_JOB", "RunOutcome", "enabled", "recover_interrupted_runs",
           "run_retention_once", "run_forever"]
