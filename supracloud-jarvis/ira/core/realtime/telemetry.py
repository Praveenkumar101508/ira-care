"""Latency measurement, and the guarantee that telemetry is only measurement.

Two jobs, and they are in the same module because they are the same claim seen
from two sides: *what IRA records about a live conversation is how the
connection behaved, and nothing about what was said.*

── The no-content guard ─────────────────────────────────────────────────────

`sanitise_metrics` is the application-side half of the CHECK constraint in
027_realtime.sql. Both exist on purpose:

  * the database constraint is the one that cannot be bypassed — no code path,
    no migration-less hotfix, no direct psql session can write a string into
    `realtime_events.metrics`;
  * this function is the one that produces a comprehensible error, at the
    boundary, naming the offending key, instead of a constraint violation from
    three layers down.

It REJECTS rather than silently dropping. A guard that quietly discards the
key you passed leaves the caller believing something was recorded, and the
next person to read the code cannot tell which fields survive.

The allow-list and the type rule are duplicated between here and the SQL, and
`tests/realtime/test_ladder_matches_migration.py` asserts they are identical —
the same drift-guard arrangement as Phase 4's tool policies.

── The latency budget ───────────────────────────────────────────────────────

`assess` compares a MEASURED time-to-first-audio against the rung's STATED
budget. Neither word is decoration:

  * measured — the number comes from the session clock, not from a constant;
  * stated — the budget is a target this build commits to being judged
    against, not a guarantee it meets. `LatencyReport.within_budget` is
    frequently False on a slow host, and that is the report doing its job.

Phase 6 does NOT claim IRA meets these budgets in production. It claims IRA
measures itself against them and stores the truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

from core.realtime.capability import CapabilityLevel, budget_ms, profile_for

#: Exactly the keys 027_realtime.sql's CHECK constraint permits. Kept in the
#: same order as the SQL so a diff between them is readable.
ALLOWED_METRIC_KEYS: Final[frozenset[str]] = frozenset({
    "first_audio_ms", "connect_ms", "rtt_ms", "jitter_ms",
    "packet_loss_pct", "downlink_kbps", "uplink_kbps",
    "attempt", "backoff_ms", "from_rung", "to_rung",
    "budget_ms", "within_budget", "video_enabled",
    "microphone_enabled",
})


class ContentInTelemetry(ValueError):
    """Something that is not a measurement was passed as one.

    Named for what it means rather than for the type error it technically is:
    the reason a string is refused here is not that strings are awkward to
    store, it is that a string is where a transcript would go.
    """


def sanitise_metrics(raw: Optional[Mapping[str, Any]]) -> dict[str, float | int | bool]:
    """Validate a metrics mapping, or raise.

    Accepts only allow-listed keys whose values are int, float or bool.
    `bool` is checked before `int` because `isinstance(True, int)` is True in
    Python and a bool must stay a bool through to JSON — `{"within_budget": 1}`
    is not the same statement as `{"within_budget": true}`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ContentInTelemetry(
            f"realtime metrics must be a mapping of measurements, got {type(raw).__name__}"
        )

    clean: dict[str, float | int | bool] = {}
    for key, value in raw.items():
        if key not in ALLOWED_METRIC_KEYS:
            raise ContentInTelemetry(
                f"{key!r} is not a permitted realtime metric. Telemetry records "
                f"how the connection behaved, never what was said; adding a key "
                f"means adding it to ALLOWED_METRIC_KEYS *and* to the CHECK "
                f"constraint in 027_realtime.sql. Permitted: "
                f"{', '.join(sorted(ALLOWED_METRIC_KEYS))}"
            )
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, int):
            clean[key] = value
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ContentInTelemetry(
                    f"metric {key!r} is {value!r}; NaN and infinity are not "
                    f"measurements and are not valid JSON"
                )
            clean[key] = value
        else:
            raise ContentInTelemetry(
                f"metric {key!r} has value of type {type(value).__name__}; only "
                f"numbers and booleans may be stored. A string here is how a "
                f"transcript would end up in the telemetry table."
            )
    return clean


def is_measurement_only(raw: Optional[Mapping[str, Any]]) -> bool:
    """Non-raising form, for callers deciding whether to try."""
    try:
        sanitise_metrics(raw)
    except ContentInTelemetry:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """A measured time-to-first-audio against its rung's stated budget."""

    level: CapabilityLevel
    measured_ms: Optional[int]
    budget_ms: Optional[int]
    within_budget: Optional[bool]

    @property
    def overrun_ms(self) -> Optional[int]:
        """How far past the budget, or None when there is nothing to compare."""
        if self.measured_ms is None or self.budget_ms is None:
            return None
        return max(0, self.measured_ms - self.budget_ms)

    def as_metrics(self) -> dict[str, float | int | bool]:
        """The subset that is storable telemetry."""
        out: dict[str, float | int | bool] = {}
        if self.measured_ms is not None:
            out["first_audio_ms"] = self.measured_ms
        if self.budget_ms is not None:
            out["budget_ms"] = self.budget_ms
        if self.within_budget is not None:
            out["within_budget"] = self.within_budget
        return sanitise_metrics(out)

    def describe(self) -> str:
        """One line for a log or a status panel. Truthful when unmeasured."""
        if self.budget_ms is None:
            return f"{self.level.value}: no audio is produced on this rung"
        if self.measured_ms is None:
            return f"{self.level.value}: no audio was produced (budget {self.budget_ms}ms)"
        verdict = "within" if self.within_budget else "over"
        return (
            f"{self.level.value}: first audio {self.measured_ms}ms, "
            f"{verdict} the {self.budget_ms}ms budget"
        )


def assess(
    level: CapabilityLevel | str, measured_ms: Optional[int]
) -> LatencyReport:
    """Compare a measurement to the rung's budget.

    Three distinct "no verdict" cases, kept distinct because collapsing them
    into False would report a silent rung as having missed a target it never
    had:

      * unknown rung        → no budget, no verdict;
      * silent rung         → no budget, no verdict;
      * no audio produced   → budget known, still no verdict.
    """
    profile = profile_for(level)
    if profile is None:
        return LatencyReport(
            level=CapabilityLevel.TEXT_ONLY, measured_ms=measured_ms,
            budget_ms=None, within_budget=None,
        )
    budget = budget_ms(profile.level)
    if budget is None or measured_ms is None:
        return LatencyReport(profile.level, measured_ms, budget, None)
    return LatencyReport(profile.level, measured_ms, budget, measured_ms <= budget)


__all__ = [
    "ALLOWED_METRIC_KEYS",
    "ContentInTelemetry",
    "LatencyReport",
    "assess",
    "is_measurement_only",
    "sanitise_metrics",
]
