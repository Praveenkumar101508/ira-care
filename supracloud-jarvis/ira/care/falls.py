"""Suspected falls: the watch detects them, the server keeps the deadline.

The watch's accelerometer heuristic (impact, then stillness) starts a countdown
on the wrist and tells us at once. We open an `asking` alert of kind
`suspected_fall` with a deadline a little after the watch's own countdown, so a
watch that dies mid-countdown still escalates. "I'm OK" resolves it; Help, or
silence, opens the SOS with the location the watch sends. Fall detection is on
by default and a carer can turn it off per person, in which case a start is
refused and the watch is told to stop.

None of the thresholds here is a medical claim; they are the carer's settings.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import askfirst, location
from .monitoring import SOS, Observation

#: Seconds the server waits beyond the watch's countdown before opening the SOS itself.
SERVER_GRACE_S = 10
KIND = "suspected_fall"


class FallDetectionOff(RuntimeError):
    """The carer turned fall detection off for this person."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def watch_settings(settings: dict) -> dict:
    """What the watch needs to run its detector: the carer's fall settings."""
    return {
        "fall_detection_enabled": bool(settings.get("fall_detection_enabled", True)),
        "fall_countdown_s": int(settings.get("fall_countdown_s", 30)),
        "fall_impact_g": float(settings.get("fall_impact_g", 2.5)),
        "fall_still_seconds": int(settings.get("fall_still_seconds", 20)),
        "fall_still_threshold_g": float(settings.get("fall_still_threshold_g", 0.3)),
    }


def summary(peak_g: Optional[float], still_seconds: Optional[int]) -> str:
    peak = f" ({peak_g:.1f} g)" if peak_g is not None else ""
    still = f" {int(still_seconds)} seconds" if still_seconds is not None else ""
    return (f"The watch detected a hard impact{peak} followed by{still} without movement. "
            f"The person is being asked if they are OK.")


async def start(principal: Principal, device: dict, body: dict) -> Optional[dict]:
    """The watch's countdown has started. Opens the asking alert; None when one
    is already asking or open for this kind."""
    now = body.get("now") or _now()
    async with workspace_connection(principal) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1 AND status = 'active'", device["person_id"])
        settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", device["person_id"])
        if person is None or settings is None:
            return None
        if not settings["fall_detection_enabled"]:
            raise FallDetectionOff("Fall detection is turned off for this person.")
        countdown = int(settings["fall_countdown_s"])
        obs = Observation(KIND, SOS, summary(body.get("peak_g"), body.get("still_seconds")),
                          {"peak_g": body.get("peak_g"), "still_seconds": body.get("still_seconds"),
                           "countdown_s": countdown, "device_id": str(device["id"]), "source": "watch_fall_detector"})
        wait_settings = dict(settings)
        wait_settings["ask_first_wait_s"] = countdown + SERVER_GRACE_S
        alert = await askfirst.start(conn, dict(person), wait_settings, obs, now, with_checkin=False)
        if alert is None:
            return None
        return alert


async def answered_ok(principal: Principal, device: dict, now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The person tapped I'm OK during the countdown."""
    now = now or _now()
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_alerts SET status = 'resolved', resolved_at = $2, next_action_at = NULL, resolution = $3, "
            "details = details || '{\"outcome\": \"answered_ok\"}'::jsonb "
            "WHERE person_id = $1 AND kind = $4 AND status = 'asking' RETURNING *",
            device["person_id"], now, "The person answered the fall check: OK.", KIND)
        if row is None:
            return None
        out = dict(row)
        if isinstance(out.get("details"), str):
            out["details"] = json.loads(out["details"])
        return out


async def confirmed(conn, device: dict, body: dict, now: dt.datetime) -> Optional[dict]:
    """Help, or silence, during the countdown: the asking alert opens with the
    watch's location. None when no fall alert is asking (the SOS path then opens a
    suspected_fall alert of its own)."""
    row = await conn.fetchrow(
        "UPDATE care_alerts SET status = 'open', next_action_at = $2, "
        "details = details || $3::jsonb WHERE person_id = $1 AND kind = $4 AND status = 'asking' RETURNING *",
        device["person_id"], now, json.dumps({"outcome": "no_answer" if body.get("via") == "fall" else "asked_for_help"}),
        KIND)
    if row is None:
        return None
    from . import repository as repo
    loc = location.from_body(body.get("location"))
    return await repo.attach_location(conn, row["id"], loc) or repo._alert(row)
