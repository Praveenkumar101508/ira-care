"""Which background jobs the worker runs: ``IRA_WORKER_JOBS``.

The worker (``python -m worker.main``) used to register every job it knows,
so starting it at all meant starting all of them. That includes jobs that
call the language model, one that writes its own notes into a conversation
(``self_reflection``), one that blocks IP addresses at the API
(``security_scan``), and one that deletes Memory Vault entries older than 90
days (``memory_retention``). ``start-ira.sh`` never started the worker, and
``scripts/run-local.ps1`` started it with everything.

Now each job runs only when it is named. The default is the jobs that tidy
IRA's own bookkeeping or deliver what a person asked for, and call no model.
The rest are the owner's choice, job by job: see
``docs/operations/WORKER_JOBS_2026-10-23.md``.

No APScheduler import here, so this can be tested where APScheduler is not
installed (CI's ``test`` job).
"""
from __future__ import annotations

import os

#: Every job the worker can register, by id. ``worker/scheduler.py`` refuses
#: to register an id that is not here, so a new job cannot run unclassified.
KNOWN_JOBS: frozenset[str] = frozenset({
    "morning_briefing", "evening_summary", "security_scan", "business_scan",
    "reminder_check", "security_digest", "calendar_sync", "self_healing",
    "self_reflection", "database_backup", "architect_evolution",
    "memory_retention", "model_perf_retention", "token_version_retention",
    "heartbeat", "weekly_reflection",
    # IRA CARE: the check-in cycle (care/checkin_cycle.py). Every minute,
    # schedules the next check-in per person, skips quiet hours, marks misses.
    "care_checkins",
    # IRA CARE: the monitoring rules and the SOS chain (care/escalation.py).
    "care_monitoring",
    # IRA CARE: the daily carer report and the weekly family report (care/reports.py).
    "care_reports",
    # IRA CARE: the daily retention purge of care data past each person's period
    # (care/retention.py).
    "care_retention",
    # IRA CARE: keeps the conversation model loaded in Ollama between turns, so the
    # first words after a quiet spell do not wait for a model load (care/conversation.py).
    "care_model_warm",
})

#: What runs when ``IRA_WORKER_JOBS`` is unset. None of these calls a model.
#:   reminder_check           delivers the reminders people set, through
#:                            notify(): Telegram or email only if configured
#:   model_perf_retention     deletes model timing rows older than 30 days
#:   token_version_retention  deletes erased accounts' logout counters after
#:                            7 days (the API also does this at every start)
DEFAULT_JOBS: frozenset[str] = frozenset({
    "reminder_check", "model_perf_retention", "token_version_retention",
})


def enabled_jobs(value: str | None = None) -> frozenset[str]:
    """The jobs ``IRA_WORKER_JOBS`` (or ``value``) switches on.

    Unset or empty: ``DEFAULT_JOBS``. ``none``: nothing. ``all``: every known
    job. Otherwise a comma-separated list of job ids. An id the worker does
    not know is refused, not ignored: a misspelt name would otherwise switch a
    job off without a word.
    """
    raw = os.getenv("IRA_WORKER_JOBS") if value is None else value
    raw = (raw or "").strip().lower()
    if not raw:
        return DEFAULT_JOBS
    if raw == "none":
        return frozenset()
    if raw == "all":
        return KNOWN_JOBS
    names = frozenset(n.strip() for n in raw.split(",") if n.strip())
    if not names:   # "," is not empty and not "none": refuse it, do not run nothing
        raise ValueError(f"IRA_WORKER_JOBS={raw!r} names no job; "
                         "leave it empty for the default, or use none / all")
    unknown = names - KNOWN_JOBS
    if unknown:
        raise ValueError(f"IRA_WORKER_JOBS names unknown job(s) {sorted(unknown)}; "
                         f"known: {sorted(KNOWN_JOBS)}, or none / all")
    return names
