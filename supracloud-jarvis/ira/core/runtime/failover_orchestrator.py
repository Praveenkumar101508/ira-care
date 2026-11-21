"""P14-R2 — the composition: detection → promotion → redirection.

── The gap this closes ────────────────────────────────────────────────────

Phase 14 and Phase 15 built three mechanisms and never joined them, and every
document in the repository says so in the same words. `failover-drill.sh`:

    Nothing consumes the detector. A process can now say "my database has been
    gone for eleven seconds"; no code path turns that into a promotion, and
    HAProxy's own health check is what moves traffic — on its schedule, not on
    the detector's.

    The three have never been run end to end, so the interval that would be an
    RTO has never been measured. Adding the three numbers is not measuring it.

`FailureDetector.actionable` is documented as *"whether a caller should treat
this as a failure"* — and had no caller. That is FINDING AB's shape a second
time, on a different control: a component that computes a decision nobody acts
on.

This module is the caller. It is what makes the drill an end-to-end sequence
rather than three numbers added together.

── Why the order is promote-then-redirect, and why that is the whole point ─

The naive composition redirects on detection and promotes in parallel. It is
worse than the outage it replaces.

A streaming standby is **read-only** until it is promoted. Point clients at it
first and every write fails with `cannot execute INSERT in a read-only
transaction` — while reads succeed. The system looks up. Health checks pass.
TCP connects. It is the failure mode that gets found in the morning, in the
data, rather than in the monitoring.

So this promotes, **verifies the promotion actually took**, and only then moves
the address. `_verify` is not defensiveness: `pg_ctl promote` returns as soon as
the signal is written, not when recovery has ended, so a redirect fired on
`promote()` returning is a redirect to a node still in recovery. That is a race
that would pass a test on a fast machine.

If the verification does not succeed within its budget, this **does not
redirect**. An un-promoted standby serving traffic is a worse position than a
database that is plainly down, because plainly-down is obvious and read-only is
not. The refusal is reported, loudly, with the stage it stopped at.

── Why it is off by default ───────────────────────────────────────────────

Promotion is not reversible: once a standby is promoted the pair has diverged
and the old primary cannot rejoin without a rebuild. A component that does that
on its own judgement must be switched on deliberately, by someone who has
decided the trade — so `IRA_FAILOVER_AUTOMATIC` gates it, and the default is
off. `enabled=False` is reported as an explicit `NOT_ENABLED` outcome rather
than silence, because a failover that did not happen because nobody turned it
on must not look like a failover that did not happen because nothing failed.

── What it deliberately does not decide ───────────────────────────────────

It does not choose a standby, arbitrate between two candidates, or fence the
old primary. Those are the parts of a real HA system that need a quorum, and a
single process inventing one is how split-brain happens. The `FailoverPlan` is
supplied by whoever configured the topology; this module supplies the trigger,
the ordering, and the refusal.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from core.runtime.failure_detector import (
    DependencyHealth,
    DependencyStatus,
    FailureDetector,
    detector as _default_detector,
)

logger = logging.getLogger("ira.runtime.failover")

#: The dependency whose failure this acts on.
DEPENDENCY = "postgres"

#: How long `promote()` has to produce a node that is actually out of recovery
#: and writable. `pg_ctl promote` returns when the signal is written; recovery
#: ends later. Generous, because the alternative to waiting is redirecting to a
#: read-only node.
VERIFY_TIMEOUT_SECONDS = 30.0

#: Gap between verification polls.
VERIFY_INTERVAL_SECONDS = 0.05


class FailoverStage(str, Enum):
    """How far the sequence got. Ordered for reading, not for comparison."""

    #: Nothing was attempted.
    NOT_ATTEMPTED = "not_attempted"
    #: `promote()` was called and has not been verified.
    PROMOTED = "promoted"
    #: The promoted node is out of recovery and accepting writes.
    VERIFIED = "verified"
    #: The stable address now points at the promoted node. The sequence is done.
    REDIRECTED = "redirected"


class FailoverRefusal(str, Enum):
    """Why nothing was done. Every one of these is a distinct situation.

    Collapsing them into a bool is how "we chose not to fail over" becomes
    indistinguishable from "we could not tell whether to".
    """

    #: The dependency is healthy, or failing but not yet declared.
    NOT_FAILED = "not_failed"
    #: Nothing has been observed recently enough to be evidence. NOT the same
    #: as healthy — see `DependencyHealth.NOT_MEASURED`.
    NOT_MEASURED = "not_measured"
    #: `IRA_FAILOVER_AUTOMATIC` is off. Reported rather than silent.
    NOT_ENABLED = "not_enabled"
    #: A failover already completed in this process. Promotion is not repeatable.
    ALREADY_DONE = "already_done"
    #: `promote()` raised.
    PROMOTION_FAILED = "promotion_failed"
    #: `promote()` returned, and the node never left recovery inside the budget.
    #: The address was NOT moved.
    PROMOTION_UNVERIFIED = "promotion_unverified"
    #: `redirect()` raised, after a verified promotion.
    REDIRECT_FAILED = "redirect_failed"


@dataclass(frozen=True)
class FailoverOutcome:
    """What happened, with the timings a drill needs to compute an RTO.

    The milliseconds are measured from the moment this call observed an
    actionable failure — NOT from when the database died, which this process
    cannot know. A drill that wants a real RTO times from its own kill; these
    numbers say how much of that interval belonged to which stage.
    """

    triggered: bool
    stage: FailoverStage
    refusal: Optional[FailoverRefusal] = None
    detail: str = ""
    #: The detector's view at the moment of the decision, carried so a caller
    #: never has to re-read it and get a different answer.
    status: Optional[DependencyStatus] = None
    promote_ms: Optional[int] = None
    verify_ms: Optional[int] = None
    redirect_ms: Optional[int] = None
    total_ms: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "stage": self.stage.value,
            "refusal": self.refusal.value if self.refusal else None,
            "detail": self.detail,
            "health": self.status.health.value if self.status else None,
            "failing_for_ms": self.status.failing_for_ms if self.status else None,
            "promote_ms": self.promote_ms,
            "verify_ms": self.verify_ms,
            "redirect_ms": self.redirect_ms,
            "total_ms": self.total_ms,
        }


class FailoverPlan(Protocol):
    """The topology-specific half, supplied by whoever configured the topology.

    Three operations, and `verified()` is the one that is easy to leave out.
    Without it there is no way to distinguish "promoted" from "sent a promotion
    signal", and the difference is whether clients land on a writable node.
    """

    async def promote(self) -> None:
        """Begin promoting the standby. May return before recovery has ended."""

    async def verified(self) -> bool:
        """True once the promoted node is out of recovery AND accepts writes.

        Both halves matter: `pg_is_in_recovery()` going false is necessary and
        not sufficient — the node has to actually take a write.
        """

    async def redirect(self) -> None:
        """Point the stable client address at the promoted node."""


def automatic_failover_enabled() -> bool:
    """Read at call time, not at import.

    A module-level constant would freeze the answer at whatever the environment
    was when the first import happened, which in a test suite is whatever ran
    first.
    """
    return os.environ.get("IRA_FAILOVER_AUTOMATIC", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class FailoverOrchestrator:
    """Turns an actionable failure into a promotion and a redirection.

    Instantiated rather than global so a drill can run several in one process
    without one run's completion suppressing the next. `reset()` exists for the
    same reason.
    """

    plan: FailoverPlan
    dependency: str = DEPENDENCY
    verify_timeout: float = VERIFY_TIMEOUT_SECONDS
    verify_interval: float = VERIFY_INTERVAL_SECONDS
    #: Overridable so tests and drills never race with the process-wide one.
    detector: Optional[FailureDetector] = None
    #: Set once a sequence completes. Promotion is not repeatable.
    _completed: bool = field(default=False, init=False)

    def reset(self) -> None:
        self._completed = False

    def _detector(self) -> FailureDetector:
        return self.detector if self.detector is not None else _default_detector()

    async def evaluate(self, *, enabled: Optional[bool] = None) -> FailoverOutcome:
        """Consider a failover. Never raises.

        `enabled` overrides the environment gate — used by the drill, which is
        the caller that has decided the trade explicitly and in one place.
        """
        status = self._detector().status(self.dependency)

        if status.health is DependencyHealth.NOT_MEASURED:
            # Deliberately its own refusal and not folded into NOT_FAILED.
            # "Nothing has been observed" is not "the database is fine", and a
            # failover that fires on absence of evidence fires on every cold
            # start.
            return FailoverOutcome(
                False, FailoverStage.NOT_ATTEMPTED, FailoverRefusal.NOT_MEASURED,
                f"{self.dependency} has no observation recent enough to act on",
                status)

        if not status.actionable:
            # SUSPECTED lands here, and that is the detector's judgement being
            # respected rather than second-guessed: acting on a wobble is the
            # outage amplifier it exists to avoid.
            return FailoverOutcome(
                False, FailoverStage.NOT_ATTEMPTED, FailoverRefusal.NOT_FAILED,
                f"{self.dependency} is {status.health.value}, not FAILED",
                status)

        if self._completed:
            return FailoverOutcome(
                False, FailoverStage.NOT_ATTEMPTED, FailoverRefusal.ALREADY_DONE,
                "a failover has already completed in this process; promotion "
                "is not repeatable and a second one would promote a node that "
                "is already primary", status)

        if not (automatic_failover_enabled() if enabled is None else enabled):
            logger.warning(
                "%s is FAILED and automatic failover is NOT enabled "
                "(IRA_FAILOVER_AUTOMATIC). Reporting this rather than staying "
                "silent: nothing will be promoted.", self.dependency)
            return FailoverOutcome(
                False, FailoverStage.NOT_ATTEMPTED, FailoverRefusal.NOT_ENABLED,
                "automatic failover is off; set IRA_FAILOVER_AUTOMATIC to "
                "enable it. Promotion is irreversible, so it is opt-in.",
                status)

        return await self._run(status)

    async def _run(self, status: DependencyStatus) -> FailoverOutcome:
        began = time.monotonic()
        logger.error(
            "%s DECLARED FAILED (failing for %s ms) — promoting the standby. "
            "This is irreversible: the old primary cannot rejoin without a "
            "rebuild.", self.dependency, status.failing_for_ms)

        try:
            await self.plan.promote()
        except Exception as exc:  # noqa: BLE001 — a failed promotion is an outcome
            logger.exception("promote() raised; the address was NOT moved")
            return FailoverOutcome(
                False, FailoverStage.NOT_ATTEMPTED,
                FailoverRefusal.PROMOTION_FAILED,
                f"promote() raised {type(exc).__name__}: {exc}", status,
                total_ms=_ms(began))
        promote_ms = _ms(began)

        # THE VERIFICATION THAT MAKES THE ORDER MEAN ANYTHING. `pg_ctl promote`
        # returns when the signal file is written; recovery ends later. Moving
        # the address on `promote()` returning is a redirect to a read-only
        # node, and it is a race that passes on a fast machine.
        verify_began = time.monotonic()
        deadline = verify_began + self.verify_timeout
        verified = False
        while time.monotonic() < deadline:
            try:
                verified = await self.plan.verified()
            except Exception as exc:  # noqa: BLE001 — an unverifiable promotion is unverified
                logger.warning("verified() raised (%s: %s); still waiting",
                               type(exc).__name__, exc)
                verified = False
            if verified:
                break
            await _sleep(self.verify_interval)
        verify_ms = _ms(verify_began)

        if not verified:
            # NOT redirecting is the decision. A read-only node behind the
            # client address takes writes and loses them; a database that is
            # plainly down does not.
            logger.error(
                "the standby did not become writable within %.1fs. NOT moving "
                "the client address — an un-promoted standby serving traffic "
                "fails writes while reads succeed, which is worse than an "
                "outage because it is invisible.", self.verify_timeout)
            return FailoverOutcome(
                False, FailoverStage.PROMOTED,
                FailoverRefusal.PROMOTION_UNVERIFIED,
                f"promote() returned but the node was still in recovery after "
                f"{self.verify_timeout:.1f}s; the address was NOT moved",
                status, promote_ms=promote_ms, verify_ms=verify_ms,
                total_ms=_ms(began))

        redirect_began = time.monotonic()
        try:
            await self.plan.redirect()
        except Exception as exc:  # noqa: BLE001 — see above
            logger.exception("redirect() raised after a VERIFIED promotion. The "
                             "standby is now primary and clients are still "
                             "pointed at the dead node — this needs a human.")
            return FailoverOutcome(
                False, FailoverStage.VERIFIED, FailoverRefusal.REDIRECT_FAILED,
                f"redirect() raised {type(exc).__name__}: {exc}. The promotion "
                f"SUCCEEDED and is not undone.", status,
                promote_ms=promote_ms, verify_ms=verify_ms,
                total_ms=_ms(began))
        redirect_ms = _ms(redirect_began)

        self._completed = True
        total = _ms(began)
        logger.warning(
            "failover complete: promote %d ms, verify %d ms, redirect %d ms, "
            "total %d ms measured from the decision. This is NOT an RTO — it "
            "excludes everything before the decision, which is most of it.",
            promote_ms, verify_ms, redirect_ms, total)
        return FailoverOutcome(
            True, FailoverStage.REDIRECTED, None,
            "promoted, verified writable, and the client address moved",
            status, promote_ms=promote_ms, verify_ms=verify_ms,
            redirect_ms=redirect_ms, total_ms=total)


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


__all__ = [
    "DEPENDENCY",
    "VERIFY_TIMEOUT_SECONDS",
    "FailoverStage",
    "FailoverRefusal",
    "FailoverOutcome",
    "FailoverPlan",
    "FailoverOrchestrator",
    "automatic_failover_enabled",
]
