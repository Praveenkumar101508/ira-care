"""
HTTP middleware — rate limiting, CORS and the IP blocklist.

`limiter` lives here so both the app factory and the auth routes share one
instance (the same object `main.py` used to define at module scope).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.responses import Response as _Response
from starlette.types import ASGIApp, Receive, Scope, Send

from core.runtime.client_ip import client_ip, rate_limit_key

# ── Rate limiter ───────────────────────────────────────────────────────────────
# P2-03: keyed on the derived client address, not the raw socket peer. Behind a
# load balancer the peer is the balancer, so every client in the world shared
# one bucket. `rate_limit_key` honours X-Forwarded-For only for the configured
# number of trusted hops (IRA_TRUSTED_PROXY_HOPS, default 0 = trust nothing).
limiter = Limiter(key_func=rate_limit_key)


class _IPBlockMiddleware:
    """P6.2: IP blocklist middleware — runs before all routes; blocked IPs get 403.

    A plain ASGI middleware, not Starlette's BaseHTTPMiddleware: that one runs
    each request's body through an anyio task group whose cancellation leaves a
    reference cycle (frames, a CancelledError and its traceback) behind on
    every request with a body. Those cycles wait for the collector's rare full
    collection, so the process's memory crept up between them (found by the
    wall-clock soak). Non-HTTP scopes pass straight through, as before.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # P2-03: the same derivation the limiter uses. Two consumers of "the
        # client IP" that disagreed would give two different answers to one
        # question — a blocked address evading the block, or a rate-limit
        # identity nothing else recognises.
        address = client_ip(Request(scope))
        if address and address != "unknown":
            from utils.playbooks import is_ip_blocked
            if await is_ip_blocked(address):
                response = _Response(
                    content='{"detail":"Access denied"}',
                    status_code=403,
                    media_type="application/json",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_middleware(app: FastAPI, cfg) -> None:
    """Attach rate limiting, CORS and the IP blocklist to the app."""
    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS — only allow localhost in development; production uses domain only
    allowed_origins = [f"https://{cfg.ira_domain}"]
    if cfg.dev_mode:
        allowed_origins += ["http://localhost:3000", "http://127.0.0.1:3000"]
    # Phone access over Tailscale Serve (HTTPS) — allow the *.ts.net origin so the
    # mobile PWA's mic (getUserMedia needs a secure context) and API calls work.
    if getattr(cfg, "ira_ts_host", ""):
        allowed_origins.append(f"https://{cfg.ira_ts_host}")
    # Fix #45: PATCH and PUT were missing — endpoints that use them (architect
    # apply, profile updates) would fail CORS preflight in the browser. OPTIONS
    # is implied by CORSMiddleware but listed explicitly for clarity.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.add_middleware(_IPBlockMiddleware)

    # PR #67: delegated accounts are default-denied everywhere except a small
    # scope-mapped allowlist. The primary owner is unaffected. Added after the
    # IP blocklist so blocking still runs first (middleware runs LIFO).
    from security.scope_enforcer import DelegatedScopeMiddleware
    app.add_middleware(DelegatedScopeMiddleware)
