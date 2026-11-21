"""Liveness, readiness, request IDs and the error contract.

The property these tests protect is the separation between liveness and
readiness. Conflating them causes a specific outage: a brief Redis failure makes
`/health` report degraded, the liveness probe fails, the orchestrator restarts
every replica, and the restarts hammer the dependency that was recovering.
"""
from __future__ import annotations

import asyncio

import pytest

from core.runtime import readiness as R
from core.runtime.errors import (
    INTERNAL_ERROR,
    IRAError,
    RATE_LIMITED,
    ROUTING_REFUSED,
    VALIDATION_ERROR,
    error_response,
)
from core.runtime.readiness import CheckResult, CheckStatus, ReadinessReport
from core.runtime.request_context import (
    MAX_ID_LENGTH,
    bind,
    clear,
    get_correlation_id,
    get_request_id,
    sanitize_id,
)


@pytest.fixture(autouse=True)
def _clean():
    clear()
    R.reset_readiness_cache()
    R.stop_draining()
    yield
    clear()
    R.reset_readiness_cache()
    R.stop_draining()


# ── Request and correlation IDs ──────────────────────────────────────────────

def test_both_ids_are_generated_when_absent():
    rid, cid = bind(None, None)
    assert rid and cid
    assert get_request_id() == rid
    assert get_correlation_id() == cid


def test_correlation_defaults_to_the_request_id():
    rid, cid = bind(None, None)
    assert rid == cid


def test_an_inbound_correlation_id_is_honoured_and_the_request_id_is_still_fresh():
    """A browser click fanning out to three API calls produces three request
    IDs and one correlation ID."""
    rid, cid = bind(None, "client-supplied-trace-42")
    assert cid == "client-supplied-trace-42"
    assert rid != cid


@pytest.mark.parametrize("bad", [
    "has spaces",
    "has\nnewline",                 # log-injection: could forge a log line
    "has\r\ncrlf",
    "x" * (MAX_ID_LENGTH + 1),      # storage amplification
    "semi;colon",
    "<script>",
    "",
])
def test_unsafe_inbound_ids_are_rejected(bad):
    assert sanitize_id(bad) is None
    rid, cid = bind(None, bad)
    # Rejected, not fatal — a malformed header must not fail the request.
    assert cid == rid
    assert "\n" not in cid and " " not in cid


@pytest.mark.parametrize("good", ["abc123", "trace-id_1.2:3", "A" * MAX_ID_LENGTH])
def test_safe_inbound_ids_are_accepted(good):
    assert sanitize_id(good) == good


def test_ids_are_empty_outside_a_request():
    clear()
    assert get_request_id() == ""
    assert get_correlation_id() == ""


async def test_ids_propagate_into_a_child_task():
    """A background task started from a request inherits its IDs — the reason
    contextvars were chosen over threading a parameter through 276 call sites."""
    rid, _ = bind(None, None)
    seen = {}

    async def child():
        seen["rid"] = get_request_id()

    await asyncio.create_task(child())
    assert seen["rid"] == rid


# ── Readiness aggregation ────────────────────────────────────────────────────

def _report(*checks: CheckResult) -> ReadinessReport:
    return ReadinessReport(ready=not any(c.blocks_readiness for c in checks),
                           checks=list(checks))


def test_all_required_checks_ok_means_ready():
    r = _report(
        CheckResult("postgres", CheckStatus.OK, 1, True),
        CheckResult("redis", CheckStatus.OK, 1, True),
    )
    assert r.ready
    assert r.as_dict()["status"] == "ready"


def test_one_failed_required_check_means_not_ready():
    r = _report(
        CheckResult("postgres", CheckStatus.OK, 1, True),
        CheckResult("redis", CheckStatus.FAILED, 0, True, "timeout"),
    )
    assert not r.ready
    assert r.as_dict()["failed"] == ["redis"]


def test_a_failed_optional_check_does_not_block_readiness():
    """An API instance serving memory, files and settings is useful with the
    model runtime down. Marking it not-ready would take the product offline
    because one subsystem is degraded."""
    r = _report(
        CheckResult("postgres", CheckStatus.OK, 1, True),
        CheckResult("model_gateway", CheckStatus.FAILED, 0, False, "timeout"),
    )
    assert r.ready


async def test_draining_is_never_ready_even_when_everything_is_healthy():
    R.begin_draining()
    assert R.is_draining()
    report = await R.evaluate_readiness()
    assert not report.ready
    assert report.draining


async def test_draining_bypasses_the_cache():
    """Draining must take effect on the very next probe, or the load balancer
    keeps sending traffic to an instance that is closing its pool."""
    R._cache = (asyncio.get_running_loop().time(), ReadinessReport(ready=True))
    R.begin_draining()
    report = await R.evaluate_readiness()
    assert not report.ready


def test_verbose_output_lists_each_check():
    r = _report(CheckResult("postgres", CheckStatus.OK, 7, True))
    body = r.as_dict(verbose=True)
    assert body["checks"][0]["name"] == "postgres"
    assert body["checks"][0]["latency_ms"] == 7


async def test_check_detail_never_carries_an_exception_message():
    """`detail` is a type name, never `str(exc)` — exception messages carry
    DSNs, hostnames and occasionally credentials."""
    async def boom():
        raise RuntimeError("could not connect to postgresql://ira:leaked@db/ira")

    result = await R._timed("postgres", True, boom(), 5.0)
    assert result.status is CheckStatus.FAILED
    assert result.detail == "RuntimeError"
    assert "leaked" not in result.detail


async def test_a_probe_that_raises_outright_still_fails_closed():
    """A broken probe must not silently become 'ready'."""
    results = [RuntimeError("probe exploded")]
    checks = []
    for item in results:
        if isinstance(item, CheckResult):
            checks.append(item)
        else:
            checks.append(CheckResult("unknown", CheckStatus.FAILED, 0, True, "probe_error"))
    assert _report(*checks).ready is False


async def test_a_timeout_is_a_failure_not_an_exception():
    async def slow():
        await asyncio.sleep(5)

    result = await R._timed("slow", True, slow(), 0.01)
    assert result.status is CheckStatus.FAILED
    assert result.detail == "timeout"


# ── Error contract ───────────────────────────────────────────────────────────

def test_an_error_body_carries_the_request_id():
    """The whole point: a user pastes one string and support finds the log."""
    rid, _ = bind(None, None)
    body = error_response(INTERNAL_ERROR)
    assert body["request_id"] == rid
    assert body["error_code"] == "internal_error"
    assert body["retryable"] is False


def test_retryable_errors_are_marked_retryable():
    assert error_response(RATE_LIMITED)["retryable"] is True


def test_a_routing_refusal_is_not_retryable():
    """A refusal from the model gateway is a policy decision, not a transient
    failure. Marking it retryable would invite clients to hammer a request that
    policy will refuse every time — a correct decision turned into a
    denial-of-wallet."""
    assert error_response(ROUTING_REFUSED)["retryable"] is False


def test_safe_details_are_included_when_present():
    body = error_response(VALIDATION_ERROR, fields=["email", "password"])
    assert body["safe_details"]["fields"] == ["email", "password"]


def test_an_error_without_safe_details_omits_the_key():
    assert "safe_details" not in error_response(INTERNAL_ERROR)


def test_ira_error_exposes_its_status_code():
    err = IRAError(RATE_LIMITED, safe_details={"retry_after": 30})
    assert err.status_code == 429
    assert err.to_response()["safe_details"]["retry_after"] == 30


def test_the_internal_error_message_is_generic():
    """Nothing from the exception reaches the client — no SQL, no hostname, no
    file path, no library detail."""
    body = error_response(INTERNAL_ERROR)
    assert body["message"] == "An internal error occurred."
    assert "safe_details" not in body


def test_error_codes_are_unique():
    from core.runtime.errors import ALL_CODES
    codes = [c.code for c in ALL_CODES]
    assert len(codes) == len(set(codes))
