"""The true state of a Help request or a fall, as the watch must show it.

The watch never says help is coming unless the server has said so. The state
is computed here from the alert row and its acknowledgements and handed to the
watch on request and as a stream (`GET /devices/me/sos/stream`): one JSON line
per change, kept open, so a change on the dashboard or in the family app
reaches the wrist within a couple of seconds.

    received     the server has the request; the chain is running
    on_the_way   a responder said "I'm on my way"           -> "[Name] is on the way"
    arrived      that responder said they have arrived      -> "[Name] has arrived"
    closed       the alert was resolved or cancelled

"Sending…" and "Waiting for connection" exist only on the watch: they describe
the watch's own outbox before the server has answered.

Idempotency: every urgent event the watch sends carries an `X-Idempotency-Key`.
The first answer is stored in `care_device_events`; a repeat of the same key
gets the stored answer back and changes nothing (the outbox re-sends until it
hears a confirmation, so a flaky link must not open two alerts).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import AsyncIterator, Optional

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

logger = logging.getLogger("ira.care.sos")

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
EVENT_KINDS = ("help", "fall_started", "fall_ok", "checkin_answer", "checkin_missed")
#: Alert kinds the wrist shows as an SOS it is waiting on.
SOS_KINDS = ("help_requested", "suspected_fall", "heart_rate_outside_range", "no_answer_no_movement",
             "off_wrist_during_check", "concerning_phrase", "missed_checkins")
RESPONDER_KINDS = ("on_my_way", "arrived", "checked_in_person", "spoke_to_person", "handed_over")
STREAM_POLL_S = 2.0


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def state_of(alert: Optional[dict], acknowledgements: list[dict], *, responders: dict) -> dict:
    """The state the watch shows for one alert (or none).

    `responders` maps member id (or user id) to a display name. The newest
    acknowledgement decides: arrival beats "on my way"; a resolved alert is closed.
    """
    if alert is None:
        return {"state": "idle", "alert_id": None, "responder_name": None, "kind": None, "updated_at": None}
    out = {"alert_id": str(alert["id"]), "kind": alert["kind"], "responder_name": None,
           "updated_at": (alert.get("resolved_at") or alert.get("acknowledged_at") or alert["opened_at"]).isoformat()}
    if alert["status"] in ("resolved", "cancelled"):
        out["state"] = "closed"
        return out
    if alert["status"] == "asking":
        out["state"] = "asking"
        return out
    arrived = [a for a in acknowledgements if a["kind"] in ("arrived", "checked_in_person")]
    coming = [a for a in acknowledgements if a["kind"] in RESPONDER_KINDS]
    if arrived:
        a = max(arrived, key=lambda x: x["acknowledged_at"])
        out.update(state="arrived", responder_name=_name(a, responders), updated_at=a["acknowledged_at"].isoformat())
    elif coming:
        a = max(coming, key=lambda x: x["acknowledged_at"])
        out.update(state="on_the_way", responder_name=_name(a, responders), updated_at=a["acknowledged_at"].isoformat())
    else:
        out["state"] = "received"
    return out


def _name(ack: dict, responders: dict) -> str:
    return (responders.get(ack.get("member_id")) or responders.get(ack.get("user_id")) or "Someone")


async def current(conn, person_id) -> dict:
    """The newest unresolved SOS for the person, or the most recently closed one
    within the last ten minutes (so the watch sees the closing), or idle."""
    alert = await conn.fetchrow(
        """
        SELECT * FROM care_alerts
         WHERE person_id = $1 AND kind = ANY($2::text[]) AND severity <> 'notice'
           AND (status IN ('asking', 'open', 'escalating', 'acknowledged')
                OR resolved_at > NOW() - interval '10 minutes')
         ORDER BY (status IN ('asking', 'open', 'escalating', 'acknowledged')) DESC, opened_at DESC
         LIMIT 1
        """, person_id, list(SOS_KINDS))
    if alert is None:
        return state_of(None, [], responders={})
    acks = [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_acknowledgements WHERE alert_id = $1 ORDER BY acknowledged_at", alert["id"])]
    responders: dict = {}
    if acks:
        rows = await conn.fetch(
            "SELECT id, user_id, display_name FROM care_circle_members WHERE person_id = $1", person_id)
        for r in rows:
            responders[r["id"]] = r["display_name"]
            if r["user_id"] is not None:
                responders.setdefault(r["user_id"], r["display_name"])
        missing = [a["user_id"] for a in acks if a.get("user_id") and a["user_id"] not in responders]
        if missing:
            for r in await conn.fetch("SELECT id, display_name FROM users WHERE id = ANY($1::uuid[])", missing):
                responders[r["id"]] = r["display_name"] or "Someone"
    return state_of(dict(alert), acks, responders=responders)


async def current_for_device(principal: Principal, device: dict) -> dict:
    async with workspace_connection(principal) as conn:
        return await current(conn, device["person_id"])


async def stream(principal: Principal, device: dict, *, poll_s: float = STREAM_POLL_S,
                 max_seconds: float = 300.0, now=None) -> AsyncIterator[dict]:
    """Yield the state at once, then every time it changes, for up to
    `max_seconds` (the watch reconnects). A heartbeat line every 30 s keeps the
    connection known to be alive."""
    last: Optional[str] = None
    started = asyncio.get_event_loop().time()
    silent = 0.0
    while asyncio.get_event_loop().time() - started < max_seconds:
        try:
            state = await current_for_device(principal, device)
        except Exception:  # noqa: BLE001 - the stream must not die on one failed read
            logger.exception("sos stream read failed")
            state = None
        if state is not None:
            key = json.dumps(state, sort_keys=True)
            if key != last:
                last = key
                silent = 0.0
                yield state
        if silent >= 30.0:
            silent = 0.0
            yield {"heartbeat": dt.datetime.now(dt.timezone.utc).isoformat()}
        await asyncio.sleep(poll_s)
        silent += poll_s


# ── Idempotency for the watch's urgent events ────────────────────────────────

class Duplicate(Exception):
    """A key already answered: carry the stored answer back."""

    def __init__(self, status_code: int, response: dict):
        super().__init__("duplicate event")
        self.status_code = status_code
        self.response = response


async def replay(principal: Principal, device: dict, key: Optional[str]) -> Optional[tuple[int, dict]]:
    """The stored answer for `key`, or None when the key is new (or absent)."""
    if not key:
        return None
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT status_code, response FROM care_device_events WHERE device_id = $1 AND idempotency_key = $2",
            device["id"], key)
    if row is None:
        return None
    return int(row["status_code"]), _json(row["response"])


async def remember(principal: Principal, device: dict, key: Optional[str], kind: str,
                   status_code: int, response: dict) -> None:
    """Store the answer under the key. A race on the same key keeps the first."""
    if not key or kind not in EVENT_KINDS:
        return
    async with workspace_connection(principal) as conn:
        await conn.execute(
            """
            INSERT INTO care_device_events (person_id, device_id, idempotency_key, kind, status_code, response)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (device_id, idempotency_key) DO NOTHING
            """, device["person_id"], device["id"], key, kind, status_code, json.dumps(response, default=str))


def watch_settings(settings: dict) -> dict:
    """What the watch holds so it can act before the server does: the carer's
    heart-rate range (an out-of-range sample is uploaded at once) and how often
    ordinary readings go up."""
    return {
        "heart_rate_min": settings.get("heart_rate_min"),
        "heart_rate_max": settings.get("heart_rate_max"),
        "reading_upload_interval_s": int(settings.get("reading_upload_interval_s", 60)),
    }


__all__ = ["state_of", "current", "current_for_device", "stream", "replay", "remember", "watch_settings",
           "IDEMPOTENCY_HEADER", "EVENT_KINDS", "SOS_KINDS", "Duplicate"]
