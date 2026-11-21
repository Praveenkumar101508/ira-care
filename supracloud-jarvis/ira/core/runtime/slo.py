"""P13-05 — service objectives, and whether their signals are actually collected.

`PHASE_13_EXECUTION_PLAN.md` §3.5 states the requirement as a prohibition:

> **No dashboard claims a signal that is not collected.** The direct counterpart
> of P12-05's finding K, where a product surface promised a feature that did not
> exist and the guard could not catch it.

Measured before writing anything here:

    GET /metrics              -> 404
    GET /api/v1/metrics       -> 404
    routes mentioning metric  -> NONE          (of 275 routes)
    setup_telemetry installs  -> TracerProvider only; no MeterProvider
    Counter / Histogram / Gauge in product code -> none

So **nothing in this process aggregates a latency or an error rate.** An alert
rule on p99 latency written today would be a YAML file that looks like coverage
and fires on nothing — the green wall §2 of the plan forbids, at the one surface
whose job is to be trusted about system health.

── Why status is DERIVED and not declared ──────────────────────────────────

Finding K was a `disclosure.py` constant that said users could export their data
while no export existed. The lesson was not "write more careful constants" — it
was that a claim nobody can check will drift from the truth, and the checkable
version is the only one worth having.

So an `SLO` here does not carry a status field. It names a `Signal`, and each
`Signal` carries a **probe**: a function that answers *is this collected, right
now, in this build?* `register()` runs the probes. Nobody can mark an objective
enforceable by editing a string; it becomes enforceable when its probe passes.

── The three collection states, and why "externally" is not a dodge ────────

`COLLECTED_IN_PROCESS` is the strong one and **nothing has it today**.

`COLLECTED_EXTERNALLY` means the signal genuinely exists and is recorded, by
something outside this process — the orchestrator's own probe history for
readiness, the reverse proxy's access log for request outcomes. That is a real
answer, not a softer one: an availability SLO computed from Kubernetes' record
of `/readyz` is a perfectly good SLO. What it is not is *this process's*
measurement, so each one names its external dependency, and the dependency is
part of the objective rather than an assumption behind it.

`NOT_COLLECTED` means no alert rule can be written. Saying so is the deliverable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Optional


class Collection(str, Enum):
    """Where a signal is recorded, if anywhere."""

    #: This process records it over time. Nothing qualifies yet.
    IN_PROCESS = "collected_in_process"
    #: Something outside this process records it, and that thing is named.
    EXTERNAL = "collected_externally"
    #: Nothing records it. No alert rule is possible.
    NONE = "not_collected"


@dataclass(frozen=True)
class Signal:
    """A measurable quantity, and the honest answer about who records it."""

    name: str
    #: What it measures, in one line.
    what: str
    #: Where it is recorded — a system, not a hope. Empty when nothing records it.
    recorded_by: str
    #: Runs now, in this build, and answers "is this collected?". The whole
    #: point: a claim that cannot be checked will drift from the truth.
    probe: Callable[[], bool]
    #: What the probe cannot see. Stated because a probe that returns True for
    #: the wrong reason is worse than no probe.
    probe_limit: str = ""
    #: **P15-01, FINDING AG.** Positive evidence that the system named in
    #: `recorded_by` actually exists in this deployment. Required whenever
    #: `recorded_by` is set; without it the signal cannot be EXTERNAL.
    #:
    #: This field exists because `recorded_by` alone used to decide the answer.
    #: See `collection()`.
    recorder_probe: Optional[Callable[[], bool]] = None

    def collection(self) -> Collection:
        """Derived by execution, on **both** halves of the question.

        ── P15-01, FINDING AG ────────────────────────────────────────────────

        This used to end:

            return Collection.EXTERNAL if self.recorded_by else Collection.IN_PROCESS

        so `self.probe()` decided only NONE-vs-not-NONE, and the whole weight of
        *"something outside records this"* rested on whether `recorded_by` was a
        **non-empty string literal**. Two signals were `EXTERNAL` and therefore
        `alertable=True` on that basis, and **neither recorder existed**:

        * `readiness_state` — *"the orchestrator's own probe history"*. Nothing
          polls `/readyz` anywhere in this repository; the only Compose
          healthchecks target the model services' `/health`, and a Docker
          healthcheck keeps a last state and a retry count, not a history.
        * `http_error_rate` — *"the reverse proxy access log"*. No Compose file
          defines an nginx, caddy or traefik service; the only nginx.conf lives
          under `future-scale/`.

        This module's own comment above the probes forbids exactly that
        reasoning — *"a probe returning a literal is a declaration wearing a
        probe's name"* — and P13-05 applied it to the `probe` half, catching
        `_process_records_metrics` failing open. The `recorded_by` half was
        never subjected to the same rule.

        So `EXTERNAL` now requires positive evidence that the named recorder is
        present, in the same shape as `_process_records_metrics`'s repair: an
        unrecognised or absent recorder is `NOT_COLLECTED`, which is the
        direction that can only **under**-claim. Each `recorder_probe` becomes
        True on its own the day the thing it looks for is deployed.
        """
        try:
            present = bool(self.probe())
        except Exception:  # noqa: BLE001 — a probe that raises has not proved
            # anything, and "not proved" is NOT_COLLECTED. Failing the other way
            # is how a register starts claiming signals nobody has.
            return Collection.NONE
        if not present:
            return Collection.NONE
        if not self.recorded_by:
            return Collection.IN_PROCESS
        if self.recorder_probe is None:
            # A named recorder with nothing to check it is a declaration. Under
            # the old code this was the ONLY case and it returned EXTERNAL.
            return Collection.NONE
        try:
            if not bool(self.recorder_probe()):
                return Collection.NONE
        except Exception:  # noqa: BLE001 — same rule as above.
            return Collection.NONE
        return Collection.EXTERNAL


@dataclass(frozen=True)
class SLO:
    """An objective, the signal it needs, and what is known about the baseline.

    Deliberately has **no status field**. Status is derived from the signal's
    probe by `register()`.
    """

    name: str
    objective: str
    signal: str
    #: A measured figure with its provenance, or None when nobody has measured
    #: it. `None` is the honest value and is rendered as such.
    baseline: str | None = None
    #: Anything that would make this objective misleading if omitted. A latency
    #: objective on this service carries the garbage-collector caveat, and a
    #: test asserts it does.
    caveats: tuple[str, ...] = ()


# ── Probes ───────────────────────────────────────────────────────────────────
#
# Each answers one question by execution. None of them is allowed to be a
# constant: a probe returning a literal is a declaration wearing a probe's name.


def _process_records_metrics() -> bool:
    """Does THIS process aggregate anything over time?

    Checks for a real OpenTelemetry SDK `MeterProvider`, because that is the
    only aggregation this codebase would plausibly grow. `setup_telemetry`
    installs a `TracerProvider` and nothing else, so this is False — and it
    becomes True on its own the day a meter is installed, which is the point.

    **This probe's first version was wrong, and running it caught that.** It
    excluded a list of known no-op provider names and returned True for
    anything else — a blacklist, so an unrecognised type read as *collected*.
    The installed default is `_ProxyMeterProvider`, which was not on the list,
    so the register claimed `http_request_latency` was collected in-process by
    a build with no meter at all.

    That is this batch's own subject — a check that fails open — inside the
    module written to stop the register claiming signals nobody has. It now
    requires **positive evidence**: an instance of the SDK's `MeterProvider`.
    Anything it cannot recognise is NOT_COLLECTED, which is the direction that
    can only under-claim.
    """
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
    except Exception:  # noqa: BLE001 — otel or its SDK is optional
        return False
    return isinstance(metrics.get_meter_provider(), MeterProvider)


#: The Compose files, read as data. Same technique — and the same reason — as
#: `tests/runtime/test_service_boundaries.py`: a deployment description is
#: checkable without starting anything.
def _compose_documents() -> list[dict]:
    import yaml

    root = Path(__file__).resolve().parents[3]
    docs: list[dict] = []
    for path in sorted(root.glob("docker-compose*.yml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable file proves nothing
            continue
        if isinstance(loaded, dict):
            docs.append(loaded)
    return docs


def _something_polls_readiness() -> bool:
    """P15-01. Is there positive evidence that anything polls `/readyz`?

    `readiness_availability` is an availability SLO over *an orchestrator's
    probe history*. The endpoint being routed says nothing about that, so this
    looks for a configured prober: a Compose healthcheck whose test names
    `/readyz`, or a Kubernetes manifest with a readiness probe.

    Today it is **False** — the only Compose healthchecks target the model
    services' `/health`, and no Kubernetes manifest exists. It becomes True on
    its own the day one is added, which is the point.

    Deliberate limit: a Docker healthcheck keeps a **last state and a retry
    count**, not a series. Finding one here proves something *polls*; whether it
    retains history for a 30-day objective is a further question this cannot
    answer, and the SLO's caveats say so.

    So a True from this probe must never be read as *"the 30-day objective is
    measurable"*. It means *"something asks the question"* — and a poller with
    no retention would make this signal EXTERNAL while the objective over it
    stays unmeasurable, which is the next version of this same finding.
    """
    for doc in _compose_documents():
        for service in (doc.get("services") or {}).values():
            if not isinstance(service, dict):
                continue
            test = (service.get("healthcheck") or {}).get("test")
            if test and "/readyz" in str(test):
                return True

    root = Path(__file__).resolve().parents[3]
    for pattern in ("k8s/**/*.yaml", "k8s/**/*.yml", "deploy/**/*.yaml",
                    "charts/**/*.yaml"):
        for path in root.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            if "readinessProbe" in text and "/readyz" in text:
                return True
    return False


#: Image names that identify a reverse proxy. A allow-list rather than a
#: blacklist, for the reason `_process_records_metrics` learned: an unrecognised
#: image must read as "no proxy", never as "probably a proxy".
_PROXY_IMAGES = ("nginx", "caddy", "traefik", "envoy", "haproxy")


def _a_reverse_proxy_is_deployed() -> bool:
    """P15-01. Does a deployment here actually run the proxy that writes the log?

    `http_error_rate` is computed from *the reverse proxy access log*. The
    existing probe checks only that `nginx_log_path` is a non-empty **setting**
    — a configured path to a file that a proxy would write **if one existed**.

    No Compose file defines a proxy service; the only `nginx.conf` in the tree
    is under `future-scale/`, which is by its own name not the current
    deployment. So this is False today, and the error-rate objective is
    correctly not alertable until a proxy is actually deployed.
    """
    for doc in _compose_documents():
        for service in (doc.get("services") or {}).values():
            if not isinstance(service, dict):
                continue
            image = str(service.get("image") or "").lower()
            if any(name in image for name in _PROXY_IMAGES):
                return True
    return False


def _readiness_is_exposed() -> bool:
    """Is `/readyz` actually routed?

    An availability SLO computed from an orchestrator's probe history depends on
    the endpoint existing. Asserted against the router rather than assumed.
    """
    from app.routers import register_routers  # noqa: F401 - import guard

    from api.routes.health import router

    return any(getattr(route, "path", "") == "/readyz" for route in router.routes)


def _proxy_access_log_is_read() -> bool:
    """Is there a configured nginx access log this deployment reads?

    Request outcomes ARE recorded — by the reverse proxy — and
    `worker/security_monitor.py` already reads that file. An error-rate SLO is
    therefore possible without adding any collection, **conditional on the log
    format matching the parser.** P13-01 finding W is exactly that conditional:
    the parser assumes JSON, and against combined format it produced 0 events
    from 30 lines of brute-force traffic, silently. It now says so, which is
    what makes this signal safe to build an objective on.
    """
    try:
        from config import get_settings

        return bool((get_settings().nginx_log_path or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _telemetry_reports_itself() -> bool:
    """P13-02's state. Enumerated rather than trusted: the probe checks the
    value is one this build actually knows."""
    try:
        from utils.telemetry import TRACING_STATES, telemetry_state

        return telemetry_state().get("tracing") in TRACING_STATES
    except Exception:  # noqa: BLE001
        return False


def _failure_detection_is_wired() -> bool:
    """P14-02. Does the readiness path actually feed the failure detector?

    Derived by executing the wiring rather than by reading the source: a fresh
    detector is given one real `CheckResult` through the same call
    `_evaluate_uncached` makes, and the question is whether the detector's state
    moved off `NOT_MEASURED`.

    Reading `readiness.py` for the string `observe` would be a declaration
    wearing a probe's name — P13-05's own rule, and its `_process_records_metrics`
    probe shipped a version that broke it. Note the deliberate limit: this
    proves the MECHANISM is connected, not that anything has been observed. The
    detector answers that itself, and answers `NOT_MEASURED` until it has.
    """
    try:
        from core.runtime.failure_detector import (DependencyHealth,
                                                   FailureDetector)
        from core.runtime.readiness import CheckResult, CheckStatus

        probe = FailureDetector()
        sample = CheckResult("probe", CheckStatus.FAILED, 0, True)
        probe.observe(sample.name, sample.status is CheckStatus.OK, now=0.0)
        return probe.status("probe", now=0.0).health is not DependencyHealth.NOT_MEASURED
    except Exception:  # noqa: BLE001 — see `Signal.collection`: not proved is
        # not collected, and failing the other way is how a register starts
        # claiming signals nobody has.
        return False


SIGNALS: Final[tuple[Signal, ...]] = (
    Signal(
        name="http_request_latency",
        what="per-request handler duration, aggregated into percentiles",
        recorded_by="",
        probe=_process_records_metrics,
        probe_limit=(
            "sees only an installed MeterProvider. A future collector that "
            "aggregates some other way would read as absent here"
        ),
    ),
    Signal(
        name="http_error_rate",
        what="the share of responses with a 5xx status",
        recorded_by="the reverse proxy access log",
        probe=_proxy_access_log_is_read,
        recorder_probe=_a_reverse_proxy_is_deployed,
        probe_limit=(
            "proves a path is CONFIGURED, not that the file exists, is "
            "readable, or is in the JSON format the parser expects — P13-01 "
            "finding W is that last one, and it is now audible"
        ),
    ),
    Signal(
        name="readiness_state",
        what="whether this instance reported itself ready",
        recorded_by="the orchestrator's own probe history",
        probe=_readiness_is_exposed,
        recorder_probe=_something_polls_readiness,
        probe_limit=(
            "proves the endpoint is routed. Whether anything polls it, and "
            "whether that poller keeps history, is outside this process"
        ),
    ),
    Signal(
        name="dependency_failure_state",
        what=(
            "whether a dependency has failed CONSISTENTLY, as opposed to "
            "whether one probe failed"
        ),
        recorded_by="",
        probe=_failure_detection_is_wired,
        probe_limit=(
            "proves the readiness path feeds the detector in THIS build. It "
            "says nothing about whether any probe has actually run, and the "
            "detector reports NOT_MEASURED until one does — so a True here is "
            "'the wiring exists', never 'the dependencies are healthy'"
        ),
    ),
    Signal(
        name="telemetry_state",
        what="whether tracing is off by configuration or off by fault",
        recorded_by="",
        probe=_telemetry_reports_itself,
        probe_limit=(
            "proves the state is reported and is a known value. It is a "
            "current state, not a series — see the SLO that depends on it"
        ),
    ),
)

_BY_NAME: Final[dict[str, Signal]] = {s.name: s for s in SIGNALS}


#: The garbage-collector caveat, attached to every latency objective and
#: asserted by a test. P13-04 measured it: with `gc.disable()` the ~100 ms
#: outlier on `/livez` disappears and max falls from 128 ms to 9.78 ms. A p99
#: target on this service is a target on Python's memory management, and an
#: objective that omitted that would be quoted without it.
GC_CAVEAT: Final[str] = (
    "the p99 and max tails on this service are the garbage collector, not the "
    "handler (P13-04: gc.disable() drops max from 128ms to 9.78ms). Any "
    "percentile target above p95 is a target on GC behaviour."
)

#: Measured only on `/livez` and `/readyz`, in-process, with no transport. Every
#: figure is a floor.
FLOOR_CAVEAT: Final[str] = (
    "P13-04's numbers are in-process ASGI latency with no uvicorn, socket or "
    "TLS in the path, so they are FLOORS; a deployed target must add transport"
)


SLOS: Final[tuple[SLO, ...]] = (
    SLO(
        name="readiness_availability",
        objective=(
            "an instance reports ready for 99.9% of the orchestrator's probes "
            "over a rolling 30 days, excluding deliberate draining"
        ),
        signal="readiness_state",
        baseline=None,
        caveats=(
            "draining is a correct not-ready and must be excluded, or a "
            "rolling deploy scores as an outage",
            "P13-04 finding AA: before single-flight, every probe arriving "
            "during a cache miss ran the full check set — a readiness SLO "
            "measured then would have been measuring the stampede",
        ),
    ),
    SLO(
        name="http_5xx_rate",
        objective="fewer than 0.1% of responses are 5xx over a rolling 24 hours",
        signal="http_error_rate",
        baseline=(
            "0 server errors across 2000 driven requests (P13-04, /livez and "
            "/readyz only). 503 from /readyz is the probe working and is "
            "excluded, not counted as an error"
        ),
        caveats=(
            "computed from the proxy log, so it covers whatever the proxy "
            "sees — not requests that never reached it",
            "P13-01 finding W: the parser assumes nginx JSON format and "
            "reported 0 events from 30 lines of real traffic in combined "
            "format. That failure is now logged; an SLO over this signal is "
            "only as true as that log line is read",
        ),
    ),
    SLO(
        name="http_request_latency_p95",
        objective="p95 handler latency stays under 250 ms",
        signal="http_request_latency",
        baseline=(
            "/livez 8.29ms, /readyz 9.23ms at concurrency 10 (P13-04). Two "
            "paths out of 275, both dependency-free probes"
        ),
        caveats=(
            FLOOR_CAVEAT,
            GC_CAVEAT,
            "no product path has been driven: /chat, /auth/token and both "
            "health surfaces have NO measured latency at all",
        ),
    ),
    SLO(
        name="dependency_failure_time_to_detect",
        objective=(
            "a failed required dependency is declared FAILED within 30 seconds "
            "of actually failing"
        ),
        signal="dependency_failure_state",
        #: MEASURED by P14-R2. This carried `None` and the caveat "NOTHING HAS
        #: BEEN MEASURED ... nobody has yet killed a dependency and timed the
        #: declaration against the moment it died" from P14-02 until
        #: `scripts/failover_e2e_drill.py` did exactly that.
        baseline=(
            "1.21 s (min 1.208, max 1.210, 5 runs) — SIGKILL to the detector "
            "declaring FAILED, measured by scripts/failover_e2e_drill.py. The "
            "kill is sent by the drill, so the moment of death is known "
            "exactly rather than inferred from a probe"
        ),
        caveats=(
            "this is the interval from the DEATH, not from the first failing "
            "probe. The detector's own `threshold_detection_ms` is the latter "
            "and is a different, smaller quantity — it cannot see the gap "
            "between a dependency dying and the next probe noticing",
            "1.21 s is dominated by the drill's own 1.0 s probe timeout: the "
            "first probe after the kill blocks until it expires, then two more "
            "fail immediately at the 3-observation threshold. A deployment "
            "with a different probe interval or timeout will measure a "
            "different number, and this one says nothing about it",
            "the 30 seconds above is an objective the owner has not agreed. "
            "The commercial RTO/RPO target is deferred, and this must not be "
            "quoted as one",
            "detection is one PART of a recovery time, and P14-R2 measured the "
            "whole: 1.39-1.53 s from kill to a client completing a write "
            "through the unchanged address, over 10 runs. Detection is ~86% of "
            "it. A met objective HERE would still not be a recovery time — the "
            "end-to-end figure is the one that is, and it belongs to "
            "`scripts/failover_e2e_drill.py`, not to this row",
            "measured on ONE HOST over loopback, with two PostgreSQL processes "
            "sharing a kernel, a disk and a page cache. It measures the "
            "mechanism, not the availability of a deployment",
            "the detector is per-process and keeps counters, not a series. "
            "Its state decays to NOT_MEASURED after 30 s without an "
            "observation, so it cannot answer questions about the past",
        ),
    ),
    SLO(
        name="tracing_is_on_when_configured",
        objective=(
            "no instance runs with OTLP_ENDPOINT set and tracing in the "
            "`failed` state"
        ),
        signal="telemetry_state",
        baseline=None,
        caveats=(
            "this is a state, not a series: it is alertable by scraping "
            "/health/detail, and nothing records its history",
            "`active` means the SDK initialised, NOT that a span was ever "
            "accepted — telemetry_state().export_confirmed is permanently "
            "False and says why",
        ),
    ),
)


def register() -> list[dict]:
    """Every objective, with its collection state DERIVED by running the probe.

    This is the guard finding K did not have. An objective whose signal is
    `not_collected` is reported as `alertable: False`, and no amount of editing
    this file changes that without also changing what the build collects.
    """
    out = []
    for slo in SLOS:
        signal = _BY_NAME.get(slo.signal)
        if signal is None:                        # pragma: no cover - guarded
            raise KeyError(f"SLO {slo.name!r} names unknown signal {slo.signal!r}")
        collection = signal.collection()
        out.append({
            "slo": slo.name,
            "objective": slo.objective,
            "signal": signal.name,
            "signal_measures": signal.what,
            "collection": collection.value,
            "recorded_by": signal.recorded_by or None,
            "probe_limit": signal.probe_limit,
            "alertable": collection is not Collection.NONE,
            "baseline": slo.baseline,
            "caveats": list(slo.caveats),
        })
    return out


def unalertable() -> list[str]:
    """Objectives that cannot have an alert rule today, by name.

    Named rather than counted, because "3 of 4 are alertable" is the kind of
    summary that gets quoted while the missing one is the interesting one.
    """
    return [row["slo"] for row in register() if not row["alertable"]]


__all__ = [
    "Collection",
    "FLOOR_CAVEAT",
    "GC_CAVEAT",
    "SLO",
    "SLOS",
    "SIGNALS",
    "Signal",
    "register",
    "unalertable",
]
