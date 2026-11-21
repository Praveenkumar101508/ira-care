"""Is the monitoring itself alive?

Every care job records a heartbeat row in `job_runs` when it runs. If the
check-in or monitoring job has not completed a pass for `STALE_AFTER_S`
(5 minutes), monitoring is "paused": the dashboard shows a red banner, and an
operations alert goes out through the provider interface (log-only until an
operations contact and provider keys are configured), at most once every
`REPEAT_ALERT_S`.

The check runs in the API process (the dashboard polls `/care/ops/status`), so
it does not depend on the worker that it is watching.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Optional

from . import providers

logger = logging.getLogger("ira.care.ops")

WATCHED_JOBS = ("care_checkins", "care_monitoring")
STALE_AFTER_S = 300
REPEAT_ALERT_S = 900

_last_alert_at: dict[str, dt.datetime] = {}


async def heartbeat(conn, job: str, outcome: Optional[dict] = None) -> None:
    """One finished pass of `job`, recorded in `job_runs` (SELECT/INSERT/UPDATE
    are granted to the runtime roles; the policies admit every row)."""
    await conn.execute(
        "INSERT INTO job_runs (job, runner, status, finished_at, detail) VALUES ($1, $2, 'succeeded', NOW(), $3::jsonb)",
        job, f"care:{os.getpid()}", json.dumps(outcome or {}, default=str))


async def last_runs(conn) -> dict[str, Optional[dt.datetime]]:
    rows = await conn.fetch(
        "SELECT job, max(finished_at) AS last FROM job_runs WHERE job = ANY($1::text[]) AND finished_at IS NOT NULL GROUP BY job",
        list(WATCHED_JOBS))
    out: dict[str, Optional[dt.datetime]] = {j: None for j in WATCHED_JOBS}
    for r in rows:
        out[r["job"]] = r["last"]
    return out


def assess(last: dict[str, Optional[dt.datetime]], now: dt.datetime, stale_after_s: int = STALE_AFTER_S) -> dict:
    jobs = {}
    paused = False
    for job in WATCHED_JOBS:
        at = last.get(job)
        age = None if at is None else int((now - at).total_seconds())
        stale = at is None or age > stale_after_s
        paused = paused or stale
        jobs[job] = {"last_run_at": at.isoformat() if at else None, "age_s": age, "stale": stale}
    return {"monitoring_paused": paused, "stale_after_s": stale_after_s, "jobs": jobs, "checked_at": now.isoformat()}


async def status(conn, now: Optional[dt.datetime] = None, *, provider: Optional[providers.MessageProvider] = None,
                 alert: bool = True) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    report = assess(await last_runs(conn), now)
    if report["monitoring_paused"] and alert:
        report["ops_alert"] = await maybe_alert(report, now, provider=provider)
    return report


async def maybe_alert(report: dict, now: dt.datetime, *, provider: Optional[providers.MessageProvider] = None) -> Optional[dict]:
    """Tell the operations contact, at most once per REPEAT_ALERT_S."""
    last = _last_alert_at.get("ops")
    if last is not None and (now - last).total_seconds() < REPEAT_ALERT_S:
        return {"sent": False, "reason": "already alerted recently"}
    provider = provider or providers.provider_from_env()
    to = (os.getenv("IRA_CARE_OPS_PHONE") or "").strip() or "+440000000000"
    stale = [j for j, v in report["jobs"].items() if v["stale"]]
    text = f"IRA CARE monitoring paused: {', '.join(stale)} have not run for over {STALE_AFTER_S // 60} minutes."
    try:
        delivery = await provider.send_sms(to, text)
    except providers.EmergencyNumberRefused as exc:
        delivery = providers.Delivery("skipped", str(exc)[:200])
    _last_alert_at["ops"] = now
    logger.error("care ops: %s (%s)", text, delivery.outcome)
    return {"sent": delivery.outcome in ("sent", "logged"), "outcome": delivery.outcome, "text": text}


def reset_alert_memory() -> None:
    _last_alert_at.clear()
