"""Suspected falls: the watch detects them, the server keeps the deadline.

The watch's accelerometer heuristic (impact, then stillness) starts a countdown
on the wrist and tells us at once. We open an `asking` alert of kind
`suspected_fall` with a deadline a little after the watch's own countdown, so a
watch that dies mid-countdown still escalates. "I'm OK" resolves it; Help, or
silence, opens the SOS with the location the watch sends. Fall detection is on
by default and a carer can turn it off per person, in which case a start is
refused and the watch is told to stop.

A fall countdown on the watch is one *incident* with its own id, sent with every event for it
(D-092). The server holds at most one unresolved fall alert per person and never drops a
detection:

  * a start while a fall alert is unresolved (asking, open, escalating or acknowledged) is
    attached to that alert: recorded in `details.detections`, nothing new opened, and the
    deadline (`next_action_at`) left exactly as it was;
  * "I'm OK" resolves the asking alert only when it names the latest incident attached to it; an
    OK for an older incident (one that arrived after a newer detection) is recorded and refused,
    and the alert keeps asking until its original deadline;
  * Help, or the end of a countdown, while a fall SOS is already open returns that SOS;
  * every change is made under a per-person transaction lock, so concurrent events cannot open
    two alerts.

Events without an incident id (a watch from before D-092) attach and resolve as before, except
that a second start is attached rather than refused.

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
    if still_seconds is not None and int(still_seconds) == 0:
        # The watch's fall model (v3, see the watch's RecoveringFallDetector) raises a suspected fall
        # from the impact and the movement around it, without the rule's stillness wait; the rule
        # always sends at least 5 seconds, so 0 means the model. Describe the signal, not a diagnosis.
        return (f"The watch detected a hard impact{peak} and a movement pattern its model flags; "
                f"it did not wait for stillness. The person is being asked if they are OK.")
    still = f" {int(still_seconds)} seconds" if still_seconds is not None else ""
    return (f"The watch detected a hard impact{peak} followed by{still} without movement. "
            f"The person is being asked if they are OK.")


class StaleAnswer(RuntimeError):
    """An "I'm OK" for an incident older than the latest detection attached to the alert."""


UNRESOLVED = ("asking", "open", "escalating", "acknowledged")


async def lock_person(conn, person_id) -> None:
    """Serialise fall events for one person until the transaction ends (concurrent starts, Help
    and OK from the outbox and the sync worker must never open two alerts)."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"care_fall:{person_id}")


def _details(row) -> dict:
    d = row["details"]
    return json.loads(d) if isinstance(d, str) else dict(d or {})


def _detection(body: dict, now: dt.datetime) -> dict:
    return {"incident_id": body.get("incident_id"), "detection": int(body.get("detection") or 1),
            "peak_g": body.get("peak_g"), "still_seconds": body.get("still_seconds"),
            "received_at": now.isoformat() if isinstance(now, dt.datetime) else str(now)}


async def unresolved_alert(conn, person_id) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT * FROM care_alerts WHERE person_id = $1 AND kind = $2 AND status = ANY($3::text[]) "
        "ORDER BY opened_at DESC LIMIT 1", person_id, KIND, list(UNRESOLVED))
    return dict(row) if row else None


async def attach(conn, alert: dict, record: dict) -> dict:
    """Record a detection on the alert already held. The deadline and status are not touched.
    The same (incident, detection) twice is recorded once."""
    details = _details(alert)
    detections = list(details.get("detections") or [])
    incident = record.get("incident_id")
    if not (incident and any(d.get("incident_id") == incident and d.get("detection") == record["detection"]
                             for d in detections)):
        detections.append(record)
    details["detections"] = detections
    if incident:
        ids = list(details.get("incident_ids") or [])
        if incident not in ids:
            ids.append(incident)
        details["incident_ids"] = ids
    row = await conn.fetchrow("UPDATE care_alerts SET details = $2::jsonb WHERE id = $1 RETURNING *",
                              alert["id"], json.dumps(details, default=str))
    out = dict(row)
    out["details"] = details
    out["attached"] = True
    return out


async def start(principal: Principal, device: dict, body: dict) -> Optional[dict]:
    """A detection from the watch's countdown. Opens the asking alert, or, when a fall alert is
    already unresolved for the person, attaches the detection to it (D-092). None only when the
    person is not active or has no settings."""
    now = body.get("now") or _now()
    async with workspace_connection(principal) as conn:
        await lock_person(conn, device["person_id"])
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1 AND status = 'active'", device["person_id"])
        settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", device["person_id"])
        if person is None or settings is None:
            return None
        if not settings["fall_detection_enabled"]:
            raise FallDetectionOff("Fall detection is turned off for this person.")
        record = _detection(body, now)
        existing = await unresolved_alert(conn, device["person_id"])
        if existing is not None:
            return await attach(conn, existing, record)
        countdown = int(settings["fall_countdown_s"])
        incident = body.get("incident_id")
        obs = Observation(KIND, SOS, summary(body.get("peak_g"), body.get("still_seconds")),
                          {"peak_g": body.get("peak_g"), "still_seconds": body.get("still_seconds"),
                           "countdown_s": countdown, "device_id": str(device["id"]), "source": "watch_fall_detector",
                           "incident_id": incident, "incident_ids": [incident] if incident else [],
                           "detections": [record]})
        wait_settings = dict(settings)
        wait_settings["ask_first_wait_s"] = countdown + SERVER_GRACE_S
        alert = await askfirst.start(conn, dict(person), wait_settings, obs, now, with_checkin=False)
        if alert is None:
            return None
        return alert


async def answered_ok(principal: Principal, device: dict, now: Optional[dt.datetime] = None,
                      incident_id: Optional[str] = None) -> Optional[dict]:
    """The person tapped I'm OK during the countdown. With an incident id, only an OK for the
    latest incident attached to the asking alert resolves it; an OK for an older one raises
    StaleAnswer and the alert keeps asking with its original deadline (D-092)."""
    now = now or _now()
    async with workspace_connection(principal) as conn:
        await lock_person(conn, device["person_id"])
        alert = await conn.fetchrow(
            "SELECT * FROM care_alerts WHERE person_id = $1 AND kind = $2 AND status = 'asking' "
            "ORDER BY opened_at DESC LIMIT 1", device["person_id"], KIND)
        if alert is None:
            return None
        details = _details(alert)
        ids = list(details.get("incident_ids") or [])
        if incident_id and ids and ids[-1] != incident_id:
            details.setdefault("answers_not_applied", []).append(
                {"incident_id": incident_id, "answer": "ok", "received_at": now.isoformat(),
                 "reason": "a later detection is attached to this alert"})
            await conn.execute("UPDATE care_alerts SET details = $2::jsonb WHERE id = $1", alert["id"],
                               json.dumps(details, default=str))
            raise StaleAnswer("A later detection is attached to this fall alert; it is still asking.")
        details["outcome"] = "answered_ok"
        row = await conn.fetchrow(
            "UPDATE care_alerts SET status = 'resolved', resolved_at = $2, next_action_at = NULL, resolution = $3, "
            "details = $4::jsonb WHERE id = $1 AND status = 'asking' RETURNING *",
            alert["id"], now, "The person answered the fall check: OK.", json.dumps(details, default=str))
        if row is None:
            return None
        out = dict(row)
        out["details"] = details
        return out


async def confirmed(conn, device: dict, body: dict, now: dt.datetime) -> Optional[dict]:
    """Help, or silence, during the countdown: the asking alert opens with the
    watch's location. A fall SOS already open is returned as it is (with the location if it had
    none), never a second one (D-092). None when no fall alert is unresolved (the SOS path then
    opens a suspected_fall alert of its own). The caller holds `lock_person`."""
    row = await conn.fetchrow(
        "UPDATE care_alerts SET status = 'open', next_action_at = $2, "
        "details = details || $3::jsonb WHERE person_id = $1 AND kind = $4 AND status = 'asking' RETURNING *",
        device["person_id"], now, json.dumps({"outcome": "no_answer" if body.get("via") == "fall" else "asked_for_help"}),
        KIND)
    from . import repository as repo
    loc = location.from_body(body.get("location"))
    if row is None:
        if body.get("via") != "fall":
            return None
        held = await unresolved_alert(conn, device["person_id"])
        if held is None:
            return None
        held = await attach(conn, held, {"incident_id": body.get("incident_id"), "detection": "help",
                                         "received_at": now.isoformat()})
        if held.get("location_latitude") is None:
            return await repo.attach_location(conn, held["id"], loc) or held
        return held
    return await repo.attach_location(conn, row["id"], loc) or repo._alert(row)
