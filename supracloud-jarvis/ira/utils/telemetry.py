"""
OpenTelemetry setup for IRA.
Sends traces to any OTLP endpoint (Grafana Cloud, Honeycomb, Jaeger, etc.)

Set OTLP_ENDPOINT in .env to enable. Leave blank to disable silently.
Example (Grafana Cloud):
  OTLP_ENDPOINT=https://otlp-gateway-prod-eu-west-0.grafana.net/otlp
  OTLP_HEADERS=Authorization=Basic <base64(instanceId:apiKey)>
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("ira.telemetry")

_tracer = None

#: P13-02. What this process actually did, RECORDED rather than inferred.
#:
#: Measured before any of this existed: a `setup_telemetry` that failed and one
#: that was deliberately disabled were indistinguishable from anywhere except a
#: startup log line. `_tracer` was `None` in both, `get_tracer()` returned a
#: non-recording tracer in both, and there was no public way to ask.
#:
#: `PHASE_13_EXECUTION_PLAN.md` §2 names that as the trap this phase must not
#: walk into — building the observability of a system that cannot report on its
#: own observability. Every field here is written by the code that performed the
#: action, never derived afterwards.
_state: dict[str, Any] = {
    "setup_called": False,
    "configured": False,
    "endpoint": "",
    "tracing": "not_initialised",
    "reason": "setup_telemetry() has not run in this process",
    "instrumentation": "not_attempted",
    "instrumentation_reason": "",
}

#: `tracing` takes exactly one of these, and the distinction that matters is the
#: last two: DISABLED is a deliberate configuration and FAILED is a fault, and
#: before P13-02 they produced identical behaviour.
TRACING_STATES = ("not_initialised", "disabled", "failed", "active")


def telemetry_state() -> dict[str, Any]:
    """What this process's telemetry is actually doing. Never inferred.

    `tracing == "active"` means the SDK initialised and a provider is
    installed. It does **not** mean spans are arriving, and `export_confirmed`
    is permanently False for a stated reason rather than omitted: spans leave
    through `BatchSpanProcessor` on a background thread, and nothing in this
    process observes whether the collector accepted them.

    Claiming otherwise would be exactly the failure this phase exists to
    prevent — a health surface reporting a signal nobody measured. Phase 12
    paid for this rule twice: a correctly-BOUND read over a table with no
    boundary (finding M), and a product surface promising an export that did
    not exist (finding K).
    """
    state = dict(_state)
    state["export_confirmed"] = False
    state["export_confirmed_reason"] = (
        "spans are exported by BatchSpanProcessor on a background thread; "
        "nothing in this process observes whether the collector accepted them"
    )
    return state


def _record(**fields: Any) -> None:
    """The only writer of `_state`. Keeping it to one function is why the state
    cannot drift from what the code actually did."""
    unknown = set(fields) - set(_state)
    if unknown:                                     # pragma: no cover - guard
        raise KeyError(f"unknown telemetry state field(s): {sorted(unknown)}")
    if "tracing" in fields and fields["tracing"] not in TRACING_STATES:
        raise ValueError(f"unknown tracing state {fields['tracing']!r}")
    _state.update(fields)


def record_instrumentation(active: bool, reason: str = "") -> None:
    """Record whether HTTP auto-instrumentation attached. Called from `main`.

    Separate from `setup_telemetry` because they can disagree: the SDK can
    initialise while `FastAPIInstrumentor` fails, in which case traces exist for
    everything except the HTTP requests that are the reason anyone wanted them.
    """
    _record(instrumentation="active" if active else "failed",
            instrumentation_reason=reason)


def setup_telemetry(service_name: str = "ira-api") -> None:
    """Initialise OpenTelemetry SDK. Call once from main.py lifespan."""
    global _tracer

    endpoint = os.getenv("OTLP_ENDPOINT", "").strip()
    _record(setup_called=True, configured=bool(endpoint), endpoint=endpoint)
    if not endpoint:
        _record(tracing="disabled",
                reason="OTLP_ENDPOINT is not set, so tracing was never started")
        logger.info("Telemetry: OTLP_ENDPOINT not set — tracing disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({
            "service.name": service_name,
            "service.version": os.getenv("GIT_SHA", "unknown"),
        })

        headers_raw = os.getenv("OTLP_HEADERS", "")
        headers = {}
        for pair in headers_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                headers[k.strip()] = v.strip()

        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _tracer = trace.get_tracer(service_name)
        _record(tracing="active", reason=f"OTLP tracing initialised for {endpoint}")
        logger.info(f"Telemetry: OTLP tracing enabled → {endpoint}")

    except Exception as e:
        # P13-02. Still non-fatal, and the direction is right — telemetry must
        # not stop the process from serving. What changed is that the failure
        # is now a RECORDED state rather than one log line at startup, so
        # anything asking later gets "failed", not "disabled".
        _record(tracing="failed",
                reason=f"OTLP_ENDPOINT is set but setup raised "
                       f"{type(e).__name__}: {e}")
        logger.error(
            "Telemetry setup FAILED (non-fatal, but tracing is OFF and "
            "OTLP_ENDPOINT is set, so this deployment expected it to be on): "
            "%s: %s", type(e).__name__, e)


def get_tracer():
    """Get the tracer. Returns a no-op tracer if telemetry is disabled OR if
    opentelemetry isn't installed — so otel stays a fully optional dependency."""
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace
        return trace.get_tracer("ira-noop")
    except Exception:
        return _NOOP_TRACER


# ── No-op fallbacks (opentelemetry absent / telemetry disabled) ───────────────
# So trace_span() never raises ModuleNotFoundError when otel isn't installed.
class _NoopSpan:
    def is_recording(self) -> bool:
        return False

    def set_attribute(self, *args, **kwargs) -> None:
        pass

    def record_exception(self, *args, **kwargs) -> None:
        pass

    def set_status(self, *args, **kwargs) -> None:
        pass


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, name: str, *args, **kwargs):
        yield _NoopSpan()


_NOOP_TRACER = _NoopTracer()


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None):
    """Context manager for a named span. No-op if telemetry is disabled."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        if attributes and span.is_recording():
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
        try:
            yield span
        except Exception as e:
            if span.is_recording():
                from opentelemetry.trace import StatusCode
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
            raise
