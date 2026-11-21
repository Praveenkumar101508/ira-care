#!/usr/bin/env python3
"""Run the background job loop in a dedicated worker process.

    IRA_SCHEDULER_ENABLED=true python scripts/staging/run_scheduler.py

The loop (`core.jobs.scheduler.run_forever`) is normally started by the API
process. On a deployment with more than one API replica, or where the API must
not do background work, the API is told not to run it (`IRA_SCHEDULER_IN_API=false`)
and this process runs it instead.

This worker gates on the master switch only. It deliberately does NOT read
`IRA_SCHEDULER_IN_API`: that setting says WHERE the loop runs, not WHETHER it
runs, and a worker that consulted it would start, read its own configuration
and idle forever while reporting itself healthy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Make the backend importable when run from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "supracloud-jarvis" / "ira"))

from core.runtime.vendor_telemetry import switch_off_vendor_telemetry  # noqa: E402

switch_off_vendor_telemetry()

from core.jobs import scheduler  # noqa: E402
from utils.db import close_pool, init_pool  # noqa: E402

logger = logging.getLogger("ira.scheduler.worker")


async def _main() -> int:
    if not scheduler.enabled():
        logger.error(
            "%s is not set; refusing to start a worker that would do nothing. "
            "Set it to true to run the job loop here.", scheduler.ENABLED_ENV)
        return 2

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - platforms without signals
            pass

    await init_pool()
    try:
        logger.info("job loop running in a dedicated worker (pid %s)", os.getpid())
        await scheduler.run_forever(stop)
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(_main()))
