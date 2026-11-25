"""A ceiling on the size of a request body for the IRA CARE server (security review S10).

Without it, any route that takes a body, including the two that need no sign-in (sign-up and
pairing), read the whole body into memory and parsed it before a schema or a rate limit could
refuse it: one request of a few gigabytes could exhaust the server's memory. The schemas bound
every field, but only after the body has been read.

The largest body a care route needs is a research heart-rate recording (care/schemas.py,
`RESEARCH_MAX_SAMPLES`: at most about 12 MB of JSON), so the default ceiling is 16 MiB.
`IRA_CARE_MAX_REQUEST_BYTES` changes it for a deployment that needs another value.

A body that declares a larger `Content-Length` is refused with 413 before any of it is read.
A body sent without a length (chunked) is read here, counted as it arrives, and refused with
413 as soon as it passes the ceiling; under it, the app is given the body it would have read.
"""
from __future__ import annotations

import os

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
ENV = "IRA_CARE_MAX_REQUEST_BYTES"
DETAIL = "The request is too large."


def max_request_bytes() -> int:
    raw = (os.getenv(ENV) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: refuse HTTP request bodies over ``max_bytes`` with 413."""

    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else max_request_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                break
        if declared is not None and (declared < 0 or declared > self.max_bytes):
            await _refuse(send, 413 if declared > self.max_bytes else 400)
            return

        if declared is not None:
            # The server reads no more than the declared length (h11 and httptools enforce it).
            await self.app(scope, receive, send)
            return

        # No declared length (chunked): read it here, counting, and refuse it the moment it
        # passes the ceiling, before the app runs. What is held is never more than the ceiling.
        # Raising from inside the app's receive instead would be wrapped by Starlette's
        # middleware task groups and answered 400, not 413.
        parts: list[bytes] = []
        seen = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":       # the client went away
                return
            chunk = message.get("body", b"")
            seen += len(chunk)
            if seen > self.max_bytes:
                await _refuse(send, 413)
                return
            parts.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(parts)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()                        # later: only a disconnect

        await self.app(scope, replay, send)


async def _refuse(send, status: int) -> None:
    body = (b'{"detail":"' + DETAIL.encode() + b'"}') if status == 413 else b'{"detail":"Bad Content-Length."}'
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
