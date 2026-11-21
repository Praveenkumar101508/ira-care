"""A single API error shape, safe to return to an untrusted client.

Today `app/factory.py` catches every unhandled exception and returns
``{"detail": "An internal error occurred. IRA is investigating."}``. That is
safe — it leaks nothing — but it is unactionable: a user reporting it has no
identifier to give support, and support has no way to find the log line.

The contract here keeps the safety and adds the identifier:

    {
      "error_code":   "internal_error",
      "message":      "An internal error occurred.",
      "request_id":   "0f3a…",
      "retryable":    false,
      "safe_details": {...}        # present only when it is safe
    }

`request_id` is the whole point. It matches the `X-Request-ID` response header
and the `request_id` field on every log line for that request, so a user can
paste one string and support can find everything.

── What never crosses this boundary ─────────────────────────────────────────

Stack traces, exception messages from unexpected errors, SQL, internal
hostnames, file paths, secrets, tenant data. The full traceback is logged (and
redacted) internally; the client gets a code and an ID.

The distinction is between *expected* and *unexpected* errors. An expected error
— validation failed, permission denied, rate limited — has a message written by
us for a user to read, so it is returned. An unexpected one has a message
written by Python, or by asyncpg, or by whatever library actually broke, so it
is replaced.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.runtime.request_context import get_request_id

logger = logging.getLogger("ira.errors")


@dataclass(frozen=True)
class ErrorCode:
    """A stable, machine-readable error identity.

    `code` is part of the API contract: clients branch on it, so it must not
    change once published. `message` is for humans and may be reworded.
    """
    code: str
    status: int
    message: str
    retryable: bool = False


# ── The catalogue ────────────────────────────────────────────────────────────

VALIDATION_ERROR = ErrorCode("validation_error", 422, "The request could not be validated.")
BAD_REQUEST = ErrorCode("bad_request", 400, "The request was malformed.")
UNAUTHENTICATED = ErrorCode("unauthenticated", 401, "Authentication is required.")
INVALID_CREDENTIALS = ErrorCode("invalid_credentials", 401, "The credentials were not accepted.")
TOKEN_EXPIRED = ErrorCode("token_expired", 401, "The session has expired. Sign in again.")
TOKEN_REVOKED = ErrorCode("token_revoked", 401, "The session is no longer valid.")
PERMISSION_DENIED = ErrorCode("permission_denied", 403, "This action is not permitted.")
STEP_UP_REQUIRED = ErrorCode("step_up_required", 403, "This action requires re-authentication.")
NOT_FOUND = ErrorCode("not_found", 404, "The requested resource does not exist.")
CONFLICT = ErrorCode("conflict", 409, "The request conflicts with the current state.")
PAYLOAD_TOO_LARGE = ErrorCode("payload_too_large", 413, "The request body is too large.")
RATE_LIMITED = ErrorCode("rate_limited", 429, "Too many requests. Slow down.", retryable=True)
QUOTA_EXCEEDED = ErrorCode("quota_exceeded", 429, "The usage quota for this plan is exhausted.")
INTERNAL_ERROR = ErrorCode("internal_error", 500, "An internal error occurred.")
NOT_IMPLEMENTED = ErrorCode("not_implemented", 501, "This capability is not available.")
DEPENDENCY_UNAVAILABLE = ErrorCode(
    "dependency_unavailable", 503, "A required service is unavailable.", retryable=True)
MODEL_UNAVAILABLE = ErrorCode(
    "model_unavailable", 503, "No approved model is currently available.", retryable=True)
ROUTING_REFUSED = ErrorCode(
    "routing_refused", 503,
    "No model may carry this request under the current privacy policy.")
NOT_READY = ErrorCode("not_ready", 503, "The service is starting up.", retryable=True)
SHUTTING_DOWN = ErrorCode("shutting_down", 503, "The service is shutting down.", retryable=True)
TIMEOUT = ErrorCode("timeout", 504, "The request timed out.", retryable=True)
MODEL_TIMEOUT = ErrorCode(
    "model_timeout", 504,
    "The model did not finish its answer in time. It was not asked again.")

#: `ROUTING_REFUSED` is deliberately **not** retryable. A refusal from the model
#: gateway is a policy decision, not a transient failure, and marking it
#: retryable would invite clients to hammer a request that policy will refuse
#: every time — turning a correct decision into a denial-of-wallet.
#:
#: `MODEL_TIMEOUT` is not retryable either. Sending the same question again
#: asks a model that just ran out of time to write the whole answer again; on
#: a CPU-only machine the streaming chat, which shows the answer as it is
#: written, is the useful next step.

ALL_CODES: tuple[ErrorCode, ...] = tuple(
    v for v in list(globals().values()) if isinstance(v, ErrorCode)
)


@dataclass
class IRAError(Exception):
    """An error with a defined API shape.

    `safe_details` must contain only values safe for an untrusted client:
    field names that failed validation, a retry-after, a quota name. Never a
    value the user supplied back to them unescaped, never anything from another
    tenant.
    """
    error: ErrorCode
    safe_details: dict[str, Any] = field(default_factory=dict)
    log_message: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.log_message or self.error.message)

    @property
    def status_code(self) -> int:
        return self.error.status

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error_code": self.error.code,
            "message": self.error.message,
            "request_id": get_request_id(),
            "retryable": self.error.retryable,
        }
        if self.safe_details:
            body["safe_details"] = self.safe_details
        return body


def error_response(code: ErrorCode, **safe_details: Any) -> dict[str, Any]:
    """Build a response body without raising."""
    return IRAError(code, safe_details=safe_details).to_response()


# ── Handlers ─────────────────────────────────────────────────────────────────

def install_error_handlers(app) -> None:
    """Attach handlers producing the contract above.

    Registered handlers are additive and specific; FastAPI's own `HTTPException`
    handling for routes that have not migrated is preserved by re-shaping rather
    than replacing, so no existing endpoint changes status code or semantics.
    """
    from fastapi import HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _rid(request: Request) -> str:
        """Request ID, preferring request.state over the ContextVar.

        Starlette's ServerErrorMiddleware is installed OUTSIDE
        RequestContextMiddleware, so when an unhandled exception reaches it the
        ContextVar has already been cleared. request.state survives the unwind,
        which is why it is consulted first — otherwise the one response that
        most needs an ID to quote would carry an empty one.
        """
        return getattr(request.state, "request_id", "") or get_request_id()

    def _json(status: int, body: dict[str, Any], request: Request | None = None) -> JSONResponse:
        rid = body.get("request_id") or (_rid(request) if request is not None else "")
        if rid:
            body["request_id"] = rid
        response = JSONResponse(status_code=status, content=body)
        if rid:
            response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(IRAError)
    async def _ira_error(request: Request, exc: IRAError):  # noqa: ARG001
        logger.warning(
            "handled error", extra={"error_code": exc.error.code, "status": exc.status_code}
        )
        return _json(exc.status_code, exc.to_response(), request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):  # noqa: ARG001
        # Field *locations* are safe and are what a client needs. The rejected
        # values are not included — they can contain whatever was submitted.
        fields = []
        for err in exc.errors()[:20]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            if loc:
                fields.append(loc)
        return _json(
            VALIDATION_ERROR.status,
            error_response(VALIDATION_ERROR, fields=fields) if fields
            else error_response(VALIDATION_ERROR),
            request,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):  # noqa: ARG001
        code = _CODE_BY_STATUS.get(exc.status_code)
        body = {
            "error_code": code.code if code else f"http_{exc.status_code}",
            # An HTTPException's detail is written by our own route code, so it
            # is safe to return — that is the difference between this and the
            # unhandled case below.
            "message": str(exc.detail) if exc.detail else (code.message if code else "Request failed."),
            "request_id": _rid(request),
            "retryable": bool(code and code.retryable),
        }
        response = _json(exc.status_code, body, request)
        # Preserve headers the raiser set (WWW-Authenticate, Retry-After).
        for k, v in (getattr(exc, "headers", None) or {}).items():
            response.headers[k] = v
        return response

    from core.interfaces.model_provider import ProviderTimeout

    @app.exception_handler(ProviderTimeout)
    async def _model_timeout(request: Request, exc: ProviderTimeout):
        # Any route that awaited a model answer which did not come. The limit is
        # our own configuration, safe to return; it tells the owner which way
        # to move OLLAMA_READ_TIMEOUT_S.
        logger.warning(
            "model timed out", extra={"error_code": MODEL_TIMEOUT.code, "status": MODEL_TIMEOUT.status}
        )
        details = {"limit_s": exc.limit_s} if exc.limit_s else {}
        return _json(MODEL_TIMEOUT.status, error_response(MODEL_TIMEOUT, **details), request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ARG001
        # Full traceback to the internal log, where the redaction filter runs.
        # Nothing from `exc` reaches the client.
        logger.error("unhandled error", exc_info=True, extra={"error_code": INTERNAL_ERROR.code})
        return _json(INTERNAL_ERROR.status, error_response(INTERNAL_ERROR), request)

    # Keep FastAPI's HTTPException import used, and make the mapping explicit.
    _ = HTTPException


_CODE_BY_STATUS: dict[int, ErrorCode] = {
    400: BAD_REQUEST,
    401: UNAUTHENTICATED,
    403: PERMISSION_DENIED,
    404: NOT_FOUND,
    409: CONFLICT,
    413: PAYLOAD_TOO_LARGE,
    422: VALIDATION_ERROR,
    429: RATE_LIMITED,
    500: INTERNAL_ERROR,
    501: NOT_IMPLEMENTED,
    503: DEPENDENCY_UNAVAILABLE,
    504: TIMEOUT,
}
