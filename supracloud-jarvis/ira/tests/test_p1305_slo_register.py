"""P13-05 — an objective may not claim a signal nobody collects.

`PHASE_13_EXECUTION_PLAN.md` §3.5 states it as a prohibition:

> **No dashboard claims a signal that is not collected.** The direct counterpart
> of P12-05's finding K, where a product surface promised a feature that did not
> exist and the guard could not catch it.

Measured before the register existed:

    GET /metrics / /api/v1/metrics -> 404, 404
    routes mentioning "metric"     -> NONE (of 275)
    setup_telemetry installs       -> TracerProvider only
    Counter / Histogram / Gauge    -> none in product code

So nothing in this process aggregates a latency or an error rate, and a p99
alert rule written today would fire on nothing while looking like coverage.

`core/runtime/slo.py` therefore derives each objective's status from a **probe**
rather than a declaration. These tests hold that property, and the first of them
is the one that matters: they run against the real build, so an objective that
claims a collector this build does not have fails here.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


# ── The guard finding K did not have ────────────────────────────────────────


def test_latency_is_reported_as_not_collected():
    """This build has no MeterProvider, and the register must say so.

    The single most important assertion in the file. If it ever fails because
    somebody added metrics collection, that is correct and this test should be
    updated to match — but it must not fail because a probe started guessing.
    """
    from core.runtime.slo import register

    row = [r for r in register() if r["slo"] == "http_request_latency_p95"][0]
    assert row["collection"] == "not_collected", row
    assert row["alertable"] is False, (
        "an objective on p99 latency is reported as alertable, and nothing in "
        "this process aggregates a latency")


def test_the_metrics_probe_requires_positive_evidence(monkeypatch):
    """The probe's FIRST version was a blacklist and claimed a signal that did
    not exist. This is the test that would have caught it.

    It excluded a list of known no-op provider names and returned True for
    anything else, so the installed `_ProxyMeterProvider` — not on the list —
    read as *collected*. An unrecognised provider must be NOT_COLLECTED: that
    direction can only under-claim, and under-claiming an observability signal
    is the only safe direction there is.
    """
    from opentelemetry import metrics

    from core.runtime import slo

    class _SomethingNobodyAnticipated:
        """Not a no-op provider, not an SDK one. The shape that broke it."""

    monkeypatch.setattr(metrics, "get_meter_provider",
                        lambda: _SomethingNobodyAnticipated())

    assert slo._process_records_metrics() is False, (
        "an unrecognised meter provider was treated as evidence of collection")


def test_a_real_meter_provider_would_flip_it(monkeypatch):
    """The control. A probe hard-wired to False would satisfy the two tests
    above and be just as useless — it must actually detect a real collector."""
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider

    from core.runtime import slo

    monkeypatch.setattr(metrics, "get_meter_provider", lambda: MeterProvider())
    assert slo._process_records_metrics() is True

    row = [r for r in slo.register()
           if r["slo"] == "http_request_latency_p95"][0]
    assert row["collection"] == "collected_in_process"
    assert row["alertable"] is True


def test_a_probe_that_raises_is_not_collected(monkeypatch):
    """"The probe blew up" is not evidence that a signal exists.

    P13-01's whole subject, applied to this module's own machinery: a failure
    that resolves to the reassuring answer.
    """
    from core.runtime.slo import Collection, Signal

    def explode():
        raise RuntimeError("the probe itself is broken")

    signal = Signal(name="x", what="x", recorded_by="", probe=explode)
    assert signal.collection() is Collection.NONE


# ── The register cannot lie about its own shape ─────────────────────────────


def test_every_slo_names_a_signal_that_exists():
    """An objective pointing at a signal nobody defined would render with an
    empty status rather than failing."""
    from core.runtime.slo import SIGNALS, SLOS

    known = {s.name for s in SIGNALS}
    for slo in SLOS:
        assert slo.signal in known, f"{slo.name} names unknown signal {slo.signal}"


def test_an_slo_has_no_status_field_to_edit():
    """Status is derived. If `SLO` ever grows a status attribute, the register
    becomes editable prose again — which is what finding K was."""
    from dataclasses import fields

    from core.runtime.slo import SLO

    names = {f.name for f in fields(SLO)}
    for forbidden in ("status", "collection", "alertable", "collected"):
        assert forbidden not in names, (
            f"SLO.{forbidden} makes the claim declarable rather than checkable")


def test_every_externally_collected_signal_names_its_collector():
    """"Collected externally" without naming the system is an assumption
    wearing a status."""
    from core.runtime.slo import Collection, SIGNALS

    for signal in SIGNALS:
        if signal.collection() is Collection.EXTERNAL:
            assert signal.recorded_by, signal.name


def test_every_signal_states_what_its_probe_cannot_see():
    """A probe that returns True for the wrong reason is worse than no probe,
    so each one publishes its own blind spot."""
    from core.runtime.slo import SIGNALS

    for signal in SIGNALS:
        assert signal.probe_limit.strip(), (
            f"{signal.name} has a probe with no stated limit")


# ── The caveats that must travel with the numbers ───────────────────────────


def test_every_latency_objective_carries_the_gc_caveat():
    """P13-04 measured the tail: `gc.disable()` drops max from 128ms to 9.78ms.

    A percentile target on this service is a target on Python's memory
    management, and an objective that omitted that would be quoted without it —
    which is the entire failure mode of a number living in a document.
    """
    from core.runtime.slo import GC_CAVEAT, SLOS

    latency = [s for s in SLOS if "latency" in s.name]
    assert latency, "no latency objective at all; this test is measuring nothing"
    for slo in latency:
        assert GC_CAVEAT in slo.caveats, (
            f"{slo.name} sets a percentile target without the garbage-collector "
            f"caveat P13-04 measured")


def test_the_latency_baseline_says_which_paths_were_driven():
    """Two of 275 routes have numbers. An objective quoting them without that
    is the same claim finding K made."""
    from core.runtime.slo import SLOS

    slo = [s for s in SLOS if s.name == "http_request_latency_p95"][0]
    assert slo.baseline and "/livez" in slo.baseline and "/readyz" in slo.baseline
    joined = " ".join(slo.caveats)
    assert "no product path has been driven" in joined


def test_an_objective_with_no_measurement_says_None():
    """`baseline=None` renders as null rather than as an invented figure."""
    from core.runtime.slo import register

    row = [r for r in register() if r["slo"] == "readiness_availability"][0]
    assert row["baseline"] is None


def test_the_error_rate_objective_carries_finding_w():
    """The proxy log is the only place request outcomes are recorded, and
    P13-01 finding W is the condition on reading it: the parser assumes JSON
    and reported 0 events from 30 lines of real traffic in combined format."""
    from core.runtime.slo import SLOS

    slo = [s for s in SLOS if s.name == "http_5xx_rate"][0]
    joined = " ".join(slo.caveats)
    assert "finding W" in joined
    assert "combined format" in joined


def test_unalertable_names_them_rather_than_counting():
    """"3 of 4 are alertable" is the summary that gets quoted while the missing
    one is the interesting one.

    **P15-01 strengthened this test, and the reason is worth recording.** As
    written it compared `unalertable()` against `register()` — a **tautology**.
    It asserted that two functions agree with each other, not that either is
    right, and it passed unchanged when FINDING AG moved two objectives from
    alertable to not-collected. A test that cannot fail for the property it
    names is the failure this programme keeps finding, and this one was inside
    the module written to prevent it.

    The exact set now lives in `test_p1501_collector_evidence.py`, beside the
    finding that determined it. What remains here is the consistency property,
    which is worth having and is honestly labelled as the weaker one.
    """
    from core.runtime.slo import register, unalertable

    missing = unalertable()
    assert isinstance(missing, list)
    assert all(isinstance(name, str) for name in missing)
    expected = {r["slo"] for r in register() if not r["alertable"]}
    assert set(missing) == expected

    # The half the tautology could not see: `unalertable()` must be non-empty
    # while anything is uncollected, and must name it. Nothing in this
    # repository collects a latency, so this cannot legitimately be empty.
    assert "http_request_latency_p95" in missing, (
        "the latency objective reads as alertable. Nothing in this process "
        "aggregates a latency — /metrics 404s and setup_telemetry installs no "
        "MeterProvider — so either a collector was added, or the register is "
        "claiming a signal nobody has.")
