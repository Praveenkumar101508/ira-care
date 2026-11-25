"""Structured JSON logging with request correlation and secret redaction.

`main.py` configures logging with `basicConfig` and a human-readable format.
That is right for a developer watching a terminal and useless for a cloud
deployment, where logs are shipped to an aggregator that parses JSON and where
"which request was that?" has to be answerable from the line itself.

This module installs a JSON formatter that carries, on every line:

    timestamp · level · logger · message · service · environment · version
    git_commit · request_id · correlation_id

plus route, status code and duration for request logs.

── Ordering matters ─────────────────────────────────────────────────────────

The redaction filter is attached to the **handler**, not to a logger. Records
from `httpx`, `asyncpg` and `uvicorn` never pass through our loggers, and those
are precisely the libraries that log a URL with credentials in it. Filtering at
the handler catches every record that is about to be written.

── Text mode is kept ────────────────────────────────────────────────────────

`IRA_LOG_FORMAT=text` restores the human-readable format, still redacted. A
developer should not have to read JSON in a terminal, and forcing them to
guarantees somebody disables structured logging in production to make local work
bearable.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from core.runtime.redaction import RedactionFilter, exception_text_allowed, redact, redact_mapping
from core.runtime.request_context import get_correlation_id, get_request_id

#: Attributes present on every LogRecord. Anything else a caller attached via
#: `extra=` is treated as a structured field and included.
_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with build and request context attached."""

    def __init__(self, *, service: str = "ira-api", include_source: bool = False) -> None:
        super().__init__()
        self._service = service
        self._include_source = include_source
        self._static: dict[str, str] = {}

    def _static_fields(self) -> dict[str, str]:
        # Resolved lazily: at import time the settings may not exist yet, and a
        # formatter that raises during configuration would take down logging.
        if not self._static:
            try:
                from core.runtime.build_info import get_build_info
                info = get_build_info(self._service)
                self._static = {
                    "service": info.service,
                    "environment": info.environment,
                    "version": info.version,
                    "git_commit": info.git_commit_short,
                }
            except Exception:  # noqa: BLE001
                self._static = {"service": self._service}
        return self._static

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(self._static_fields())

        if rid := get_request_id():
            payload["request_id"] = rid
        if cid := get_correlation_id():
            payload["correlation_id"] = cid

        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        }
        if extras:
            payload.update(redact_mapping(extras))

        if self._include_source:
            payload["source"] = f"{record.module}:{record.lineno}"

        if record.exc_info:
            # The exception type, never the traceback, and not the message either: a
            # message is where a driver quotes the value it refused (a person's words, a
            # medication, an address) and where a DSN or token ends up. The type is what an
            # alert needs. A developer can ask for the text locally (IRA_LOG_EXCEPTION_TEXT).
            exc_type, exc_value, _ = record.exc_info
            payload["exc_type"] = getattr(exc_type, "__name__", str(exc_type))
            if exception_text_allowed():
                payload["exc_message"] = redact(str(exc_value))[:512]

        # `default=str` keeps a non-serialisable extra from raising inside the
        # formatter, which would lose the record entirely.
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable, with the request ID appended when there is one."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)s %(name)s — %(message)s")

    def formatException(self, ei) -> str:  # noqa: N802 - logging's name
        """The traceback's frames, with each exception reduced to its type.

        The frames (file, line, code) are what a developer needs to find the fault; the
        message is where the rejected value is quoted. Chained causes are kept, the same way.
        """
        if exception_text_allowed():
            return redact(super().formatException(ei))
        import traceback
        chain: list[BaseException] = []
        value = ei[1]
        while value is not None and value not in chain:
            chain.append(value)
            value = value.__cause__ or (None if value.__suppress_context__ else value.__context__)
        parts = []
        for i, exc in enumerate(reversed(chain)):
            tb = exc.__traceback__ if exc.__traceback__ is not None else (ei[2] if exc is ei[1] else None)
            frames = "".join(traceback.format_list(traceback.extract_tb(tb))) if tb is not None else ""
            parts.append(f"Traceback (most recent call last):\n{frames}{type(exc).__name__} (message withheld)")
        return "\n\nDuring handling of the above, another exception occurred:\n\n".join(parts)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if rid := get_request_id():
            base = f"{base}  [request_id={rid}]"
        return base


def log_format() -> str:
    """`json` or `text`. Defaults to json in production-like environments."""
    explicit = (os.getenv("IRA_LOG_FORMAT") or "").strip().lower()
    if explicit in ("json", "text"):
        return explicit
    try:
        from core.runtime.environment import get_environment
        return "json" if get_environment().is_production_like else "text"
    except Exception:  # noqa: BLE001
        return "text"


def log_level() -> int:
    name = (os.getenv("IRA_LOG_LEVEL") or "INFO").strip().upper()
    return getattr(logging, name, logging.INFO)


# ── Third-party transport loggers, floored at INFO ───────────────────────────
#
# Redaction is two layers: structural (values we know are secret) and textual (a
# regex over the finished message). Neither sees a secret it was never told
# about, arriving in a shape nobody anticipated — and a third-party HTTP
# transport at DEBUG is exactly that shape.
#
# Two measurements, not two guesses:
#
#   * P4-10 found botocore logging the decrypted tenant KEK at DEBUG. A KMS
#     `Decrypt` response body is `{"Plaintext": "<the key>"}`, and it went
#     straight into the log.
#
#   * P4-16 found `httpcore.http11` logging complete RESPONSE headers at DEBUG.
#     Measured with a server that returned `Set-Cookie: session=…` and
#     `X-Api-Key: …`: both appeared verbatim in the output, past the redaction
#     filter, because the filter matches field *names* in structured extras and
#     regexes over the message — and this arrives as a repr of a header tuple
#     list.
#
# So `IRA_LOG_LEVEL=DEBUG` — a thing an operator does while chasing a bug —
# turned two third-party libraries into a secret sink. The floor makes DEBUG on
# IRA's own loggers safe to use, which is the point: a control an operator has
# to remember to apply is a control that is not applied at 3am.
#
# `IRA_LOG_THIRD_PARTY_DEBUG=true` lifts it, deliberately and by name. Debugging
# a transport is a real need; doing it by accident is not.
THIRD_PARTY_FLOOR: tuple[str, ...] = (
    "httpcore", "httpcore.http11", "httpcore.http2", "httpcore.connection",
    "httpx",
    "botocore", "botocore.parsers", "botocore.endpoint",
    "botocore.httpsession", "botocore.hooks",
    "urllib3", "urllib3.connectionpool",
    "aiohttp.client",
)

THIRD_PARTY_DEBUG_ENV = "IRA_LOG_THIRD_PARTY_DEBUG"


def third_party_debug_allowed() -> bool:
    return (os.getenv(THIRD_PARTY_DEBUG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def _floor_third_party_loggers() -> None:
    if third_party_debug_allowed():
        return
    for name in THIRD_PARTY_FLOOR:
        lg = logging.getLogger(name)
        # Only ever raises the floor. A library already pinned to WARNING by
        # something else keeps its own, stricter setting.
        if lg.level == logging.NOTSET or lg.level < logging.INFO:
            lg.setLevel(logging.INFO)


def configure_logging(service: str = "ira-api", *, force: bool = True) -> logging.Handler:
    """Install the structured handler on the root logger. Returns the handler.

    Idempotent: existing handlers are removed first, so calling it twice does
    not double every line.
    """
    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(service=service) if log_format() == "json" else TextFormatter()
    )
    # On the handler, so third-party records are redacted too.
    handler.addFilter(RedactionFilter())

    root.addHandler(handler)
    root.setLevel(log_level())

    # uvicorn installs its own handlers; let them propagate to ours instead so
    # access logs are redacted and structured like everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    _floor_third_party_loggers()

    return handler
