"""Readings checked as they arrive, day and night.

`confirmed_outside_range` is the one rule: a heart rate counts as outside the
carer's range only when `confirm_readings` readings in a row are outside it and
they span at least `confirm_seconds`. One spike is ignored. An unset range is
no rule. Nothing here knows about the time of day: the night rules differ only
in what happens next (ask first, then the escalation chain), and that is
decided where the rule fires.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class RangeBreach:
    values: tuple[float, ...]
    first_at: dt.datetime
    last_at: dt.datetime
    low: Optional[int]
    high: Optional[int]

    @property
    def summary(self) -> str:
        shown = ", ".join(f"{v:g}" for v in self.values)
        span = int((self.last_at - self.first_at).total_seconds())
        return (f"Heart rate readings {shown} over {span} seconds are outside the range set for this "
                f"person ({self.low if self.low is not None else 'no lower'} to "
                f"{self.high if self.high is not None else 'no upper'}).")


def _outside(value: float, low: Optional[int], high: Optional[int]) -> bool:
    return (low is not None and value < low) or (high is not None and value > high)


def confirmed_outside_range(readings: Sequence[Mapping], settings: Mapping) -> Optional[RangeBreach]:
    """The newest heart-rate readings, if the trailing run of readings outside
    the range is at least N long and spans long enough. One reading back inside
    the range ends the run. `readings` may be in any order and any kind."""
    low, high = settings.get("heart_rate_min"), settings.get("heart_rate_max")
    if low is None and high is None:
        return None
    n = max(1, int(settings.get("heart_rate_confirm_readings", 2)))
    span_needed = int(settings.get("heart_rate_confirm_seconds", 120))
    hr = sorted((r for r in readings if r["kind"] == "heart_rate"), key=lambda r: r["recorded_at"])
    tail: list = []
    for r in reversed(hr):
        if not _outside(float(r["value"]), low, high):
            break
        tail.insert(0, r)
    if len(tail) < n:
        return None
    first, last = tail[0]["recorded_at"], tail[-1]["recorded_at"]
    if n > 1 and (last - first).total_seconds() < span_needed:
        return None
    return RangeBreach(tuple(float(r["value"]) for r in tail), first, last, low, high)


def movement_since(readings: Sequence[Mapping], since: dt.datetime) -> Optional[float]:
    """Steps and movement counts since `since`; None when nothing was recorded."""
    total, seen = 0.0, False
    for r in readings:
        if r["kind"] in ("steps", "movement") and r["recorded_at"] >= since:
            total += float(r["value"])
            seen = True
    return total if seen else None
