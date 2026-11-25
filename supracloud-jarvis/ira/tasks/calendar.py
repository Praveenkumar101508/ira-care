"""
Calendar integration — Cal.com (primary) + Google Calendar (optional).

Cal.com (self-hosted or cloud):
  Set CALCOM_API_KEY and CALCOM_API_URL in .env.
  IRA reads upcoming bookings, creates events, and syncs to calendar_events table.

Google Calendar:
  Set GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_JSON in .env.
  Uses service account auth — no OAuth prompt needed for self-hosted.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from config import get_settings

logger = logging.getLogger("ira.calendar")


# ── Cal.com Integration ────────────────────────────────────────────────────────

async def sync_calcom_bookings(principal) -> int:
    """
    Fetch upcoming bookings from Cal.com and sync to calendar_events table.
    Returns the number of new events synced.
    """
    cfg = get_settings()
    if not cfg.calcom_api_key:
        logger.debug("Cal.com not configured — skipping sync")
        return 0

    try:
        async with httpx.AsyncClient(
            base_url=cfg.calcom_api_url,
            headers={"Authorization": f"Bearer {cfg.calcom_api_key}"},
            timeout=15,
        ) as client:
            resp = await client.get("/v2/bookings", params={"status": "upcoming"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"Cal.com sync failed: {type(e).__name__}")
        return 0

    bookings = data.get("bookings", [])
    synced = 0

    for booking in bookings:
        event_id = str(booking.get("id", ""))
        title = booking.get("title", "Meeting")
        start = booking.get("startTime")
        end = booking.get("endTime")
        attendees = booking.get("attendees", [])
        location = booking.get("location", "")

        if not start or not end:
            continue

        try:
            from core.identity.db_context import workspace_connection

            async with workspace_connection(principal) as conn:
                await conn.execute(
                    """INSERT INTO calendar_events
                       (external_id, source, title, attendees, start_at, end_at,
                        location, status, tenant_id, workspace_id, created_by)
                       VALUES ($1, 'calcom', $2, $3, $4, $5, $6, 'confirmed',
                               NULLIF(current_setting('app.tenant_id', true), '')::uuid, NULLIF(current_setting('app.workspace_id', true), '')::uuid,
                               (SELECT id FROM users WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid))
                       ON CONFLICT (tenant_id, workspace_id, external_id) DO UPDATE
                       SET title=EXCLUDED.title,
                           start_at=EXCLUDED.start_at,
                           end_at=EXCLUDED.end_at""",
                    event_id, title, json.dumps(attendees),
                    datetime.fromisoformat(start.replace("Z", "+00:00")),
                    datetime.fromisoformat(end.replace("Z", "+00:00")),
                    location,
                )
            synced += 1
        except Exception as e:
            logger.warning(f"Failed to sync booking {event_id}: {type(e).__name__}")

    if synced:
        logger.info(f"Cal.com: synced {synced} event(s)")
    return synced


async def create_calcom_booking(
    event_type_id: int,
    start: str,
    name: str,
    email: str,
    notes: str = "",
) -> dict | None:
    """
    Create a booking in Cal.com via the API.
    Requires CALCOM_API_KEY and a valid event_type_id.
    """
    cfg = get_settings()
    if not cfg.calcom_api_key:
        return None

    try:
        async with httpx.AsyncClient(
            base_url=cfg.calcom_api_url,
            headers={"Authorization": f"Bearer {cfg.calcom_api_key}"},
            timeout=15,
        ) as client:
            resp = await client.post("/v2/bookings", json={
                "eventTypeId": event_type_id,
                "start": start,
                "responses": {"name": name, "email": email, "notes": notes},
                "timeZone": "UTC",
                "language": "en",
            })
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Cal.com booking creation failed: {type(e).__name__}")
        return None


async def create_calcom_event(
    principal,
    event_type_id: int,
    start: str,
    name: str,
    email: str,
    notes: str = "",
    idempotency_key: str | None = None,
) -> dict | None:
    """
    Create a Cal.com booking and persist it to calendar_events.  # Feat P27

    idempotency_key: caller-supplied UUID; prevents duplicate bookings on retry.
    Returns None (not an error) when CALCOM_API_KEY is unset.
    """
    cfg = get_settings()
    if not cfg.calcom_api_key:
        logger.debug("Cal.com not configured — create_calcom_event is a no-op")
        return None

    ikey = idempotency_key or str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(
            base_url=cfg.calcom_api_url,
            headers={
                "Authorization": f"Bearer {cfg.calcom_api_key}",
                "Idempotency-Key": ikey,   # Feat P27: safe retries
            },
            timeout=15,
        ) as client:
            resp = await client.post("/v2/bookings", json={
                "eventTypeId": event_type_id,
                "start": start,
                "responses": {"name": name, "email": email, "notes": notes},
                "timeZone": "UTC",
                "language": "en",
            })
            resp.raise_for_status()
            booking = resp.json()
    except Exception as e:
        logger.error(f"Cal.com create_calcom_event failed: {type(e).__name__}")
        return None

    # Persist to local DB so get_upcoming_events works without a live API call
    external_id = str(booking.get("uid") or booking.get("id") or ikey)
    try:
        from core.identity.db_context import workspace_connection

        async with workspace_connection(principal) as conn:
            await conn.execute(
                """INSERT INTO calendar_events
                   (external_id, source, title, attendees, start_at, end_at,
                    location, status, tenant_id, workspace_id, created_by)
                   VALUES ($1, 'calcom', $2, $3, $4::timestamptz, $5::timestamptz, '', 'confirmed',
                           NULLIF(current_setting('app.tenant_id', true), '')::uuid, NULLIF(current_setting('app.workspace_id', true), '')::uuid,
                           (SELECT id FROM users WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid))
                   ON CONFLICT (tenant_id, workspace_id, external_id) DO NOTHING""",
                external_id,
                booking.get("title", "Meeting"),
                json.dumps([{"name": name, "email": email}]),
                start,
                booking.get("endTime", start),
            )
    except Exception as e:
        logger.warning(f"Failed to persist calcom event {external_id}: {type(e).__name__}")

    return booking


async def cancel_calcom_event(principal, external_id: str) -> bool:
    """
    Cancel a Cal.com booking by its external_id and mark it cancelled locally.  # Feat P27

    Returns True on success, False if the API call failed.
    Returns True (no-op) when CALCOM_API_KEY is unset so callers don't need to check.
    """
    cfg = get_settings()
    if not cfg.calcom_api_key:
        logger.debug("Cal.com not configured — cancel_calcom_event is a no-op")
        return True

    try:
        async with httpx.AsyncClient(
            base_url=cfg.calcom_api_url,
            headers={"Authorization": f"Bearer {cfg.calcom_api_key}"},
            timeout=15,
        ) as client:
            resp = await client.delete(f"/v2/bookings/{external_id}")
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Cal.com cancel_calcom_event({external_id}) failed: {type(e).__name__}")
        return False

    # Mark cancelled in local DB regardless of whether the row exists
    try:
        from core.identity.db_context import workspace_connection

        async with workspace_connection(principal) as conn:
            await conn.execute(
                "UPDATE calendar_events SET status='cancelled' WHERE external_id=$1",
                external_id,
            )
    except Exception as e:
        logger.warning(f"Failed to mark event {external_id} cancelled locally: {type(e).__name__}")

    return True


async def get_upcoming_events(principal, hours: int = 24) -> list[dict]:
    """Return calendar events starting within the next N hours."""
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """SELECT title, start_at, end_at, location, attendees
               FROM calendar_events
               WHERE start_at BETWEEN NOW() AND NOW() + make_interval(hours => $1)
               AND status = 'confirmed'
               ORDER BY start_at ASC""",
            hours,
        )
    return [
        {
            "title": r["title"],
            "start": r["start_at"].isoformat(),
            "end": r["end_at"].isoformat(),
            "location": r["location"],
            "attendees": r["attendees"],
        }
        for r in rows
    ]
