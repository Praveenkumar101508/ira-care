"""
IRA APScheduler — Proactive intelligence job registry.

Jobs are held in memory and registered afresh at every start, and only the
ones IRA_WORKER_JOBS names (worker/jobs.py) are registered. They used to be
persisted in PostgreSQL. That needed CREATE on schema public, which 042
revokes from every role IRA runs as, and it kept running a job that had been
switched off, because a stored job is reloaded whether or not this start
registered it. With a two-minute misfire grace, persistence caught up nothing.

Job schedule:
  08:00 daily    — Morning briefing
  20:00 daily    — Evening summary
  Every 60s      — Security scan (nginx logs + system health)
  Every 5 min    — Business scan (new leads, bookings)
  Every 15 min   — Reminder check (due reminders)
  Every 30 min   — Cal.com calendar sync
  Every 6 hours  — Security report summary
  Every 12 hours — Architect Evolution Team cycle (silent, notifies when ready)
  Every 60s      — Liveness tick (always on: the container healthcheck reads it)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Healthcheck: write a fresh timestamp here on every liveness tick (every 60 s,
# always registered). The compose healthcheck verifies this file exists and is
# < 120s old.
# Portable default: OS temp dir (/tmp on Linux, %LOCALAPPDATA%\Temp on Windows).
# In Docker, WORKER_HEARTBEAT_FILE MUST be set explicitly to the path the
# compose healthcheck watches — do NOT rely on this default in containers.
WORKER_HEARTBEAT_FILE = os.getenv(
    "WORKER_HEARTBEAT_FILE",
    str(Path(tempfile.gettempdir()) / "ira_worker_tick"),
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

from config import get_settings
from worker.briefing import generate_briefing
from worker.security_monitor import run_security_scan
from worker.business_monitor import run_business_scan
from worker.reminders import check_due_reminders as _check_due_reminders


async def check_due_reminders() -> None:
    """Scheduler seam: bind the tenant's default workspace, then run the sweep.

    Reminders are the tenant's own scheduled work — no request, no enqueuing
    user — so this uses the one sanctioned background path. If that workspace
    is absent the sweep is skipped and logged rather than running unscoped: a
    reminder that does not fire is a missed nudge, and one that fires from
    another workspace is a disclosure.
    """
    from core.identity.background import default_workspace_job_principal
    from core.identity.principal import PRIVATE_TENANT_ID

    try:
        principal = await default_workspace_job_principal(
            PRIVATE_TENANT_ID, job="reminders"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("reminders: no workspace context, sweep skipped: %s", e)
        return
    await _check_due_reminders(principal)
from worker.self_healing import run_self_healing_check, run_self_reflection
from worker.backup import run_database_backup
from tasks.calendar import sync_calcom_bookings
from worker.jobs import KNOWN_JOBS, enabled_jobs

logger = logging.getLogger("ira.scheduler")

#: Always registered, whatever IRA_WORKER_JOBS says, and not one of KNOWN_JOBS:
#: it only writes WORKER_HEARTBEAT_FILE, so "is the scheduler alive" does not
#: depend on which jobs are switched on.
LIVENESS_JOB_ID = "worker_liveness"

_scheduler: AsyncIOScheduler | None = None


def build_scheduler(jobs: frozenset[str] | None = None) -> AsyncIOScheduler:
    """The scheduler, with only ``jobs`` registered (default: ``enabled_jobs()``)."""
    cfg = get_settings()
    jobs = enabled_jobs() if jobs is None else jobs

    jobstores = {
        "default": MemoryJobStore()
    }
    executors = {
        "default": AsyncIOExecutor()
    }
    job_defaults = {
        "coalesce": True,         # If a job missed multiple runs, run it once
        "max_instances": 1,       # Never run the same job twice simultaneously
        "misfire_grace_time": 120, # Allow up to 2min late execution
    }

    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone="UTC",
    )

    def add(func, *, id: str, **kwargs) -> None:
        if id not in KNOWN_JOBS:   # an unclassified job would never run, silently
            raise ValueError(f"job {id!r} is not in worker/jobs.py KNOWN_JOBS: add it, and decide its default")
        if id in jobs:
            scheduler.add_job(func, id=id, **kwargs)

    # Liveness — every 60 seconds, from the first second. The tick file used to
    # be written by self_healing, which calls the model and is now off unless
    # named; the container healthcheck must not depend on that choice.
    scheduler.add_job(
        _liveness_tick,
        trigger="interval",
        seconds=60,
        id=LIVENESS_JOB_ID,
        name="IRA Worker liveness tick",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )

    # ── Register jobs ─────────────────────────────────────────────────────────

    # Morning briefing (08:00 UTC — adjust IRA_BRIEFING_TZ if needed)
    add(
        _morning_briefing,
        trigger="cron",
        hour=int(cfg.briefing_hour_utc),
        minute=0,
        id="morning_briefing",
        name="IRA Morning Briefing",
        replace_existing=True,
    )

    # Evening summary (20:00 UTC)
    add(
        _evening_summary,
        trigger="cron",
        hour=20,
        minute=0,
        id="evening_summary",
        name="IRA Evening Summary",
        replace_existing=True,
    )

    # Security scan — every 60 seconds
    add(
        _care_checkins,
        trigger="interval",
        seconds=60,
        id="care_checkins",
        name="IRA CARE check-in cycle",
        replace_existing=True,
    )
    add(
        _care_monitoring,
        trigger="interval",
        seconds=60,
        id="care_monitoring",
        name="IRA CARE monitoring and escalation",
        replace_existing=True,
    )
    add(
        _care_reports,
        trigger="cron",
        minute=5,
        id="care_reports",
        name="IRA CARE daily and weekly reports",
        replace_existing=True,
    )
    add(
        run_security_scan,
        trigger="interval",
        seconds=60,
        id="security_scan",
        name="Security Guardian Scan",
        replace_existing=True,
    )

    # Business scan — every 5 minutes
    add(
        run_business_scan,
        trigger="interval",
        minutes=5,
        id="business_scan",
        name="Business Monitor Scan",
        replace_existing=True,
    )

    # Reminder check — every 15 minutes
    add(
        check_due_reminders,
        trigger="interval",
        minutes=15,
        id="reminder_check",
        name="Reminder Delivery Check",
        replace_existing=True,
    )

    # 6-hourly security digest
    add(
        _security_digest,
        trigger="interval",
        hours=6,
        id="security_digest",
        name="Security Digest",
        replace_existing=True,
    )

    # Calendar sync — every 30 minutes (pulls Cal.com bookings into calendar_events)
    add(
        _sync_calendar,
        trigger="interval",
        minutes=30,
        id="calendar_sync",
        name="Cal.com Calendar Sync",
        replace_existing=True,
    )

    # Self-Healing Agent — every 60 seconds, detects and fixes system issues
    add(
        _self_healing_check,
        trigger="interval",
        seconds=60,
        id="self_healing",
        name="IRA Self-Healing Agent",
        replace_existing=True,
    )

    # Self-Learning Reflection — hourly, improves IRA's own routing and prompts
    add(
        _self_reflection,
        trigger="interval",
        hours=1,
        id="self_reflection",
        name="IRA Self-Learning Reflection",
        replace_existing=True,
    )

    # Daily database backup — 03:00 UTC (low-traffic window)
    add(
        _database_backup,
        trigger="cron",
        hour=3,
        minute=0,
        id="database_backup",
        name="IRA Daily Database Backup",
        replace_existing=True,
    )

    # Architect Evolution Team — every 12 hours (06:00 and 18:00 UTC)
    # Silently analyses IRA vs peer systems, generates proposal, notifies owner.
    # Nothing is implemented until owner types "architect implement: [feature name]"
    add(
        _architect_cycle,
        trigger="interval",
        hours=12,
        id="architect_evolution",
        name="IRA Architect Evolution Team",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    # Memory retention — weekly at 02:00 UTC on Sunday (Fix #75)
    # Deletes memory_embeddings rows older than 90 days to keep the table
    # from growing unbounded. Messages + conversations are NOT deleted, and
    # neither are the reference corpora (RETENTION_EXEMPT_NAMESPACES in
    # memory/namespaces.py).
    add(
        _memory_retention,
        trigger="cron",
        day_of_week="sun",
        hour=2,
        minute=0,
        id="memory_retention",
        name="IRA Memory Retention Purge",
        replace_existing=True,
    )

    # Model performance retention — weekly at 02:30 UTC on Sunday (Fix P30)
    # model_performance grows by 1 row per LLM call — unbounded without a purge.
    # Keep 30 days (enough for monthly trend analysis); older rows are noise.
    add(
        _model_perf_retention,
        trigger="cron",
        day_of_week="sun",
        hour=2,
        minute=30,
        id="model_perf_retention",
        name="IRA Model Performance Retention Purge",
        replace_existing=True,
    )

    # Token-version retention — daily at 03:15 UTC (migration 096). An erased
    # account's counter is kept seven days after erasure, the longest life of a
    # credential IRA signs, then deleted. 096 lets only ira_worker delete, and
    # only rows past those seven days.
    add(
        _token_version_retention,
        trigger="cron",
        hour=3,
        minute=15,
        id="token_version_retention",
        name="IRA Token-Version Retention Purge",
        replace_existing=True,
    )

    # Heartbeat — proactive read-only pass that surfaces pending decision reviews
    # and stale commitments via the voice path. Only when IRA_WORKER_JOBS names it AND
    # IRA_HEARTBEAT is on (it defaults to on).
    from worker.heartbeat import register_heartbeat
    if "heartbeat" in jobs:
        register_heartbeat(scheduler)

    # Weekly Self-Reflection — retrospective summary over already-logged activity,
    # spoken through the same surface. Only when IRA_WORKER_JOBS names it AND
    # IRA_REFLECTION is on (it defaults to on).
    from worker.reflection import register_reflection
    if "weekly_reflection" in jobs:
        register_reflection(scheduler)

    registered = sorted(job.id for job in scheduler.get_jobs() if job.id in KNOWN_JOBS)
    logger.info("Worker jobs on: %s. Off: %s. (IRA_WORKER_JOBS; see worker/jobs.py)",
                ", ".join(registered) or "none",
                ", ".join(sorted(KNOWN_JOBS - set(registered))) or "none")

    return scheduler


async def _morning_briefing() -> None:
    try:
        await generate_briefing("morning")
    except Exception as e:
        logger.error(f"Morning briefing failed: {e}", exc_info=True)


async def _evening_summary() -> None:
    try:
        await generate_briefing("evening")
    except Exception as e:
        logger.error(f"Evening summary failed: {e}", exc_info=True)


async def _security_digest() -> None:
    """
    Fix #72: generate a 6-hour security digest from the DB instead of
    re-running the scan.  The old implementation called run_security_scan()
    which would re-process logs rather than summarise what happened.
    """
    try:
        from utils.db import acquire
        from worker.notifier import notify
        from config import get_settings

        async with acquire() as conn:
            rows = await conn.fetch(
                """SELECT severity, event_type, source_ip, description, created_at
                   FROM security_events
                   WHERE created_at > NOW() - INTERVAL '6 hours'
                   ORDER BY created_at DESC
                   LIMIT 20"""
            )

        if not rows:
            logger.debug("Security digest: no events in last 6 hours")
            return

        cfg = get_settings()
        total = len(rows)
        critical = sum(1 for r in rows if r["severity"] == "critical")
        high = sum(1 for r in rows if r["severity"] == "high")

        lines = "\n".join(
            f"• [{r['severity'].upper()}] {r['event_type']} — {r['description'][:80]}"
            for r in rows[:10]
        )
        summary = (
            f"🔐 *6-hour Security Digest* — {total} event(s), "
            f"{critical} critical, {high} high\n\n{lines}"
        )
        if total > 10:
            summary += f"\n…and {total - 10} more events"

        priority = "critical" if critical > 0 else ("warning" if high > 0 else "info")
        await notify(
            f"Security Digest — {total} event(s) in last 6h",
            summary,
            category="security",
            priority=priority,
        )
        logger.info(f"Security digest sent: {total} events ({critical} critical, {high} high)")
    except Exception as e:
        logger.error(f"Security digest failed: {e}", exc_info=True)


async def _sync_calendar() -> None:
    try:
        from core.identity.background import default_workspace_job_principal
        from core.identity.principal import PRIVATE_TENANT_ID

        principal = await default_workspace_job_principal(
            PRIVATE_TENANT_ID, job="calcom_sync"
        )
        await sync_calcom_bookings(principal)
    except Exception as e:
        logger.error(f"Calendar sync failed: {e}", exc_info=True)


async def _care_checkins() -> None:
    """One pass of the IRA CARE check-in cycle. Guarded: a failing pass is
    logged and the next minute's pass still runs."""
    try:
        from care.checkin_cycle import run_cycle_once
        outcome = await run_cycle_once()
        logger.debug("care check-ins: %s", outcome)
    except Exception as e:
        logger.error(f"care check-in cycle failed: {e}", exc_info=True)


async def _care_monitoring() -> None:
    """One pass of the IRA CARE rules and escalation chain. Guarded like the
    check-in cycle: a failing pass is logged and the next minute's still runs."""
    try:
        from care.escalation import run_once
        outcome = await run_once()
        logger.debug("care monitoring: %s", outcome)
    except Exception as e:
        logger.error(f"care monitoring pass failed: {e}", exc_info=True)


async def _care_reports() -> None:
    """Hourly: generate and deliver the reports that have fallen due in each
    person's own timezone (care/reports.py). Idempotent per person and period."""
    try:
        from care.reports import run_once
        outcome = await run_once()
        logger.debug("care reports: %s", outcome)
    except Exception as e:
        logger.error(f"care reports pass failed: {e}", exc_info=True)


async def _liveness_tick() -> None:
    # Touch the heartbeat file so the healthcheck knows the scheduler is alive.
    try:
        with open(WORKER_HEARTBEAT_FILE, "w") as _hb:
            _hb.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"Heartbeat write failed ({WORKER_HEARTBEAT_FILE}): {e}")


async def _self_healing_check() -> None:
    try:
        await run_self_healing_check()
    except Exception as e:
        logger.error(f"Self-healing check failed: {e}", exc_info=True)


async def _self_reflection() -> None:
    try:
        await run_self_reflection()
    except Exception as e:
        logger.error(f"Self-reflection failed: {e}", exc_info=True)


async def _database_backup() -> None:
    try:
        path = await run_database_backup()
        if path:
            logger.info(f"Daily backup saved: {path.name}")
        else:
            logger.error("Daily backup FAILED — check pg_dump logs")
    except Exception as e:
        logger.error(f"Backup job failed: {e}", exc_info=True)


async def _architect_cycle() -> None:
    """
    24/7 Evolution Team background cycle.
    Runs silently every 12 hours — generates a proposal and notifies the owner.
    NEVER auto-implements anything — always waits for explicit 'architect implement: X' command.
    """
    try:
        from agents.architect_agent import run_background_architect_cycle
        await run_background_architect_cycle()
    except Exception as e:
        logger.error(f"Architect evolution cycle failed: {e}", exc_info=True)


async def _memory_retention() -> None:
    """Weekly purge of memory_embeddings older than 90 days, reference corpora excepted. (Fix #75)"""
    try:
        from memory.store import purge_old_memories_for_every_tenant

        # One scoped pass per tenant, never one unscoped pass. Through a bare
        # connection this deleted nothing at all — `memory_embeddings` has
        # forced RLS and an unbound connection matches no rows — while logging
        # "purged 0" every week, which reads exactly like a healthy no-op.
        deleted = await purge_old_memories_for_every_tenant(retention_days=90)
        logger.info(f"Memory retention complete: {deleted} old embeddings purged")
    except Exception as e:
        logger.error(f"Memory retention job failed: {e}", exc_info=True)


async def _token_version_retention() -> None:
    """Daily purge of counters for accounts erased more than seven days ago (096)."""
    try:
        from core.identity.token_versions import purge_erased

        purged = await purge_erased()
        logger.info("Token-version retention complete: %d counter(s) purged", purged)
    except Exception as e:
        logger.error("Token-version retention job failed: %s", e, exc_info=True)


async def _model_perf_retention() -> None:
    """Weekly purge of model_performance rows older than 30 days. (Fix P30)"""
    try:
        from core.identity.db_context import owner_connection
        # Batch 4 gave this table a tenant and forced row-level security, so a bare
        # `acquire()` binds no `app.tenant_id`, every predicate evaluates against
        # NULL, and the query matches zero rows *without failing*. `owner_connection`
        # binds the single private tenant and REFUSES in the cloud edition — which is
        # the honest state of this subsystem: it has no Principal to thread and is
        # Private-edition only (CLOUD-ADR-0001).
        async with owner_connection(reason="worker/scheduler.py") as conn:
            result = await conn.execute(
                "DELETE FROM model_performance WHERE created_at < NOW() - INTERVAL '30 days'"
            )
        # asyncpg returns "DELETE <n>" as the status string
        count = int(result.split()[-1]) if result else 0
        logger.info(f"Model performance retention: {count} old rows purged")
    except Exception as e:
        logger.error(f"Model performance retention job failed: {e}", exc_info=True)


def running_jobs() -> list[str]:
    """The ids of the jobs registered on this worker's scheduler, liveness aside."""
    return sorted(job.id for job in get_scheduler().get_jobs() if job.id in KNOWN_JOBS)


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = build_scheduler()
    return _scheduler


async def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info(f"IRA Scheduler started. Heartbeat file: {WORKER_HEARTBEAT_FILE}")


async def stop_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("IRA Scheduler stopped.")