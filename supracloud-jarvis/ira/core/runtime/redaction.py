"""Keep secrets out of logs.

This module exists because of a finding in this repository's own history: six
credentials were committed to `.env.example` and are still readable in public
git history (`docs/security/CREDENTIAL_ROTATION.md`). The lesson generalises —
a credential leaks through whichever channel nobody was watching, and logs are
the channel nobody watches.

── Two layers, because one is not enough ────────────────────────────────────

1. **Structural** — a value known to be a secret is replaced before it is ever
   formatted into a message. This is exact and cheap, and it is what
   :func:`redact_mapping` does for configuration dumps and structured fields.

2. **Textual** — a regex pass over the finished message, catching secrets that
   arrived inside a string nobody thought about: an exception message
   containing a DSN, a repr of a request body, a traceback line.

Layer 2 alone would be fragile (regexes miss things). Layer 1 alone would be
insufficient (it only sees values it was told about). Together they cover the
realistic paths.

── What is never logged ─────────────────────────────────────────────────────

Passwords, access tokens, refresh tokens, API keys, raw voiceprints, TOTP
secrets, encryption keys, full private prompts, full memories, uploaded
document content, payment information.

Note the last four: they are not credentials, they are *customer data*. A log
line containing a user's full prompt is a privacy incident even though nothing
in it is a secret, and it is the failure mode a cloud platform is most likely to
walk into by accident.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

#: What replaces a redacted value. Fixed-width and obviously deliberate, so a
#: reader can tell redaction happened rather than wondering if the field was
#: empty.
PLACEHOLDER = "[REDACTED]"

# ── Field names whose values are never logged ────────────────────────────────
#
# Matched case-insensitively as a *substring* of the key, so `ira_secret_key`,
# `SECRET_KEY` and `x-api-key` all match. Over-matching is the safe direction:
# redacting a harmless field costs a debugging session, leaking a real one costs
# a rotation and a disclosure.

SENSITIVE_KEY_PARTS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "credential", "private_key", "privatekey", "session_key", "cookie",
    "totp", "otp", "mfa", "recovery_code", "canary",
    "voiceprint", "voice_embedding", "embedding", "biometric",
    "dsn", "database_url", "redis_url", "connection_string",
    "card", "cvv", "iban", "account_number", "payment",
    "prompt", "memory_content", "document_content", "file_content",
})

#: Keys that contain a sensitive substring but are safe and useful to log.
#: Without this, `auth` would redact `auth_provider` and `authenticated`, which
#: are exactly the fields you want during an auth incident.
SAFE_KEY_EXCEPTIONS: frozenset[str] = frozenset({
    "auth_provider", "authenticated", "auth_method", "token_type",
    "prompt_token_count", "completion_token_count", "embedding_model",
    "password_changed_at", "totp_enabled", "mfa_enabled", "secret_rotated_at",
    "has_password", "api_key_id", "api_key_prefix", "token_version",
})


def is_sensitive_key(key: str) -> bool:
    """True when a field name means its value must not be logged.

    Separators are normalised to `_` first, so the header form (`x-api-key`),
    the setting form (`api_key`) and the JSON form (`apiKey` lowercased to
    `apikey`) all match the same entry.
    """
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    if k in SAFE_KEY_EXCEPTIONS:
        return False
    return any(part in k for part in SENSITIVE_KEY_PARTS)


# ── Textual patterns ─────────────────────────────────────────────────────────
#
# Each keeps the identifying prefix and redacts only the value, so a log line
# stays diagnosable: "Authorization: [REDACTED]" tells you a header was present,
# which "[REDACTED]" alone would not.

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer / Basic / Token authorization headers
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9\-._~+/=]{8,}", re.I), r"\1 " + PLACEHOLDER),
    # JWTs anywhere — three base64url segments
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"), PLACEHOLDER),
    # Credentials inside a URL: postgres://user:pw@host, redis://:pw@host
    (re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^:@/\s]*):(?P<pw>[^@/\s]+)@"),
     r"\g<scheme>\g<user>:" + PLACEHOLDER + "@"),
    # key=value and key: value assignments for sensitive-looking names
    (re.compile(
        r"(?i)\b([A-Za-z0-9_.\-]*(?:password|passwd|secret|token|api[_-]?key|"
        r"credential|private[_-]?key|canary|totp)[A-Za-z0-9_.\-]*)"
        r"(\s*[=:]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"),
     r"\1\2" + PLACEHOLDER),
    # Common provider key shapes
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), PLACEHOLDER),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), PLACEHOLDER),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), PLACEHOLDER),
)


def redact_text(text: str) -> str:
    """Redact secret-shaped substrings from a finished log message."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_value(key: str, value: Any) -> Any:
    """Redact one field, recursing into nested structures."""
    if is_sensitive_key(key):
        return PLACEHOLDER
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(key, v) for v in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a mapping by field name, then by content."""
    return {k: redact_value(str(k), v) for k, v in data.items()}


# ── Registered literal secrets ───────────────────────────────────────────────
#
# The strongest layer, and the one that catches what the other two miss. At
# startup the known secret *values* are registered here; any log line containing
# one is redacted regardless of how it got there — a value formatted into a
# message by a third-party library, say, where neither the field name nor a
# regex would have caught it.

_LITERALS: set[str] = set()

#: Short values are not registered: a 4-character secret would match inside
#: ordinary words and redact half of every log line. Anything this short is
#: already a configuration problem (see environment.MIN_SECRET_KEY_LENGTH).
_MIN_LITERAL_LENGTH = 8


def register_secret(value: str | None) -> None:
    """Register a known secret value for unconditional redaction."""
    if value and len(value) >= _MIN_LITERAL_LENGTH:
        _LITERALS.add(value)


def clear_registered_secrets() -> None:
    """Drop all registered literals. For tests."""
    _LITERALS.clear()


def redact_registered(text: str) -> str:
    if not text or not _LITERALS:
        return text
    out = text
    # Longest first, so a secret that contains another is redacted whole.
    for literal in sorted(_LITERALS, key=len, reverse=True):
        if literal in out:
            out = out.replace(literal, PLACEHOLDER)
    return out


def redact(text: str) -> str:
    """Full redaction: registered literals, then patterns."""
    return redact_text(redact_registered(text))


class RedactionFilter(logging.Filter):
    """Logging filter that redacts every record passing through a handler.

    Attached to the handler rather than the logger, so it applies to records
    from third-party libraries too — which is the point. Our own code can be
    careful; `httpx` logging a URL with credentials in it cannot.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redact_arg(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(_redact_arg(a) for a in record.args)
        except Exception:  # noqa: BLE001
            # A redaction failure must never drop a log record or crash a
            # request. Falling through un-redacted is the lesser evil only
            # because the structured path (redact_mapping) has already run on
            # anything we control; this branch exists so a malformed record
            # cannot take down logging itself.
            pass
        return True


def _redact_arg(value: Any) -> Any:
    if isinstance(value, BaseException) and not exception_text_allowed():
        # An exception passed as a log argument (`log.warning("failed: %s", exc)`) prints its
        # message, and a message is where a driver quotes the value it rejected: asyncpg's
        # DataError names the argument, a model client echoes the request, an SMTP library
        # the recipient. In a care product that value is a person's words, medication or
        # address. The type is what an operator needs to triage; the text stays out.
        return type(value).__name__
    return redact(value) if isinstance(value, str) else value


EXCEPTION_TEXT_ENV = "IRA_LOG_EXCEPTION_TEXT"


def exception_text_allowed() -> bool:
    """Exception messages in logs: only when a developer asks by name, never in a
    production-like environment (where the setting is ignored)."""
    import os
    if (os.getenv(EXCEPTION_TEXT_ENV) or "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        from core.runtime.environment import get_environment
        return not get_environment().is_production_like
    except Exception:  # noqa: BLE001 - unsure means no
        return False


def register_secrets_from_settings(cfg) -> int:
    """Register every known secret value from settings. Returns how many.

    Called once at startup. The count is safe to log; the values never are.
    """
    names = (
        "ira_secret_key", "ira_admin_password", "postgres_password",
        "redis_password", "vllm_api_key", "ira_cortex_key", "ira_canary_token",
        "webhook_secret", "livekit_api_secret", "livekit_api_key",
    )
    before = len(_LITERALS)
    for name in names:
        register_secret(getattr(cfg, name, None))
    return len(_LITERALS) - before
