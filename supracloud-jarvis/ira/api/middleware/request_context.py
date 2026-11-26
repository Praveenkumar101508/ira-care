"""Middleware binding request/correlation IDs and emitting the access log.

Installed outermost so every other middleware, route and exception handler runs
with the IDs already bound — including the error handlers, which put
`request_id` into the response body.

The access log line it emits carries route, status, duration and both IDs. It
uses the URL *path template* (`/api/v1/memory/{id}`) rather than the resolved
path where available, because logging the resolved path puts identifiers into
log storage and makes the lines unaggregatable.
"""
from __future__ import annotations

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.runtime.request_context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    bind,
    clear,
)

logger = logging.getLogger("ira.access")

#: Probes are excluded from the access log. A readiness check every second from
#: each of N replicas would otherwise be most of the log volume, and would bury
#: the requests anyone cares about.
_QUIET_PATHS = frozenset({"/livez", "/readyz", "/health", "/metrics"})


class RequestContextMiddleware:
    """A plain ASGI middleware, not Starlette's BaseHTTPMiddleware: that one runs
    each request's body through an anyio task group whose cancellation leaves a
    reference cycle (frames, a CancelledError and its traceback) behind on
    every request with a body, and those cycles wait for the collector's rare
    full collection (found by the wall-clock soak). Running in the request's
    own task also means the IDs bound here are the ones every later middleware
    and the route see, with no copied context in between.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request_id, correlation_id = bind(
            request.headers.get(REQUEST_ID_HEADER),
            request.headers.get(CORRELATION_ID_HEADER),
        )
        # Also stashed on request.state, and this is load-bearing rather than
        # belt-and-braces. Starlette's ServerErrorMiddleware — which handles an
        # unhandled exception — is installed OUTSIDE this middleware, so by the
        # time it runs, the `finally` below has already cleared the ContextVar.
        # Reading the ID from the contextvar there yields "", precisely on the
        # 500 response where the ID matters most. request.state survives, so the
        # error handlers read from it first.
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        started = time.perf_counter()
        status = 500

        async def send_with_ids(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                headers[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_ids)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            path = request.url.path
            if path not in _QUIET_PATHS:
                route = scope.get("route")
                logger.info(
                    "request",
                    extra={
                        "method": request.method,
                        # Template where the router resolved one, else the raw
                        # path — never the query string, which carries values.
                        "route": getattr(route, "path", None) or path,
                        "status_code": status,
                        "duration_ms": duration_ms,
                    },
                )
            # The IDs are bound in the request's task; clear them so none can
            # leak into an unrelated log line written later in the same task.
            clear()
