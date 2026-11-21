"""Turning a sequence of probe results into a state something can act on.

P14-02. `scripts/failover-drill.sh` names this batch's gap in its own docstring,
which is the strongest possible statement of scope:

    The RTO here is **PostgreSQL's promotion time only**. A real recovery time
    also includes **detecting the failure** and **pointing clients at the new
    primary**, and neither exists in this project yet — in most real outages
    those dominate, and promotion is the fast part.

This module is the first of those two. The second is deliberately NOT built
here, and §"What this does not do" below says so rather than letting a reader
infer it from silence.

── Why this is not just `/readyz` again ─────────────────────────────────────

`core/runtime/readiness.py` answers *should this instance receive traffic right
now?* — a question about **one moment**, answered by **one probe**, and answered
correctly. Phase 13 made it able to say it is unwell and to distinguish "not
measured" from "measured and healthy".

Detection is a different question: *has this dependency actually failed, or did
one probe blip?* That cannot be answered by a single observation, and every
property below follows from that:

* **One failure is not a failure.** A dependency is declared FAILED only after
  `failure_threshold` consecutive failing observations. A detector that fires on
  the first blip is an outage amplifier — the exact mechanism `readiness.py`'s
  own docstring describes, where a 20-second Redis wobble becomes a 20-minute
  restart storm.
* **One success is not a recovery.** Coming back requires
  `recovery_threshold` consecutive successes, because a dependency flapping
  between up and down is not recovered and treating it as recovered puts traffic
  back onto it every few seconds.
* **Old evidence is not evidence.** If nothing has been observed within
  `staleness`, the answer is `NOT_MEASURED` — not the last state, however good
  it was. A detector that keeps reporting HEALTHY off a probe from an hour ago
  is the Phase 13 failure class exactly, arriving inside the component built to
  close it.
* **No observations at all is `NOT_MEASURED`, never `HEALTHY`.** The default
  cannot be the good answer. This is the single most likely way for this file to
  reintroduce what Phase 13 spent a batch removing, so it is the first thing
  asserted in the tests.

── What it measures ─────────────────────────────────────────────────────────

`detection_ms` is the interval between the **first failing observation** of a
run and the moment the state became FAILED. It is a real measurement of this
mechanism, and it is **not** the detection half of an RTO. Two reasons, both of
which must survive into any document quoting the number:

1. It starts at the first failing *probe*, not at the moment the dependency
   actually died. Everything between those two is invisible here and is bounded
   below by the probe interval, which this module does not control.
2. It is measured wherever it runs. A number from a loopback pair on one machine
   says little about a deployment with a network.

Read it as *"this many milliseconds of the detection cost belongs to the
threshold"*, never as *"failures are detected in this long"*.

── What this does not do ────────────────────────────────────────────────────

**It does not point clients anywhere.** There is no promotion trigger, no DSN
switch and no orchestrator integration in this repository, so the second half of
the drill's sentence is still open after this batch. `state()` returning FAILED
for `postgres` means the process now KNOWS its database is gone. Acting on that
knowledge by failing over is a deployment capability, and the deployment does
not exist yet — Phase 15 owns infrastructure.

**It sets no RTO or RPO target.** Mechanism and measurement only; the commercial
number is the owner's and stays deferred.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("ira.failure_detector")

#: Consecutive failing observations before a dependency is declared FAILED.
#: Three rather than one for the reason in the docstring; three rather than ten
#: because a detector nobody trusts to fire is not a detector.
FAILURE_THRESHOLD = 3
#: Consecutive successes before a FAILED dependency is HEALTHY again.
RECOVERY_THRESHOLD = 2
#: How long an observation stays evidence. Past this the answer is
#: NOT_MEASURED, whatever the last observation said.
STALENESS_SECONDS = 30.0

#: Cap on tracked dependency names. The readiness set is eight and fixed, but
#: `observe()` takes a string, and an unbounded map keyed by a string is how a
#: monitoring component becomes a memory-exhaustion hole.
MAX_TRACKED = 64


class DependencyHealth(str, Enum):
    """What is known about a dependency, including "nothing".

    The members are ordered from least to most alarming for reading, not for
    comparison — they are deliberately not orderable, because `>= FAILED` is the
    kind of expression that silently starts meaning something else when a member
    is inserted.
    """

    #: No observation, or none recent enough to be evidence. NOT a synonym for
    #: healthy, and never returned as a default because nothing has gone wrong
    #: yet — returned because nothing is known.
    NOT_MEASURED = "not_measured"
    #: Observed, and the recent observations succeeded.
    HEALTHY = "healthy"
    #: Failing, but not yet for long enough to declare. This state exists so an
    #: operator can see a wobble that never became an outage; nothing should
    #: act on it, which is why it is distinct from FAILED rather than folded in.
    SUSPECTED = "suspected"
    #: Failing consistently. This is the state something may act on.
    FAILED = "failed"
    #: Was FAILED, is succeeding again, but not yet for long enough. Traffic
    #: should not go back yet — a flapping dependency spends its whole life
    #: here, which is the point.
    RECOVERING = "recovering"


@dataclass
class _Record:
    """The observations for one dependency. Counters, not a history buffer.

    A ring buffer of results would let the thresholds be expressed as "k of the
    last n", which is a better detector — and it is deliberately not what this
    is. Consecutive counters cannot silently grow, cannot be misread, and make
    every state below derivable in one line. If a later batch needs k-of-n, it
    should replace this explicitly rather than inherit a buffer nobody sized.
    """

    state: DependencyHealth = DependencyHealth.NOT_MEASURED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_observed_at: float = 0.0
    #: When the current run of failures began, monotonic.
    first_failure_at: Optional[float] = None
    #: When the state became FAILED, monotonic. None while it has not.
    declared_failed_at: Optional[float] = None
    #: `declared_failed_at - first_failure_at` for the run that produced the
    #: current FAILED state. Read the caveats in the module docstring.
    detection_ms: Optional[int] = None
    #: Set the moment a FAILED dependency is declared healthy again, so the
    #: outage's measured duration survives the transition.
    last_outage_ms: Optional[int] = None


@dataclass(frozen=True)
class DependencyStatus:
    """What `status()` hands a caller. Deliberately not a bare enum.

    A caller deciding what to do needs "how long has this been true" as much as
    "what is true", and returning only the state is how a component ends up with
    the information and no way to pass it on.
    """

    name: str
    health: DependencyHealth
    #: Milliseconds since the state became FAILED. None unless FAILED.
    failing_for_ms: Optional[int] = None
    #: See the module docstring: the threshold's share of detection cost, not
    #: an RTO.
    detection_ms: Optional[int] = None
    #: How stale the newest observation is. Always present once anything has
    #: been observed, including when staleness has pushed the state back to
    #: NOT_MEASURED — which is the one case where a caller most needs it.
    last_observed_ms_ago: Optional[int] = None
    #: The measured length of the last completed outage, if there has been one.
    last_outage_ms: Optional[int] = None

    @property
    def actionable(self) -> bool:
        """Whether a caller should treat this as a failure.

        `SUSPECTED` is deliberately false: acting on a wobble is the outage
        amplifier this module exists to avoid. `NOT_MEASURED` is also false, and
        that is a real trade-off stated rather than hidden — an unmeasured
        dependency is not known to be failing, and a detector that acted on
        absence of evidence would fire on every cold start.
        """
        return self.health is DependencyHealth.FAILED


class FailureDetector:
    """Per-dependency state derived from a sequence of observations.

    Not a singleton class: `detector()` below returns the process-wide instance
    the readiness path feeds, and tests construct their own so they never race
    with it.
    """

    def __init__(self, *, failure_threshold: int = FAILURE_THRESHOLD,
                 recovery_threshold: int = RECOVERY_THRESHOLD,
                 staleness: float = STALENESS_SECONDS,
                 max_tracked: int = MAX_TRACKED) -> None:
        if failure_threshold < 1 or recovery_threshold < 1:
            raise ValueError("thresholds must be at least 1")
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.staleness = staleness
        self.max_tracked = max_tracked
        self._records: dict[str, _Record] = {}

    # ── Input ────────────────────────────────────────────────────────────────

    def observe(self, name: str, ok: bool, *, now: Optional[float] = None) -> None:
        """Record one probe result.

        `now` is injectable because every property worth asserting here is about
        TIME, and a test that has to sleep to check a staleness window is a test
        that will be flaky on a loaded CI runner and then be deleted.
        """
        now = time.monotonic() if now is None else now
        record = self._records.get(name)
        if record is None:
            if len(self._records) >= self.max_tracked:
                logger.error(
                    "failure detector is tracking %d dependencies and will not "
                    "add %r — this observation is DROPPED, and %r's state will "
                    "read NOT_MEASURED rather than healthy",
                    len(self._records), name, name)
                return
            record = self._records[name] = _Record()

        record.last_observed_at = now
        if ok:
            record.consecutive_successes += 1
            record.consecutive_failures = 0
            record.first_failure_at = None
            if record.state is DependencyHealth.FAILED:
                record.state = DependencyHealth.RECOVERING
            if (record.state is DependencyHealth.RECOVERING
                    and record.consecutive_successes >= self.recovery_threshold):
                if record.declared_failed_at is not None:
                    record.last_outage_ms = int(
                        (now - record.declared_failed_at) * 1000)
                    logger.error(
                        "dependency %r RECOVERED after %d ms failed (detection "
                        "took %s ms of that)", name, record.last_outage_ms,
                        record.detection_ms)
                record.declared_failed_at = None
                record.state = DependencyHealth.HEALTHY
            elif record.state is not DependencyHealth.RECOVERING:
                record.state = DependencyHealth.HEALTHY
            return

        record.consecutive_failures += 1
        record.consecutive_successes = 0
        if record.first_failure_at is None:
            record.first_failure_at = now
        if record.consecutive_failures >= self.failure_threshold:
            if record.state is not DependencyHealth.FAILED:
                record.declared_failed_at = now
                record.detection_ms = int(
                    (now - record.first_failure_at) * 1000)
                logger.error(
                    "dependency %r DECLARED FAILED after %d consecutive failing "
                    "observations; %d ms elapsed between the first failing "
                    "observation and this declaration. That figure is the "
                    "threshold's share of detection cost, NOT a time-to-detect: "
                    "it excludes everything before the first failing probe.",
                    name, record.consecutive_failures, record.detection_ms)
                record.state = DependencyHealth.FAILED
        else:
            record.state = DependencyHealth.SUSPECTED

    # ── Output ───────────────────────────────────────────────────────────────

    def status(self, name: str, *, now: Optional[float] = None) -> DependencyStatus:
        now = time.monotonic() if now is None else now
        record = self._records.get(name)
        if record is None:
            return DependencyStatus(name=name, health=DependencyHealth.NOT_MEASURED)

        age = now - record.last_observed_at
        age_ms = int(age * 1000)
        if age > self.staleness:
            # Stale evidence is not evidence. The last state is deliberately NOT
            # returned here, however healthy it was — a detector reporting
            # HEALTHY off an observation nobody has refreshed is precisely the
            # failure Phase 13 closed elsewhere.
            return DependencyStatus(
                name=name, health=DependencyHealth.NOT_MEASURED,
                last_observed_ms_ago=age_ms,
                last_outage_ms=record.last_outage_ms)

        failing_for = None
        if (record.state is DependencyHealth.FAILED
                and record.declared_failed_at is not None):
            failing_for = int((now - record.declared_failed_at) * 1000)
        return DependencyStatus(
            name=name, health=record.state, failing_for_ms=failing_for,
            detection_ms=record.detection_ms if record.state is DependencyHealth.FAILED else None,
            last_observed_ms_ago=age_ms,
            last_outage_ms=record.last_outage_ms)

    def report(self, *, now: Optional[float] = None) -> list[DependencyStatus]:
        """Every tracked dependency. Names it has never seen are simply absent —
        a detector must not invent a row for something nobody probed."""
        now = time.monotonic() if now is None else now
        return [self.status(name, now=now) for name in sorted(self._records)]

    def failing(self, *, now: Optional[float] = None) -> list[str]:
        """The dependencies a caller may act on. FAILED only — see `actionable`."""
        return [s.name for s in self.report(now=now) if s.actionable]

    def reset(self) -> None:
        self._records.clear()


_detector: Optional[FailureDetector] = None


def detector() -> FailureDetector:
    """The process-wide detector the readiness path feeds."""
    global _detector
    if _detector is None:
        _detector = FailureDetector()
    return _detector


def reset_detector() -> None:
    """Drop all observations. For tests, and for the same reason
    `reset_readiness_cache` exists: process-wide state that tests share is
    process-wide state that makes them pass for the wrong reason."""
    global _detector
    _detector = None


__all__ = [
    "FAILURE_THRESHOLD",
    "RECOVERY_THRESHOLD",
    "STALENESS_SECONDS",
    "DependencyHealth",
    "DependencyStatus",
    "FailureDetector",
    "detector",
    "reset_detector",
]
