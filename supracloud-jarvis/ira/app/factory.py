"""
App factory — assembles the FastAPI application.

Pieces: lifespan (app.lifespan), middleware (app.middleware), auth endpoints
(auth.routes) and all routers (app.routers). Behavior is identical to the old
inline main.py; only the layout changed.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import get_settings

from app.lifespan import lifespan
from app.middleware import install_middleware, limiter
from app.routers import register_routers
from auth.routes import register_auth_routes

logger = logging.getLogger("ira")


def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="SupraCloud IRA",
        description="Private sovereign AI assistant — fully self-hosted.",
        version=cfg.ira_version,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    install_middleware(app, cfg)
    register_auth_routes(app, limiter)
    register_routers(app)

    # ── Request/correlation IDs ───────────────────────────────────────────────
    # Added LAST so it runs FIRST — Starlette runs middleware in reverse
    # registration order. Every other middleware, route and exception handler
    # therefore executes with both IDs already bound, which is what lets the
    # error handlers put `request_id` into the response body.
    from api.middleware.request_context import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware)

    # ── Error contract ────────────────────────────────────────────────────────
    # Replaces the previous bare `{"detail": ...}` 500 handler. Same safety —
    # nothing from the exception reaches the client — plus the request ID, so a
    # user can quote one string and support can find the log line.
    from core.runtime.errors import install_error_handlers
    install_error_handlers(app)

    _ = (Request, JSONResponse, logger)  # retained for import compatibility

    return app
