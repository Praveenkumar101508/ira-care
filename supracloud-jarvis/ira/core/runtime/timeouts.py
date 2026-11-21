"""Timeout, retry and size defaults — in one place, with reasons.

Every value here was previously either absent or inline at a call site. Both are
problems in a cloud deployment: an absent timeout means a hung dependency holds
a worker until the load balancer gives up, and an inline one means nobody can
answer "what is our database timeout?" without grepping.

── Retry policy, stated once ────────────────────────────────────────────────

Retries are **off by default** and permitted only for operations that are
idempotent by construction: a read, a health probe, a GET. `MAX_RETRIES` is 2,
meaning three attempts total, with full jitter.

A non-idempotent operation — anything that spends money, sends a message,
creates a record or calls a tool — **must not** be retried without an
idempotency key. Retrying a failed "send email" is how one message becomes
three. `is_retryable_operation` exists so this is a decision the code makes
explicitly rather than a default it inherits.

── Why full jitter ──────────────────────────────────────────────────────────

Exponential backoff without jitter synchronises clients: every instance that
failed at T retries at T+1, T+2, T+4 together, and the dependency that was
recovering is knocked over by the retry storm. Full jitter — a uniform draw from
[0, backoff] — spreads them.

── Inference timeouts are not request timeouts ──────────────────────────────

A local 14B model producing a long answer legitimately takes minutes.
`OLLAMA_INFERENCE_TIMEOUT` is therefore much larger than `API_REQUEST_TIMEOUT`,
and streaming responses are exempt from the request timeout — the first token
matters, the last does not. `OLLAMA_CONNECT_TIMEOUT` stays small: failing to
*connect* is immediate and should not wait.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

# ── HTTP ─────────────────────────────────────────────────────────────────────

#: Non-streaming API request budget. Beyond this the client has given up.
API_REQUEST_TIMEOUT = 60.0

#: Streaming responses are exempt: a long generation is not a stuck request.
API_STREAMING_TIMEOUT: float | None = None

#: Outbound HTTP to anything that is not a model runtime.
EXTERNAL_HTTP_TIMEOUT = 15.0
EXTERNAL_HTTP_CONNECT_TIMEOUT = 5.0

# ── Data stores ──────────────────────────────────────────────────────────────

DB_CONNECT_TIMEOUT = 10.0
#: Statement timeout for ordinary queries. Set per-transaction, not globally:
#: migrations and backfills legitimately exceed it and set their own.
DB_STATEMENT_TIMEOUT = 30.0
DB_POOL_ACQUIRE_TIMEOUT = 10.0

REDIS_CONNECT_TIMEOUT = 5.0
REDIS_OPERATION_TIMEOUT = 5.0

# ── Model runtimes ───────────────────────────────────────────────────────────

OLLAMA_CONNECT_TIMEOUT = 10.0
OLLAMA_INFERENCE_TIMEOUT = 300.0
#: A single health probe must be fast — readiness depends on it.
OLLAMA_HEALTH_TIMEOUT = 5.0

VLLM_CONNECT_TIMEOUT = 10.0
VLLM_INFERENCE_TIMEOUT = 300.0
VLLM_HEALTH_TIMEOUT = 5.0

# ── Background work ──────────────────────────────────────────────────────────

BACKGROUND_JOB_TIMEOUT = 600.0
#: Bounded shutdown. Longer than this and an orchestrator SIGKILLs us anyway,
#: so the value is chosen to stay inside a typical 30s grace period.
SHUTDOWN_TIMEOUT = 25.0
SHUTDOWN_TASK_TIMEOUT = 10.0

# ── Retry ────────────────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.25
RETRY_MAX_DELAY = 8.0

#: Operations safe to retry without an idempotency key.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# ── Sizes ────────────────────────────────────────────────────────────────────

MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024        # 10 MiB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024              # 50 MiB
MAX_MODEL_CONTEXT_TOKENS = 32_768
MAX_MODEL_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = MAX_RETRIES
    base_delay: float = RETRY_BASE_DELAY
    max_delay: float = RETRY_MAX_DELAY
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Backoff before `attempt` (1-based). Full jitter when enabled."""
        if attempt < 1:
            return 0.0
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return random.uniform(0.0, backoff) if self.jitter else backoff


DEFAULT_RETRY_POLICY = RetryPolicy()

#: Never retried automatically. Each either spends money, has an external
#: side-effect, or mutates state that a second attempt would duplicate.
NON_IDEMPOTENT_OPERATIONS = frozenset({
    "model_inference", "tool_execution", "connector_write", "email_send",
    "calendar_create", "payment", "webhook_delivery", "data_export",
    "account_deletion", "key_rotation",
})


def is_retryable_operation(operation: str) -> bool:
    """False for anything whose second attempt could duplicate an effect."""
    return operation not in NON_IDEMPOTENT_OPERATIONS


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def api_request_timeout() -> float:
    return _float_env("IRA_API_REQUEST_TIMEOUT", API_REQUEST_TIMEOUT)


def shutdown_timeout() -> float:
    return _float_env("IRA_SHUTDOWN_TIMEOUT", SHUTDOWN_TIMEOUT)


def max_request_body_bytes() -> int:
    return int(_float_env("IRA_MAX_REQUEST_BODY_BYTES", MAX_REQUEST_BODY_BYTES))


def as_dict() -> dict[str, float | int | None]:
    """All effective values. For the configuration matrix and health detail."""
    return {
        "api_request_timeout": api_request_timeout(),
        "api_streaming_timeout": API_STREAMING_TIMEOUT,
        "external_http_timeout": EXTERNAL_HTTP_TIMEOUT,
        "db_connect_timeout": DB_CONNECT_TIMEOUT,
        "db_statement_timeout": DB_STATEMENT_TIMEOUT,
        "db_pool_acquire_timeout": DB_POOL_ACQUIRE_TIMEOUT,
        "redis_connect_timeout": REDIS_CONNECT_TIMEOUT,
        "redis_operation_timeout": REDIS_OPERATION_TIMEOUT,
        "ollama_connect_timeout": OLLAMA_CONNECT_TIMEOUT,
        "ollama_inference_timeout": OLLAMA_INFERENCE_TIMEOUT,
        "background_job_timeout": BACKGROUND_JOB_TIMEOUT,
        "shutdown_timeout": shutdown_timeout(),
        "max_retries": MAX_RETRIES,
        "max_request_body_bytes": max_request_body_bytes(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_model_context_tokens": MAX_MODEL_CONTEXT_TOKENS,
        "max_model_output_tokens": MAX_MODEL_OUTPUT_TOKENS,
    }
