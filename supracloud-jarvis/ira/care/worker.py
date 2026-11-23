"""The IRA CARE worker: the care jobs, in a process of their own.

    python -m care.worker

Runs `worker/scheduler.py`'s scheduler with the jobs `IRA_WORKER_JOBS` names
(the deployment sets the four care jobs), against the same database and
Redis as the server. Each pass writes a heartbeat (care/ops.py); the server's
/health/jobs and the dashboard banner say when it stops.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger("ira.care.worker")

os.environ.setdefault("IRA_WORKER_JOBS", "care_checkins,care_monitoring,care_reports,care_retention")


async def _main() -> int:
    from core.runtime.logging_config import configure_logging
    from utils.db import close_pool, init_pool
    from utils.redis_client import close_redis, init_redis
    from worker.scheduler import build_scheduler
    configure_logging(service="ira-care-worker")
    await init_pool()
    await init_redis()
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("care worker up: jobs %s", sorted(j.id for j in scheduler.get_jobs()))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - not on this platform
            pass
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await close_redis()
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
