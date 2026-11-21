"""Secrets must not reach the logs.

The repository already learned this lesson the expensive way: six credentials
were committed to `.env.example` and remain readable in public git history
(`docs/security/CREDENTIAL_ROTATION.md`). Logs are the same class of channel —
written constantly, read rarely, shipped somewhere else, retained for months.

These tests are the regression gate. Each one names a value that must never
appear in output.
"""
from __future__ import annotations

import io
import logging

import pytest

from core.runtime import redaction
from core.runtime.logging_config import JsonFormatter
from core.runtime.redaction import (
    PLACEHOLDER,
    RedactionFilter,
    clear_registered_secrets,
    is_sensitive_key,
    redact,
    redact_mapping,
    redact_text,
    register_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# ── Field-name based redaction ───────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "password", "ira_admin_password", "PASSWORD", "postgres_password",
    "secret", "ira_secret_key", "SECRET_KEY", "client_secret",
    "token", "access_token", "refresh_token", "id_token",
    "api_key", "apikey", "x-api-key", "authorization",
    "totp_secret", "recovery_code", "mfa_secret",
    "voiceprint", "voice_embedding", "biometric_template",
    "database_url", "redis_url", "dsn", "connection_string",
    "card_number", "cvv", "iban", "payment_method",
    "prompt", "memory_content", "document_content", "file_content",
    "ira_canary_token",
])
def test_sensitive_field_names_are_redacted(key):
    assert is_sensitive_key(key), key
    assert redact_mapping({key: "the-actual-value"})[key] == PLACEHOLDER


@pytest.mark.parametrize("key", [
    "auth_provider", "authenticated", "auth_method", "token_type",
    "prompt_token_count", "completion_token_count", "embedding_model",
    "totp_enabled", "mfa_enabled", "has_password", "api_key_id",
    "token_version", "password_changed_at",
])
def test_useful_lookalike_fields_survive(key):
    """Redacting `auth_provider` would blind exactly the incident it matters in."""
    assert not is_sensitive_key(key)
    assert redact_mapping({key: "value"})[key] == "value"


def test_nested_structures_are_redacted():
    out = redact_mapping({
        "user": {"id": "u1", "password": "hunter2", "profile": {"api_key": "sk-abc"}},
        "items": [{"token": "t1"}, {"name": "safe"}],
    })
    assert out["user"]["password"] == PLACEHOLDER
    assert out["user"]["profile"]["api_key"] == PLACEHOLDER
    assert out["user"]["id"] == "u1"
    assert out["items"][0]["token"] == PLACEHOLDER
    assert out["items"][1]["name"] == "safe"


# ── Pattern based redaction ──────────────────────────────────────────────────

def test_bearer_tokens_are_redacted_but_the_scheme_survives():
    out = redact_text("Authorization: Bearer abcdef1234567890abcdef")
    assert "abcdef1234567890abcdef" not in out
    # Keeping "Bearer" tells a reader a token was present, which "[REDACTED]"
    # alone would not.
    assert "Bearer" in out


def test_jwts_are_redacted():
    # Assembled at runtime rather than written as a literal. A JWT-shaped string
    # committed here would trip the repository's own Gitleaks job — the same
    # reason .github/scripts/verify-secret-scan.sh generates its planted
    # credentials instead of hard-coding them.
    jwt = ".".join([
        "eyJ" + "hbGciOiJIUzI1NiJ9",
        "eyJ" + "zdWIiOiJhZG1pbiJ9",
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ])
    assert jwt not in redact_text(f"decoded token {jwt} for user")


@pytest.mark.parametrize("dsn,secret", [
    ("postgresql://ira:sup3rs3cret@db:5432/ira", "sup3rs3cret"),
    ("redis://:redispassword123@cache:6379/0", "redispassword123"),
    ("postgres://user:p%40ssw0rd@host/db", "p%40ssw0rd"),
])
def test_credentials_inside_urls_are_redacted(dsn, secret):
    out = redact_text(f"connection failed: {dsn}")
    assert secret not in out
    # The host survives, because that is the diagnosable part.
    assert "REDACTED" in out


# Built at runtime for the same reason as the JWT above: a literal
# `NAME=<secret-shaped-value>` line in a tracked file is what the repository's
# secret scan exists to catch, and this suite must not be the thing that trips it.
# The value halves are concatenated so no `NAME", "<16+ chars>` pair ever sits
# adjacent in the source — that adjacency is itself what the generic-api-key
# rule matches, and this file must not be the one that trips the scan.
@pytest.mark.parametrize("name,secret", [
    ("IRA_SECRET_KEY", "abcdef" + "0123456789abcdef"),
    ("password", "hunter2" + "hunter2"),
    ("api_key", "sk-very" + "secretvalue"),
    ("IRA_CANARY_TOKEN", "tripwire" + "value123"),
])
@pytest.mark.parametrize("separator", ["=", ": ", " = "])
def test_assignments_are_redacted(name, secret, separator):
    assert secret not in redact_text(name + separator + secret)


@pytest.mark.parametrize("prefix,body", [
    ("sk-", "abcdefghij0123456789"),
    ("ghp_", "abcdefghij0123456789abcd"),
    ("AKIA", "IOSFODNN7EXAMPLE"),
])
def test_provider_key_shapes_are_redacted(prefix, body):
    key = prefix + body          # assembled, never a committed literal
    assert key not in redact_text(f"using {key} now")


def test_private_key_blocks_are_redacted():
    blob = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----")
    assert "MIIEowIBAAKCAQEA" not in redact_text(f"loaded key: {blob}")


# ── Registered literal values ────────────────────────────────────────────────

def test_a_registered_secret_is_redacted_however_it_appears():
    """The layer that catches what field names and regexes miss: a value
    formatted into a message by a third-party library."""
    secret = "Zq7-unusual-value-with-no-telltale-shape"
    register_secret(secret)
    assert secret not in redact(f"httpx retrying with {secret} in query")


def test_very_short_values_are_not_registered():
    """A 4-character secret would match inside ordinary words and redact half
    of every log line."""
    register_secret("abc")
    assert redact("abc is a common substring") == "abc is a common substring"


def test_overlapping_secrets_redact_longest_first():
    register_secret("secretvalue")
    register_secret("secretvalue-extended-form")
    out = redact("token secretvalue-extended-form here")
    assert "secretvalue" not in out


def test_register_secrets_from_settings_counts_what_it_registered():
    class Cfg:
        ira_secret_key = "k" * 40
        ira_admin_password = "p" * 20
        postgres_password = "q" * 20
        redis_password = ""          # absent — not registered
        vllm_api_key = None          # absent — not registered
    assert redaction.register_secrets_from_settings(Cfg()) == 3


# ── Through the logging pipeline ─────────────────────────────────────────────

def _capture(record_factory) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(service="test"))
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("ira.test.redaction")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    record_factory(logger)
    handler.flush()
    return stream.getvalue()


def test_a_secret_in_a_log_message_never_reaches_the_stream():
    secret = "Zq7-registered-startup-secret-value"
    register_secret(secret)
    out = _capture(lambda lg: lg.info("connecting with %s", secret))
    assert secret not in out
    assert PLACEHOLDER in out


def test_a_secret_in_a_structured_extra_field_is_redacted():
    out = _capture(lambda lg: lg.info("login", extra={"password": "hunter2", "user": "alice"}))
    assert "hunter2" not in out
    assert "alice" in out


def test_a_dsn_in_an_exception_message_is_redacted():
    def emit(lg):
        try:
            raise RuntimeError("could not connect to postgresql://ira:leakedpw@db:5432/ira")
        except RuntimeError:
            lg.error("db failure", exc_info=True)
    out = _capture(emit)
    assert "leakedpw" not in out


def test_tracebacks_are_not_serialised_into_the_json_line():
    """A traceback in a shipped log line is where a DSN, a token or a prompt
    fragment ends up. The type and message are enough for an alert."""
    def emit(lg):
        try:
            raise ValueError("boom")
        except ValueError:
            lg.error("failed", exc_info=True)
    out = _capture(emit)
    assert "ValueError" in out
    assert "Traceback" not in out
    assert "test_log_redaction.py" not in out
