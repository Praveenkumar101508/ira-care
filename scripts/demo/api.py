#!/usr/bin/env python3
"""The IRA CARE demo API: the real care, login and workspace routers, without
the rest of IRA (chat, voice, the agent graph and their heavy dependencies).

    python scripts/demo/api.py            # http://127.0.0.1:8000

It runs the same request pipeline (rate limiting, IP blocklist, request
context), the same login, the same care routes and the two care jobs, against
the database and Redis named in the environment. It allows the dashboard and
the family app on localhost as browser origins, which the full server does
only in development mode. It is for demos and local development; the full
server is `uvicorn main:app`.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("IRA_EDITION", "cloud")
os.environ.setdefault("IRA_CARE_MODEL", "fake")
os.environ.setdefault("IRA_WORKER_JOBS", "care_checkins,care_monitoring,care_reports,care_retention")
from demo._local_state import prepare_environment  # noqa: E402

for _k, _v in (("POSTGRES_USER", "ira_app"), ("POSTGRES_PASSWORD", "ira_app"), ("POSTGRES_DB", "ira_test"),
               ("POSTGRES_HOST", "127.0.0.1"), ("REDIS_PASSWORD", "")):
    os.environ.setdefault(_k, _v)
prepare_environment(edition="cloud", model="fake")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

logger = logging.getLogger("ira.care.demo")
DEMO_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8081", "http://127.0.0.1:8081"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    from utils.db import close_pool, init_pool
    from utils.redis_client import close_redis, init_redis
    from worker.scheduler import build_scheduler
    await init_pool()
    await init_redis()
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("care demo API up: jobs %s", sorted(j.id for j in scheduler.get_jobs()))
    from demo.memtrace import start_if_asked
    memtrace = start_if_asked()   # debug only: nothing unless IRA_DEBUG_MEMTRACE is set
    try:
        yield
    finally:
        if memtrace is not None:
            memtrace.cancel()
        scheduler.shutdown(wait=False)
        await close_redis()
        await close_pool()


def create_demo_app() -> FastAPI:
    from config import get_settings
    from api.middleware.request_context import RequestContextMiddleware
    from api.routes.workspaces import router as workspaces_router
    from app.middleware import _IPBlockMiddleware, limiter
    from auth.routes import register_auth_routes
    from care.routes import router as care_router
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    get_settings()
    app = FastAPI(title="IRA CARE demo API", lifespan=lifespan)
    # The same rate limiting and IP blocklist as the full server. The full server's
    # delegated-scope gate is NOT installed here: it knows the private edition's
    # delegated accounts only and answers 403 to every cloud account on /api/*
    # (recorded in STATUS.md for the deployment phase). Workspace roles are what
    # authorise care calls (care/authz.py), and those are enforced by the routes.
    app.state.limiter = limiter
    # The same error handlers as the full server (app/factory.py): without them a structured
    # refusal such as "step-up required" escaped as a 500, and the dashboard showed "No connection"
    # instead of asking the administrator to confirm who they are (found by the browser test, round 10).
    from core.runtime.errors import install_error_handlers
    install_error_handlers(app)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(_IPBlockMiddleware)
    register_auth_routes(app, limiter)
    app.include_router(workspaces_router, prefix="/api/v1")
    app.include_router(care_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "ira-care-demo"}

    # Outermost, so the browser preflight for the demo origins is answered here.
    app.add_middleware(CORSMiddleware, allow_origins=DEMO_ORIGINS, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Workspace-ID", "X-Device-Token", "X-Idempotency-Key"])
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_demo_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("IRA_CARE_DEMO_PORT") or "8000")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
