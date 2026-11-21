"""Request and correlation IDs, carried without threading them through calls.

Two identifiers, and the distinction matters:

    request_id      unique to this HTTP request. Always generated here.
    correlation_id  shared across every request in one logical operation.
                    Accepted from the caller when present, generated otherwise.

A browser click that fans out to three API calls produces three request IDs and
one correlation ID. Without the second, correlating a user's report ("it failed
around 10:42") to the logs means guessing.

── Why contextvars rather than a parameter ──────────────────────────────────

The alternative is passing a context object through every call site, and this
codebase has roughly 276 call sites still threading a bare username string —
which is exactly the migration that Phase 2 has to do and that nobody enjoys.
A `ContextVar` is per-task and propagates into `asyncio.create_task`, so a
background task started from a request inherits its IDs for free.

── Why the inbound ID is not trusted verbatim ───────────────────────────────

`X-Correlation-ID` is client-supplied, so it is untrusted input that ends up in
log files. Unbounded, it is a log-injection vector (newlines forging log lines)
and a storage-amplification one. It is therefore length-capped and
character-filtered before use, and rejected values are replaced rather than
causing an error — a malformed header should not fail a request.
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"

#: Cap on an accepted inbound correlation ID.
MAX_ID_LENGTH = 128

#: Only these characters survive. Excludes whitespace and control characters,
#: so a correlation ID cannot forge a log line.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % MAX_ID_LENGTH)

_request_id: ContextVar[str] = ContextVar("ira_request_id", default="")
_correlation_id: ContextVar[str] = ContextVar("ira_correlation_id", default="")

#: The principal this request is running as, or None outside a request.
#:
#: ── Why a ContextVar, added in Phase 8 ──────────────────────────────────
#:
#: The model gateway is a chokepoint: it checks a tenant's quota before a call
#: and meters the call after it. `utils/llm.py` is reached from forty-three
#: modules — agents, the overlay, the reasoning backends — none of which has a
#: request object, and threading a principal through all of them is how a
#: chokepoint becomes forty-three call sites that each remember to pass it.
#:
#: Nothing reads this to decide ACCESS. Authorisation stays where it is, in the
#: route dependency that verified the membership; this carries the answer that
#: dependency already reached, so that metering can attribute a call. A read
#: returning None means "no tenant context", and every consumer treats that as
#: unmetered rather than as permission.
_principal: ContextVar[object | None] = ContextVar("ira_principal", default=None)


def new_id() -> str:
    return uuid.uuid4().hex


def sanitize_id(value: str | None) -> str | None:
    """Return a client-supplied ID if it is safe to use, else None."""
    if not value:
        return None
    v = value.strip()
    if not v or len(v) > MAX_ID_LENGTH:
        return None
    return v if _SAFE_ID.match(v) else None


def set_request_id(value: str) -> None:
    _request_id.set(value)


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_request_id() -> str:
    """Current request ID, or "" outside a request (a worker, a test)."""
    return _request_id.get()


def get_correlation_id() -> str:
    return _correlation_id.get()


def bind(request_id: str | None = None, correlation_id: str | None = None) -> tuple[str, str]:
    """Bind both IDs for the current context, generating what is missing."""
    rid = sanitize_id(request_id) or new_id()
    cid = sanitize_id(correlation_id) or rid
    set_request_id(rid)
    set_correlation_id(cid)
    return rid, cid


def set_principal(principal: object | None) -> None:
    """Bind the principal this request is running as.

    Called from the workspace dependency once membership has been verified, so
    that what lands here is an answer authorisation already reached rather than
    a claim the request made.
    """
    _principal.set(principal)


def get_principal() -> object | None:
    """The current principal, or None outside a request.

    None means "no tenant context" — a worker, a private install, a test — and
    every consumer must treat it as unmetered, never as permitted.
    """
    return _principal.get()


def clear() -> None:
    """Reset the bound values. For tests.

    The principal is included deliberately. A test that binds one and does not
    clear it leaves a tenant identity visible to whatever runs next in the same
    task, and metering attributed to the wrong tenant is worse than metering
    nothing.
    """
    _request_id.set("")
    _correlation_id.set("")
    _principal.set(None)


def current_ids() -> dict[str, str]:
    """Both IDs as a dict, omitting empties. For log records."""
    out = {}
    if rid := get_request_id():
        out["request_id"] = rid
    if cid := get_correlation_id():
        out["correlation_id"] = cid
    return out
