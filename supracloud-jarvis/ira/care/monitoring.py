"""Is this normal? The safety rules, as pure functions.

Every rule names what was OBSERVED and the person's OWN setting it was compared
with. None of them says what it means medically: there are no built-in
thresholds, and an unset range is no rule at all.

    after a missed check-in ("No answer.") ..............................
      no movement in the window          -> no_answer_no_movement   (sos)
      heart rate outside the carer's range -> heart_rate_outside_range (sos)
      watch off the wrist                -> off_wrist_during_check  (sos)
      readings normal, person moving     -> nothing yet; N misses in a row
                                            -> missed_checkins        (urgent)
    at any time, during waking hours ......................................
      watch off the wrist for M minutes  -> off_wrist                (notice)
      battery under B %                  -> low_battery              (notice)
      background sensor permission off   -> sensor_permission_off    (notice)
      no sync for S minutes              -> no_sync                  (notice)

"Help" (button or spoken) opens an SOS directly at ingestion (phase 2) and
never comes through here.

Severity decides what the escalation does (care/escalation.py): `sos` and
`urgent` escalate through the tiers until somebody acknowledges; `notice`
tells the carers once and waits to be resolved.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from . import checkins as rules

SOS = "sos"
URGENT = "urgent"
NOTICE = "notice"

#: One open alert per kind per person: a rule that keeps firing must not keep
#: opening alerts while the first is still being dealt with.
ESCALATING_KINDS = frozenset({"no_answer_no_movement", "heart_rate_outside_range",
                              "off_wrist_during_check", "missed_checkins", "help_requested"})


@dataclass(frozen=True)
class Observation:
    kind: str
    severity: str
    #: Plain words for the alert: what was seen and the setting it was compared with
    summary: str
    details: dict = field(default_factory=dict)
    checkin_id: Optional[object] = None


def _latest(readings: Iterable[Mapping], kind: str) -> Optional[Mapping]:
    best = None
    for r in readings:
        if r["kind"] == kind and (best is None or r["recorded_at"] > best["recorded_at"]):
            best = r
    return best


def movement_in_window(readings: Iterable[Mapping], since: dt.datetime) -> Optional[float]:
    """Steps and movement counts recorded since `since`, summed. None when no
    movement reading at all fell in the window (unknown is not zero)."""
    total, seen = 0.0, False
    for r in readings:
        if r["kind"] in ("steps", "movement") and r["recorded_at"] >= since:
            total += float(r["value"])
            seen = True
    return total if seen else None


def evaluate_missed_checkin(
    checkin: Mapping, settings: Mapping, readings: Sequence[Mapping], device: Optional[Mapping],
    recent_checkins: Sequence[Mapping], now: dt.datetime,
) -> list[Observation]:
    """The sensor check that follows a check-in nobody answered."""
    out: list[Observation] = []
    window = dt.timedelta(seconds=int(settings["no_movement_window_s"]))
    moved = movement_in_window(readings, now - window)
    floor = int(settings["movement_floor_steps"])
    if moved is not None and moved <= floor:
        out.append(Observation(
            "no_answer_no_movement", SOS,
            f"No answer to the check-in and no movement for {int(window.total_seconds() // 60)} minutes "
            f"(movement count {moved:g}, floor {floor}).",
            {"movement": moved, "floor": floor, "window_s": int(window.total_seconds())}, checkin["id"]))
    hr = _latest(readings, "heart_rate")
    lo, hi = settings.get("heart_rate_min"), settings.get("heart_rate_max")
    if hr is not None and (lo is not None or hi is not None) and (now - hr["recorded_at"]) <= window * 2:
        value = float(hr["value"])
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            out.append(Observation(
                "heart_rate_outside_range", SOS,
                f"No answer to the check-in and the last heart-rate reading ({value:g}) is outside the "
                f"range set for this person ({lo if lo is not None else 'no lower'} to "
                f"{hi if hi is not None else 'no upper'}).",
                {"heart_rate": value, "range": [lo, hi], "recorded_at": hr["recorded_at"].isoformat()},
                checkin["id"]))
    on_wrist = _latest(readings, "on_wrist")
    worn = device.get("on_wrist") if device else None
    if on_wrist is not None and (now - on_wrist["recorded_at"]) <= window * 2:
        worn = bool(on_wrist["value"])
    if worn is False:
        out.append(Observation(
            "off_wrist_during_check", SOS,
            "No answer to the check-in and the watch is not on the wrist.",
            {"on_wrist": False}, checkin["id"]))
    if not out:
        misses = rules.missed_in_a_row(recent_checkins)
        limit = int(settings["missed_checkins_before_escalation"])
        if misses >= limit:
            out.append(Observation(
                "missed_checkins", URGENT,
                f"{misses} check-ins in a row with no answer (the limit set for this person is {limit}); "
                f"readings were in range and the person was moving.",
                {"missed_in_a_row": misses, "limit": limit}, checkin["id"]))
    return out


def evaluate_device(
    device: Mapping, settings: Mapping, person_timezone: str, now: dt.datetime,
    off_wrist_since: Optional[dt.datetime] = None,
) -> list[Observation]:
    """The quiet, carer-facing checks on the watch itself."""
    out: list[Observation] = []
    local = rules.local_now(now, person_timezone)
    quiet = rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"])
    if device.get("status") != "paired":
        return out
    if not quiet and device.get("on_wrist") is False and off_wrist_since is not None:
        minutes = (now - off_wrist_since).total_seconds() / 60
        limit = int(settings["off_wrist_alert_minutes"])
        if minutes >= limit:
            out.append(Observation("off_wrist", NOTICE,
                                   f"The watch has been off the wrist for {int(minutes)} minutes "
                                   f"(alert after {limit}).",
                                   {"minutes": int(minutes), "limit": limit}))
    battery = device.get("battery_percent")
    if battery is not None and battery < int(settings["battery_alert_percent"]):
        out.append(Observation("low_battery", NOTICE,
                               f"The watch battery is at {battery} % (alert under "
                               f"{int(settings['battery_alert_percent'])} %).",
                               {"battery_percent": battery}))
    if device.get("background_sensor_permission") is False:
        out.append(Observation("sensor_permission_off", NOTICE,
                               "The watch's background sensor permission is switched off, so readings "
                               "cannot be collected between check-ins.", {}))
    last_sync = device.get("last_sync_at") or device.get("paired_at")
    if last_sync is not None:
        minutes = (now - last_sync).total_seconds() / 60
        limit = int(settings["sync_alert_minutes"])
        if minutes >= limit:
            out.append(Observation("no_sync", NOTICE,
                                   f"The watch has not synced for {int(minutes)} minutes (alert after {limit}).",
                                   {"minutes": int(minutes), "limit": limit}))
    return out


def no_diagnosis_words(text: str) -> bool:
    """A guard the tests pin: alert wording describes, it does not diagnose."""
    lowered = text.lower()
    return not any(w in lowered for w in (
        "diagnos", "arrhythm", "bradycard", "tachycard", "cardiac", "stroke", "heart attack",
        "hypotens", "hypertens", "sepsis", "unconscious", "fall risk", "dementia", "medical emergency"))
