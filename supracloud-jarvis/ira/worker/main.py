"""
IRA Worker — Background intelligence process entry point.

Initialises DB/Redis connections, starts the APScheduler,
then keeps running until a shutdown signal is received.

Run: python -m worker.main
"""

from __future__ import annotations

import asyncio
import logging
import signal

# First, before anything that could import ONNX Runtime: it reports to
# Microsoft as soon as it is imported unless ORT_DISABLE_TELEMETRY is already
# in the environment (core/runtime/vendor_telemetry.py).
from core.runtime.vendor_telemetry import switch_off_vendor_telemetry

switch_off_vendor_telemetry()

from config import get_settings
from utils.db import init_pool, close_pool
from utils.redis_client import init_redis, close_redis
from worker.scheduler import running_jobs, start_scheduler, stop_scheduler
from worker.notifier import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ira.worker")


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, on_shutdown
) -> None:
    """
    Register SIGTERM/SIGINT handlers in the most async-safe way the
    platform supports.

    Fix #74: on Unix, loop.add_signal_handler() delivers signals via the
    loop's self-pipe, so the callback runs inside the event loop thread —
    unlike signal.signal(), which can interrupt at arbitrary await points.

    On Windows (ProactorEventLoop) add_signal_handler() raises
    NotImplementedError, so we fall back to signal.signal() but marshal
    back onto the loop with call_soon_threadsafe() to preserve safety.
    """
    try:
        loop.add_signal_handler(signal.SIGTERM, on_shutdown)
        loop.add_signal_handler(signal.SIGINT, on_shutdown)
        logger.debug("Signal handlers installed via add_signal_handler (Unix)")
    except NotImplementedError:
        def _win_handler(signum, frame):
            loop.call_soon_threadsafe(on_shutdown)

        # SIGINT (Ctrl+C) is reliably settable on Windows.
        signal.signal(signal.SIGINT, _win_handler)
        # SIGTERM is not reliably supported on Windows; best-effort only.
        try:
            signal.signal(signal.SIGTERM, _win_handler)
        except (ValueError, AttributeError, OSError):
            logger.warning(
                "SIGTERM not settable on this platform; "
                "graceful shutdown available via Ctrl+C (SIGINT) only."
            )
        logger.debug("Signal handlers installed via signal.signal (Windows)")


async def _shutdown() -> None:
    """Tear down resources, ensuring every step runs even if one fails."""
    for name, coro in (
        ("scheduler", stop_scheduler),
        ("db pool", close_pool),
        ("redis", close_redis),
    ):
        try:
            await coro()
        except Exception:
            logger.exception("Error while shutting down %s", name)


async def main() -> None:
    logger.info("IRA Worker starting...")

    await init_pool()
    logger.info("Database pool ready")

    await init_redis()
    logger.info("Redis connection ready")

    await start_scheduler()

    # Announce startup: what is on, not what could be (IRA_WORKER_JOBS).
    owner = get_settings().owner_name
    jobs = running_jobs()
    await notify(
        "IRA Worker Online",
        f"{owner}, IRA's background worker is running. "
        f"Jobs on: {', '.join(jobs) if jobs else 'none'}.",
        category="system",
        priority="info",
    )

    logger.info("IRA Worker is running.")

    # Block until a shutdown signal is received.
    stop_event = asyncio.Event()

    def _request_shutdown():
        logger.info("Shutdown signal received. Stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    _install_shutdown_handlers(loop, _request_shutdown)

    try:
        await stop_event.wait()
    finally:
        await _shutdown()
        logger.info("IRA Worker stopped. Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Fallback if Ctrl+C arrives before handlers are installed
        # (e.g. during init_pool / init_redis).
        logger.info("Interrupted before startup completed. Exiting.")