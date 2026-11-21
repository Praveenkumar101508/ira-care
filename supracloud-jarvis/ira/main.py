"""
SupraCloud IRA — FastAPI application entry point (`uvicorn main:app`).

The application is assembled by `app.factory.create_app`:
  - startup/shutdown sequence  → app/lifespan.py
  - rate limit / CORS / IP block → app/middleware.py
  - auth endpoints             → auth/routes.py
  - router registration        → app/routers.py
"""

from __future__ import annotations

import logging

# First, before anything that could import ONNX Runtime: it reports to
# Microsoft as soon as it is imported unless ORT_DISABLE_TELEMETRY is already
# in the environment (core/runtime/vendor_telemetry.py).
from core.runtime.vendor_telemetry import switch_off_vendor_telemetry

switch_off_vendor_telemetry()

from app.factory import create_app
from app.middleware import limiter  # re-export: shared rate limiter instance

# Structured, redacted logging. JSON in staging/production, human-readable
# text in development — see core/runtime/logging_config.py. The lifespan
# reconfigures this once settings are loaded and the known secret values have
# been registered for redaction; this call covers the window before that, so
# even an import-time failure is logged through a redacting handler.
try:
    from core.runtime.logging_config import configure_logging
    configure_logging(service="ira-api")
except Exception:  # noqa: BLE001
    # Never let logging configuration prevent the process from starting.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
logger = logging.getLogger("ira")

app = create_app()

# Auto-instrument FastAPI routes (adds trace spans for every HTTP request)
#
# P13-02. This was `except Exception: pass`, and `PHASE_13_EXECUTION_PLAN.md`
# §2 names it as the phase's own trap: if instrumentation failed, tracing was
# off, the "active" line never printed, and NOTHING said so. The first thing a
# Phase 13 dashboard would have been unable to tell you is whether Phase 13 was
# working.
#
# Still non-fatal — telemetry must never stop the process from serving — but
# the outcome is now recorded, so `telemetry_state()` can answer instead of an
# operator inferring it from a log line that is not there.
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
    logger.info("OpenTelemetry FastAPI instrumentation active")
    _instrumented, _instr_reason = True, ""
except ImportError as exc:
    # The ordinary case on a deployment that does not install the extra. Not an
    # event, and logging it at ERROR would train an operator past the line that
    # matters — the same reason P13-01's finding O ended up with two handlers.
    _instrumented = False
    _instr_reason = f"opentelemetry-instrumentation-fastapi is not installed ({exc})"
    logger.debug("OpenTelemetry FastAPI instrumentation unavailable: %s", exc)
except Exception as exc:  # noqa: BLE001 — installed but would not attach
    _instrumented = False
    _instr_reason = f"{type(exc).__name__}: {exc}"
    logger.error(
        "OpenTelemetry FastAPI instrumentation FAILED to attach (%s). HTTP "
        "requests will produce NO spans; any latency or error rate read from "
        "traces is measuring nothing.", _instr_reason)

try:
    from utils.telemetry import record_instrumentation

    record_instrumentation(_instrumented, _instr_reason)
except Exception:  # noqa: BLE001 — recording the state must not break startup
    logger.exception("could not record telemetry instrumentation state")
