"""The IRA CARE server: the care routes, login and workspaces, as one process.

    uvicorn care.server:app --host 0.0.0.0 --port 8000

This is the process the deployment runs (deploy/docker-compose.yml). It is
the same request pipeline as the full IRA server (rate limiting, IP blocklist,
request context) with the routers IRA CARE needs, and none of the full
server's chat, voice or agent-graph start-up, which needs libraries and a
model server a care deployment does not have. The care jobs run in the
separate worker (`care/worker.py`) unless `IRA_CARE_JOBS_IN_API=true`.

Browser origins come from `IRA_CARE_ALLOWED_ORIGINS` (comma separated); with
none set, only same-origin calls work, which is how the reverse proxy serves
the dashboard and the API from one host name.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("ira.care.server")


def allowed_origins() -> list[str]:
    raw = os.getenv("IRA_CARE_ALLOWED_ORIGINS") or ""
    return [o.strip() for o in raw.split(",") if o.strip()]


def jobs_in_api() -> bool:
    return (os.getenv("IRA_CARE_JOBS_IN_API") or "false").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from utils.db import close_pool, init_pool
    from utils.redis_client import close_redis, init_redis
    await init_pool()
    await init_redis()
    scheduler = None
    if jobs_in_api():
        from worker.scheduler import build_scheduler
        scheduler = build_scheduler()
        scheduler.start()
        logger.info("care server: jobs in this process: %s", sorted(j.id for j in scheduler.get_jobs()))
    else:
        logger.info("care server: jobs run in the worker (IRA_CARE_JOBS_IN_API is off)")
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await close_redis()
        await close_pool()


def create_care_app() -> FastAPI:
    from config import get_settings
    from api.middleware.request_context import RequestContextMiddleware
    from api.routes.workspaces import router as workspaces_router
    from app.middleware import _IPBlockMiddleware, limiter
    from auth.routes import register_auth_routes
    from care.routes import router as care_router
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    get_settings()
    app = FastAPI(title="IRA CARE", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # The full server's delegated-scope gate is not installed: it knows the
    # private edition's delegated accounts only and refuses every cloud account
    # on /api/*. Workspace roles authorise every care route (care/authz.py).
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(_IPBlockMiddleware)
    register_auth_routes(app, limiter)
    app.include_router(workspaces_router, prefix="/api/v1")
    app.include_router(care_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        """Liveness and readiness in one: the process, the database, Redis."""
        from utils.db import acquire
        out = {"status": "ok", "service": "ira-care", "database": "ok", "redis": "ok"}
        try:
            async with acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            out["database"] = f"down: {type(exc).__name__}"
            out["status"] = "degraded"
        try:
            from utils.redis_client import get_redis
            await get_redis().ping()
        except Exception as exc:  # noqa: BLE001
            out["redis"] = f"down: {type(exc).__name__}"
            out["status"] = "degraded"
        from fastapi.responses import JSONResponse
        return JSONResponse(out, status_code=200 if out["status"] == "ok" else 503)

    @app.get("/health/jobs")
    async def health_jobs():
        """Whether the care jobs are running (the same check as the dashboard banner)."""
        from care import ops
        from utils.db import acquire
        from fastapi.responses import JSONResponse
        async with acquire() as conn:
            report = await ops.status(conn, alert=False)
        return JSONResponse(report, status_code=503 if report["monitoring_paused"] else 200)

    origins = allowed_origins()
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           # X-Idempotency-Key is what the watch's outbox sends with help, falls and answers.
                           allow_headers=["Authorization", "Content-Type", "X-Workspace-ID", "X-Device-Token", "X-Idempotency-Key"])
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_care_app()
