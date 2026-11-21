"""P13-02 — the observability layer reports its own state.

`PHASE_13_EXECUTION_PLAN.md` §2 names the trap this batch had to avoid:

> Building the observability of a system that cannot be observed. … If
> instrumentation fails for any reason, tracing is off, the "active" line never
> prints, and **nothing says so**. The observability layer swallows its own
> failure, which means the first thing a Phase 13 dashboard would be unable to
> tell you is whether Phase 13 is working.

Measured before any of this existed, with `OTLP_ENDPOINT` set and the exporter
import made to fail:

    endpoint unset -> _tracer=None, get_tracer() -> ProxyTracer
    setup FAILED   -> _tracer=None, get_tracer() -> ProxyTracer
    public way to ask which one you are in: NONE

A deliberate configuration and a fault produced identical behaviour and
identical values. These tests hold the four states apart.
"""
from __future__ import annotations

import importlib
import logging
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


@pytest.fixture
def telemetry(monkeypatch):
    """A freshly-imported telemetry module.

    `_state` and `_tracer` are module-level, so a test that mutated them would
    leak into the next one — and a leaked "active" is precisely the false-clean
    this batch is about.
    """
    import utils.telemetry as t

    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    yield importlib.reload(t)
    importlib.reload(t)


def _fail_exporter_import(monkeypatch):
    """Make the OTLP exporter import raise, which is what a deployment with the
    SDK but not the exporter extra actually does."""
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter"):
            raise ImportError("exporter not installed")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


# ── The four states ─────────────────────────────────────────────────────────


def test_before_setup_the_state_says_so(telemetry):
    """"Nobody has started it" must not read as "it is off"."""
    state = telemetry.telemetry_state()
    assert state["tracing"] == "not_initialised"
    assert state["setup_called"] is False
    assert "has not run" in state["reason"]


def test_an_unconfigured_deployment_reads_as_disabled(telemetry, caplog):
    """The ordinary state of this product: no OTLP endpoint, no tracing.

    It is a configuration, not a fault, so it stays at INFO — an error here
    would train an operator past the one in the next test.
    """
    with caplog.at_level(logging.DEBUG, logger="ira.telemetry"):
        telemetry.setup_telemetry("probe")

    state = telemetry.telemetry_state()
    assert state["tracing"] == "disabled"
    assert state["configured"] is False
    assert "OTLP_ENDPOINT is not set" in state["reason"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_failed_setup_reads_as_failed_not_disabled(telemetry, caplog,
                                                     monkeypatch):
    """The distinction the whole batch is for.

    `OTLP_ENDPOINT` set means this deployment EXPECTED tracing. Setup raising
    is therefore a fault, and before P13-02 it produced the same observable
    result as deliberately turning tracing off.
    """
    monkeypatch.setenv("OTLP_ENDPOINT", "https://otlp.invalid")
    _fail_exporter_import(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="ira.telemetry"):
        telemetry.setup_telemetry("probe")

    state = telemetry.telemetry_state()
    assert state["tracing"] == "failed", (
        "a setup that raised is reported the same as one that was never asked "
        "to start")
    assert state["configured"] is True
    assert "ImportError" in state["reason"]

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "FAILED" in messages
    assert "expected it to be on" in messages, (
        "the log does not say why this is a fault rather than a setting")


def test_instrumentation_is_recorded_separately_from_setup(telemetry):
    """They can disagree, and the disagreement is the interesting case.

    The SDK can initialise while `FastAPIInstrumentor` fails — traces exist for
    everything EXCEPT the HTTP requests that are the reason anyone wanted them.
    A single boolean cannot say that.
    """
    telemetry.setup_telemetry("probe")            # disabled: no endpoint
    assert telemetry.telemetry_state()["instrumentation"] == "not_attempted"

    telemetry.record_instrumentation(False, "ImportError: no instrumentor")
    state = telemetry.telemetry_state()
    assert state["instrumentation"] == "failed"
    assert "ImportError" in state["instrumentation_reason"]
    assert state["tracing"] == "disabled", (
        "recording instrumentation must not overwrite the tracing state")

    telemetry.record_instrumentation(True)
    assert telemetry.telemetry_state()["instrumentation"] == "active"


# ── What it must NOT claim ──────────────────────────────────────────────────


def test_active_never_means_spans_are_arriving(telemetry):
    """The Phase 12 lesson, applied before it costs anything.

    Finding M was a correctly-BOUND read over a table with no boundary, and
    finding K was a product surface promising an export that did not exist.
    "Tracing is active" is the same shape: the SDK initialising says nothing
    about whether the collector accepted a single span.

    `export_confirmed` is therefore permanently False WITH a reason, rather
    than omitted — an absent field would read as an oversight, and a True one
    would be a lie.

    **A plant survived the first version of this test**, and the reason is
    recorded here rather than quietly re-planted: it asserted only against a
    fresh module, where `tracing` is `not_initialised`, so an
    `export_confirmed = (tracing == "active")` plant was False anyway and the
    suite stayed green. The state that matters is the ACTIVE one — that is the
    only state in which claiming confirmed export is tempting.
    """
    for tracing in ("not_initialised", "disabled", "failed", "active"):
        telemetry._record(tracing=tracing, reason="set by the test")
        state = telemetry.telemetry_state()
        assert state["export_confirmed"] is False, (
            f"export was reported as confirmed with tracing={tracing!r}; "
            f"the SDK initialising says nothing about whether the collector "
            f"accepted a single span")
        assert "background thread" in state["export_confirmed_reason"]
        assert "nothing in this process observes" in state["export_confirmed_reason"]


def test_the_state_cannot_be_written_with_an_unknown_value(telemetry):
    """`_record` is the only writer, and it refuses vocabulary it does not know.

    P12-04 lost a batch to an `f"consent_{state}"` that a CHECK constraint
    rejected into a non-fatal `except`, leaving refused attempts with no audit
    row. The same mistake here would put a made-up string on a health surface.
    """
    with pytest.raises(ValueError):
        telemetry._record(tracing="probably_fine")
    with pytest.raises(KeyError):
        telemetry._record(tracing_enabled=True)

    assert telemetry.telemetry_state()["tracing"] == "not_initialised", (
        "a refused write still moved the state")


def test_the_state_is_a_copy(telemetry):
    """A caller that mutates what it was handed must not rewrite the record."""
    state = telemetry.telemetry_state()
    state["tracing"] = "active"
    assert telemetry.telemetry_state()["tracing"] == "not_initialised"


# ── The wiring ──────────────────────────────────────────────────────────────


def test_main_records_the_instrumentation_outcome():
    """`main.py` had `except Exception: pass` on the observability layer's own
    wiring — the phase plan's §2 example, quoted verbatim in this file's
    docstring.

    Read from source: attaching `FastAPIInstrumentor` to a real app at import
    time is not something a unit test can do without standing the app up, and
    the property here is that the outcome reaches `record_instrumentation` on
    both paths. Said plainly rather than dressed up as an execution test.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "main.py"
    text = src.read_text(encoding="utf-8")
    block = text[text.index("# Auto-instrument FastAPI routes"):]

    assert "except Exception:\n    pass" not in block
    assert "record_instrumentation" in block
    assert "except ImportError" in block, (
        "an absent optional package must not be reported like a broken one — "
        "P13-01 finding O")
    assert "logger.error" in block, (
        "an instrumentor that is installed and will not attach is a fault")
    assert "measuring nothing" in block, (
        "the error does not say what the consequence is")


def test_the_health_surface_carries_the_telemetry_state():
    """§3 of the phase plan: the answer is available at RUNTIME rather than
    inferred from an absent log line."""
    import inspect

    from api.routes import health

    src = inspect.getsource(health)
    assert "telemetry_state" in src
    assert '"telemetry": telemetry' in src


def test_the_endpoint_url_is_stripped_in_production():
    """An unauthenticated surface must not map internal topology.

    `_DETAIL_KEYS` already existed for exactly this; the assertion is that the
    new pillar is covered by it rather than being a new leak.
    """
    from api.routes.health import _DETAIL_KEYS, _reduce_pillars

    assert "endpoint" in _DETAIL_KEYS
    reduced = _reduce_pillars({
        "telemetry": {"tracing": "active", "endpoint": "https://otlp.internal",
                      "reason": "initialised"},
    })
    assert reduced["telemetry"]["tracing"] == "active"
    assert "endpoint" not in reduced["telemetry"], (
        "the collector URL is exposed on an unauthenticated endpoint")
