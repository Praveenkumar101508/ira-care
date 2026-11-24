"""Reads and writes for the care tables.

Every function takes the caller's verified `Principal` and opens
`workspace_connection(principal)`, so the connection carries the tenant and
workspace that row-level security checks. No query here names a tenant or a
workspace in a WHERE clause: the boundary is the connection's, not the SQL's.

Functions that READ health data or transcripts take the connection from the
route instead, so the route can write the access-log row in the same
transaction as the read.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import replace
from typing import Any, Optional

from core.identity.background import background_principal
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import devices as _devices


def _d(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def _alert(row) -> Optional[dict]:
    """An alert row with its JSON details decoded (asyncpg hands jsonb back as text)."""
    d = _d(row)
    if d is not None and isinstance(d.get("details"), str):
        try:
            d["details"] = json.loads(d["details"])
        except ValueError:
            d["details"] = {}
    return d


def _ds(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── People ───────────────────────────────────────────────────────────────────

#: Columns never returned to a client: the sealed access notes.
_SEALED_COLUMNS = ("access_notes_ciphertext", "access_notes_nonce", "access_notes_wrapped_dek",
                   "access_notes_wrap_nonce", "access_notes_kek_id", "access_notes_key_version")


def _person(row) -> Optional[dict]:
    """A person row for a client: the sealed notes are replaced by a flag."""
    d = _d(row)
    if d is None:
        return None
    d["has_access_notes"] = d.get("access_notes_ciphertext") is not None
    for c in _SEALED_COLUMNS:
        d.pop(c, None)
    return d


async def create_person(principal: Principal, body: dict) -> dict:
    from . import accessnotes
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO care_people
                (display_name, preferred_name, date_of_birth, timezone, language, notes, created_by,
                 home_address, home_latitude, home_longitude, home_radius_m, emergency_summary)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
            """,
            body["display_name"], body.get("preferred_name", ""), body.get("date_of_birth"),
            body.get("timezone", "Europe/London"), body.get("language", "en"),
            body.get("notes", ""), principal.user_id,
            body.get("home_address") or "", body.get("home_latitude"), body.get("home_longitude"),
            body.get("home_radius_m") or 150, body.get("emergency_summary") or "",
        )
        if body.get("access_notes"):
            cols = accessnotes.seal(body["access_notes"], tenant_id=principal.tenant_id, person_id=row["id"])
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(accessnotes.COLUMNS))
            row = await conn.fetchrow(f"UPDATE care_people SET {sets} WHERE id = $1 RETURNING *",
                                      row["id"], *[cols[c] for c in accessnotes.COLUMNS])
        # Settings exist from the first moment, at the documented defaults, so a
        # person is never monitored against settings that do not exist.
        await conn.execute(
            "INSERT INTO care_safety_settings (person_id, set_by) VALUES ($1, $2)",
            row["id"], principal.user_id,
        )
        return _person(row)


async def list_people(principal: Principal, *, include_archived: bool = False) -> list[dict]:
    async with workspace_connection(principal) as conn:
        if include_archived:
            rows = await conn.fetch("SELECT * FROM care_people ORDER BY display_name")
        else:
            rows = await conn.fetch(
                "SELECT * FROM care_people WHERE status <> 'archived' ORDER BY display_name")
        return [_person(r) for r in rows]


async def get_person(principal: Principal, person_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        return _person(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id))


_PERSON_FIELDS = ("display_name", "preferred_name", "date_of_birth", "timezone",
                  "language", "notes", "status", "home_address", "home_latitude", "home_longitude",
                  "home_radius_m", "emergency_summary")


async def update_person(principal: Principal, person_id: uuid.UUID, changes: dict) -> Optional[dict]:
    sets, args = [], []
    for field in _PERSON_FIELDS:
        if field in changes and changes[field] is not None:
            args.append(changes[field])
            sets.append(f"{field} = ${len(args)}")
    notes = changes.get("access_notes")
    if notes is not None:
        from . import accessnotes
        cols = accessnotes.seal(notes, tenant_id=principal.tenant_id, person_id=person_id)
        for col in accessnotes.COLUMNS:
            args.append(cols[col])
            sets.append(f"{col} = ${len(args)}")
    if not sets:
        return await get_person(principal, person_id)
    args.append(person_id)
    async with workspace_connection(principal) as conn:
        return _person(await conn.fetchrow(
            f"UPDATE care_people SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE id = ${len(args)} RETURNING *", *args))


async def open_access_notes(principal: Principal, alert_id: uuid.UUID) -> Optional[dict]:
    """The access notes behind an alert, for a tier 1 or 2 recipient while it is
    open; None when the alert does not exist; {"allowed": False} when the
    caller may not see them. Every successful opening is written to the access
    log with the alert it was for."""
    from . import accessnotes, audit, authz
    async with workspace_connection(principal) as conn:
        alert = await conn.fetchrow("SELECT id, person_id, status FROM care_alerts WHERE id = $1", alert_id)
        if alert is None:
            return None
        circle = _ds(await conn.fetch(
            "SELECT user_id, tier, active FROM care_circle_members WHERE person_id = $1", alert["person_id"]))
        if not accessnotes.can_view(alert["status"], viewer_user_id=principal.user_id, circle=circle,
                                   is_manager=authz.can_manage(principal)):
            return {"allowed": False, "alert_id": alert_id, "status": alert["status"]}
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", alert["person_id"])
        text = accessnotes.use_notes(dict(person), lambda t: t, tenant_id=principal.tenant_id)
        await audit.record_access(conn, principal, person_id=alert["person_id"], resource_kind="access_notes",
                                  resource_id=alert_id, action="view",
                                  detail={"alert_status": alert["status"], "characters": len(text)})
        return {"allowed": True, "alert_id": alert_id, "status": alert["status"], "access_notes": text,
                "emergency_summary": person["emergency_summary"], "home_address": person["home_address"]}


async def read_access_notes(principal: Principal, person_id: uuid.UUID) -> Optional[dict]:
    """The access notes (key safe, door, pets) on the person's profile, for a
    manager of the care organisation only, outside any alert. Every reading is
    written to the access log; None when the person does not exist."""
    from . import accessnotes, audit, authz
    async with workspace_connection(principal) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
        if person is None:
            return None
        if not authz.can_manage(principal):
            return {"allowed": False}
        text = accessnotes.use_notes(dict(person), lambda t: t, tenant_id=principal.tenant_id)
        await audit.record_access(conn, principal, person_id=person_id, resource_kind="access_notes",
                                  resource_id=person_id, action="view", detail={"from": "profile", "characters": len(text)})
        return {"allowed": True, "access_notes": text, "home_address": person["home_address"], "emergency_summary": person["emergency_summary"]}


async def erase_person(principal: Principal, person_id: uuid.UUID) -> bool:
    """Delete the person and, by cascade, everything recorded about them.

    The access log is the one thing that stays: it has no foreign key to the
    person, and it is the record that the data existed and who looked at it.
    """
    async with workspace_connection(principal) as conn:
        tag = await conn.execute("DELETE FROM care_people WHERE id = $1", person_id)
        return tag.endswith(" 1")


# ── Care circle ──────────────────────────────────────────────────────────────

async def add_member(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO care_circle_members
                (person_id, user_id, display_name, relationship, role, tier, priority,
                 phone, email, push_enabled, sms_enabled, call_enabled)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
            """,
            person_id, body.get("user_id"), body["display_name"], body.get("relationship", ""),
            body["role"], body.get("tier", 1), body.get("priority", 100), body.get("phone", ""),
            body.get("email", ""), body.get("push_enabled", True), body.get("sms_enabled", True),
            body.get("call_enabled", True),
        )
        return dict(row)


async def list_members(principal: Principal, person_id: uuid.UUID) -> list[dict]:
    async with workspace_connection(principal) as conn:
        return _ds(await conn.fetch(
            "SELECT * FROM care_circle_members WHERE person_id = $1 "
            "ORDER BY active DESC, tier, priority, display_name", person_id))


_MEMBER_FIELDS = ("display_name", "relationship", "role", "tier", "priority", "phone", "email",
                  "push_enabled", "sms_enabled", "call_enabled", "active")


async def update_member(principal: Principal, member_id: uuid.UUID, changes: dict) -> Optional[dict]:
    sets, args = [], []
    for field in _MEMBER_FIELDS:
        if field in changes and changes[field] is not None:
            args.append(changes[field])
            sets.append(f"{field} = ${len(args)}")
    async with workspace_connection(principal) as conn:
        if not sets:
            return _d(await conn.fetchrow("SELECT * FROM care_circle_members WHERE id = $1", member_id))
        args.append(member_id)
        row = _d(await conn.fetchrow(
            f"UPDATE care_circle_members SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE id = ${len(args)} RETURNING *", *args))
        if row is not None and (changes.get("active") is False or changes.get("push_enabled") is False):
            # A member who left the circle, or turned push off, is pushed to no more.
            from . import push as _push
            await _push.revoke_for_member(conn, member_id, "member left the circle" if changes.get("active") is False
                                          else "push turned off for this member")
        return row


async def remove_member(principal: Principal, member_id: uuid.UUID) -> bool:
    async with workspace_connection(principal) as conn:
        tag = await conn.execute("DELETE FROM care_circle_members WHERE id = $1", member_id)
        return tag.endswith(" 1")


async def circle_role_of(conn, person_id: uuid.UUID, user_id: uuid.UUID) -> Optional[str]:
    """The caller's role in this person's circle, or None if not a member."""
    return await conn.fetchval(
        "SELECT role FROM care_circle_members WHERE person_id = $1 AND user_id = $2 AND active "
        "ORDER BY tier, priority LIMIT 1", person_id, user_id)


# ── Safety settings ──────────────────────────────────────────────────────────

async def get_settings(principal: Principal, person_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        return _d(await conn.fetchrow(
            "SELECT * FROM care_safety_settings WHERE person_id = $1", person_id))


_SETTINGS_FIELDS = (
    "quiet_start", "quiet_end", "checkin_min_minutes", "checkin_max_minutes", "repeat_after_s",
    "sensor_check_after_s", "heart_rate_min", "heart_rate_max", "no_movement_window_s",
    "movement_floor_steps", "off_wrist_alert_minutes", "battery_alert_percent",
    "sync_alert_minutes", "missed_checkins_before_escalation", "ack_wait_minutes",
    "ask_first_wait_s", "heart_rate_confirm_readings", "heart_rate_confirm_seconds",
    "night_off_wrist_minutes", "usual_wake_time", "wake_grace_minutes",
    "fall_detection_enabled", "fall_countdown_s", "fall_impact_g", "fall_still_seconds", "fall_still_threshold_g",
    "reading_upload_interval_s", "follow_up_minutes", "follow_up_grace_minutes", "visit_late_minutes",
    "hydration_every_minutes",
)


async def put_settings(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    cols = list(_SETTINGS_FIELDS)
    values = [body.get(c) for c in cols]
    placeholders = ", ".join(f"${i + 3}" for i in range(len(cols)))
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        return _d(await conn.fetchrow(
            f"""
            INSERT INTO care_safety_settings (person_id, set_by, {', '.join(cols)})
            VALUES ($1, $2, {placeholders})
            ON CONFLICT (person_id) DO UPDATE
               SET {assignments}, set_by = EXCLUDED.set_by, updated_at = NOW()
            RETURNING *
            """,
            person_id, principal.user_id, *values))


# ── Devices ──────────────────────────────────────────────────────────────────

async def create_device(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    """A pending device and its one-time pairing code (returned once, in clear)."""
    code = _devices.new_pairing_code(principal.tenant_id, principal.require_workspace())
    encoded = code.encode()
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO care_devices
                (person_id, platform, model, status, pairing_code_hash, pairing_expires_at,
                 expected_report_interval_s)
            VALUES ($1, $2, $3, 'pending', $4, $5, $6)
            RETURNING *
            """,
            person_id, body.get("platform", "wearos"), body.get("model", ""),
            _devices.digest(encoded), _now() + _devices.PAIRING_CODE_TTL,
            body.get("expected_report_interval_s", 300),
        )
    out = _public_device(dict(row))
    out["pairing_code"] = encoded
    return out


def _public_device(row: dict) -> dict:
    row.pop("pairing_code_hash", None)
    row.pop("token_hash", None)
    return row


async def list_devices(principal: Principal, person_id: uuid.UUID) -> list[dict]:
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT * FROM care_devices WHERE person_id = $1 ORDER BY created_at DESC", person_id)
        return [_public_device(dict(r)) for r in rows]


async def revoke_device(principal: Principal, device_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_devices SET status = 'revoked', token_hash = NULL, pairing_code_hash = NULL "
            "WHERE id = $1 RETURNING *", device_id)
        return _public_device(dict(row)) if row else None


def device_principal(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Principal:
    """A principal for a watch: no roles, no scopes, bound to the credential's
    tenant and workspace. It can reach exactly the rows the watch's own family
    can, and nothing of anyone else's."""
    return replace(background_principal(tenant_id, job="care-device"), workspace_id=workspace_id)


async def pair_device(pairing_code: str, *, app_version: str = "") -> Optional[dict]:
    """Exchange a pairing code for a device token. None if the code is unknown,
    expired, already used or malformed. The token is returned once, in clear."""
    cred = _devices.parse(pairing_code)
    if cred is None or cred.kind != "pairing":
        return None
    principal = device_principal(cred.tenant_id, cred.workspace_id)
    token = _devices.new_device_token(cred.tenant_id, cred.workspace_id)
    encoded = token.encode()
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            """
            UPDATE care_devices
               SET status = 'paired', token_hash = $2, pairing_code_hash = NULL,
                   pairing_expires_at = NULL, paired_at = NOW(), last_seen_at = NOW(),
                   app_version = $3
             WHERE pairing_code_hash = $1 AND status = 'pending' AND pairing_expires_at > NOW()
            RETURNING *
            """,
            _devices.digest(pairing_code), _devices.digest(encoded), app_version[:40])
    if row is None:
        return None
    out = _public_device(dict(row))
    out["device_token"] = encoded
    return out


async def authenticate_device(token: str) -> Optional[tuple[Principal, dict]]:
    """The device a token belongs to, inside the token's own scope, or None."""
    cred = _devices.parse(token)
    if cred is None or cred.kind != "token":
        return None
    principal = device_principal(cred.tenant_id, cred.workspace_id)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_devices SET last_seen_at = NOW() "
            "WHERE token_hash = $1 AND status = 'paired' RETURNING *", _devices.digest(token))
    if row is None:
        return None
    return principal, _public_device(dict(row))


async def record_report(principal: Principal, device: dict, report: dict) -> dict:
    """Store a watch's readings and state. Re-sent readings are ignored, not
    duplicated (unique on device, kind, recorded_at)."""
    readings = report.get("readings") or []
    async with workspace_connection(principal) as conn:
        inserted = 0
        if readings:
            rows = await conn.fetch(
                """
                INSERT INTO care_vitals (person_id, device_id, kind, value, unit, recorded_at, source)
                SELECT $1, $2, r.kind, r.value, r.unit, r.recorded_at, 'watch'
                  FROM jsonb_to_recordset($3::jsonb)
                       AS r(kind text, value double precision, unit text, recorded_at timestamptz)
                ON CONFLICT (device_id, kind, recorded_at) WHERE device_id IS NOT NULL DO NOTHING
                RETURNING id
                """,
                device["person_id"], device["id"],
                json.dumps([{
                    "kind": r["kind"], "value": r["value"], "unit": r.get("unit", ""),
                    "recorded_at": r["recorded_at"].isoformat(),
                } for r in readings]),
            )
            inserted = len(rows)
        await conn.execute(
            """
            UPDATE care_devices
               SET last_sync_at = NOW(), last_seen_at = NOW(),
                   battery_percent = COALESCE($2, battery_percent),
                   on_wrist = COALESCE($3, on_wrist),
                   background_sensor_permission = COALESCE($4, background_sensor_permission),
                   app_version = COALESCE($5, app_version)
             WHERE id = $1
            """,
            device["id"], report.get("battery_percent"), report.get("on_wrist"),
            report.get("background_sensor_permission"), report.get("app_version"))
        # Every batch is checked as it arrives, day and night (care/continuous.py).
        asked = await evaluate_arrivals(conn, device["person_id"], now=report.get("now"))
    out = {"accepted": len(readings), "stored": inserted}
    if asked is not None:
        out["safety_check"] = {"alert_id": str(asked["id"]), "kind": asked["kind"]}
    return out


async def evaluate_arrivals(conn, person_id, *, now: Optional[dt.datetime] = None) -> Optional[dict]:
    """Run the arrival-time rules for one person on the readings just stored.
    Opens an `asking` alert (ask first) when a heart rate is confirmed outside
    the carer's range. Returns the alert opened, or None."""
    from . import askfirst, continuous
    from .monitoring import Observation
    now = now or _now()
    person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1 AND status = 'active'", person_id)
    settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", person_id)
    if person is None or settings is None:
        return None
    window = max(int(settings["heart_rate_confirm_seconds"]) * 3, 600)
    readings = [dict(r) for r in await conn.fetch(
        "SELECT kind, value, recorded_at FROM care_vitals WHERE person_id = $1 AND kind = 'heart_rate' "
        "AND recorded_at >= $2 ORDER BY recorded_at", person_id, now - dt.timedelta(seconds=window))]
    breach = continuous.confirmed_outside_range(readings, dict(settings))
    if breach is None:
        return None
    obs = Observation("heart_rate_outside_range", "sos", breach.summary,
                      {"values": list(breach.values), "range": [breach.low, breach.high],
                       "first_at": breach.first_at.isoformat(), "last_at": breach.last_at.isoformat()})
    return await askfirst.start(conn, dict(person), dict(settings), obs, now)


# ── Vitals ───────────────────────────────────────────────────────────────────

async def fetch_vitals(conn, person_id: uuid.UUID, *, kind: Optional[str], since: Optional[dt.datetime],
                       until: Optional[dt.datetime], limit: int) -> list[dict]:
    return _ds(await conn.fetch(
        """
        SELECT id, device_id, kind, value, unit, recorded_at, received_at, source
          FROM care_vitals
         WHERE person_id = $1
           AND ($2::text IS NULL OR kind = $2)
           AND ($3::timestamptz IS NULL OR recorded_at >= $3)
           AND ($4::timestamptz IS NULL OR recorded_at <= $4)
         ORDER BY recorded_at DESC
         LIMIT $5
        """, person_id, kind, since, until, limit))


async def latest_vitals(conn, person_id: uuid.UUID) -> list[dict]:
    return _ds(await conn.fetch(
        """
        SELECT DISTINCT ON (kind) kind, value, unit, recorded_at, received_at, source
          FROM care_vitals WHERE person_id = $1
         ORDER BY kind, recorded_at DESC
        """, person_id))


# ── Check-ins (read side; the engine that writes them is phase 3) ────────────

async def fetch_checkins(conn, person_id: uuid.UUID, *, limit: int, with_answers: bool) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM care_checkins WHERE person_id = $1 ORDER BY scheduled_at DESC LIMIT $2",
        person_id, limit)
    out = []
    for r in rows:
        d = dict(r)
        if not with_answers:
            d["answer_text"] = ""
        out.append(d)
    return out


# ── Alerts ───────────────────────────────────────────────────────────────────

async def open_alert(conn, person_id: uuid.UUID, *, kind: str, severity: str,
                     details: Optional[dict] = None, checkin_id: Optional[uuid.UUID] = None) -> dict:
    return _alert(await conn.fetchrow(
        """
        INSERT INTO care_alerts (person_id, checkin_id, kind, severity, status, details, next_action_at)
        VALUES ($1, $2, $3, $4, 'open', $5::jsonb, NOW())
        RETURNING *
        """, person_id, checkin_id, kind, severity, json.dumps(details or {})))


async def record_help_request(principal: Principal, device: dict, body: dict) -> dict:
    from . import askfirst, falls, location
    loc = location.from_body(body.get("location"))
    now = _now()
    async with workspace_connection(principal) as conn:
        # A fall countdown that ended, or Help during one, opens the fall alert itself.
        fall = await falls.confirmed(conn, device, body, now)
        if fall is not None:
            return fall
        promoted = await askfirst.promote_on_help(conn, device["person_id"], now)
        if promoted is not None:
            return await attach_location(conn, promoted["id"], loc) or _alert(promoted)
        if body.get("via") == "fall":
            # The watch could not reach us when its countdown started: the SOS still says what it was.
            alert = await open_alert(
                conn, device["person_id"], kind=falls.KIND, severity="sos",
                details={"via": "fall", "summary": falls.summary(None, None).replace(
                    " The person is being asked if they are OK.", " Nobody answered the watch's question."),
                         "outcome": "no_answer", "device_id": str(device["id"])})
        else:
            alert = await open_alert(
                conn, device["person_id"], kind="help_requested", severity="sos",
                details={"via": body.get("via", "button"), "note": body.get("note", ""),
                         "device_id": str(device["id"])})
        return await attach_location(conn, alert["id"], loc) or alert


async def attach_location(conn, alert_id: uuid.UUID, loc) -> Optional[dict]:
    """Store the watch's one-off location on the alert and decide at-home from
    the person's home coordinates. None when there is no location to store."""
    from . import location
    if loc is None:
        return None
    person = await conn.fetchrow(
        "SELECT p.* FROM care_people p JOIN care_alerts a ON a.person_id = p.id WHERE a.id = $1", alert_id)
    home = location.at_home(loc, dict(person)) if person else None
    return _alert(await conn.fetchrow(
        """
        UPDATE care_alerts SET location_latitude = $2, location_longitude = $3, location_accuracy_m = $4,
               location_at = COALESCE($5, NOW()), at_home = $6
         WHERE id = $1 RETURNING *
        """, alert_id, loc.latitude, loc.longitude, loc.accuracy_m, loc.recorded_at, home))


async def list_alerts(principal: Principal, *, person_id: Optional[uuid.UUID], open_only: bool,
                      limit: int) -> list[dict]:
    async with workspace_connection(principal) as conn:
        return [_alert(r) for r in await conn.fetch(
            """
            SELECT * FROM care_alerts
             WHERE ($1::uuid IS NULL OR person_id = $1)
               AND (NOT $2 OR status IN ('open', 'escalating'))
             ORDER BY opened_at DESC LIMIT $3
            """, person_id, open_only, limit)]


async def get_alert(principal: Principal, alert_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        alert = _alert(await conn.fetchrow("SELECT * FROM care_alerts WHERE id = $1", alert_id))
        if alert is None:
            return None
        alert["attempts"] = _ds(await conn.fetch(
            "SELECT * FROM care_alert_attempts WHERE alert_id = $1 ORDER BY attempted_at", alert_id))
        # Push delivery, as the service reported it: per attempt, from its tickets.
        from . import push as _push
        tickets = _ds(await conn.fetch(
            "SELECT attempt_id, status, detail, receipt_at FROM care_push_tickets WHERE alert_id = $1 ORDER BY created_at", alert_id))
        for a in alert["attempts"]:
            if a["channel"] == "push":
                mine = [t for t in tickets if t["attempt_id"] == a["id"]]
                a["delivery"] = _push.delivery_state(mine)
                a["delivered_at"] = max((t["receipt_at"] for t in mine if t["status"] == "delivered" and t["receipt_at"]), default=None)
        alert["acknowledgements"] = _ds(await conn.fetch(
            "SELECT * FROM care_acknowledgements WHERE alert_id = $1 ORDER BY acknowledged_at", alert_id))
        return alert


async def acknowledge_alert(principal: Principal, alert_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        alert = await conn.fetchrow("SELECT * FROM care_alerts WHERE id = $1", alert_id)
        if alert is None:
            return None
        member_id = await conn.fetchval(
            "SELECT id FROM care_circle_members WHERE person_id = $1 AND user_id = $2 AND active "
            "ORDER BY tier, priority LIMIT 1", alert["person_id"], principal.user_id)
        kind = body.get("kind", "on_my_way")
        await conn.execute(
            "INSERT INTO care_acknowledgements (alert_id, member_id, user_id, kind, note) "
            "VALUES ($1, $2, $3, $4, $5)",
            alert_id, member_id, principal.user_id, kind, body.get("note", ""))
        if kind == "on_my_way":
            # The promise starts a deadline (care/followup.py): an arrival or an outcome is
            # expected within follow_up_minutes, or the responder is asked and the chain restarts.
            # Atomic on the status, so two responders at once change it once.
            settings = await conn.fetchrow("SELECT follow_up_minutes FROM care_safety_settings WHERE person_id = $1",
                                           alert["person_id"])
            minutes = int(settings["follow_up_minutes"]) if settings else 30
            await conn.execute(
                "UPDATE care_alerts SET status = 'acknowledged', acknowledged_at = NOW(), next_action_at = NULL, "
                "follow_up_due_at = NOW() + make_interval(mins => $2), follow_up_asked_at = NULL "
                "WHERE id = $1 AND status IN ('open', 'escalating', 'acknowledged')", alert_id, minutes)
        elif kind in ("arrived", "checked_in_person", "spoke_to_person"):
            # Someone reached the person: the deadline is met; the alert still waits for its outcome.
            await conn.execute(
                "UPDATE care_alerts SET follow_up_due_at = NULL, status = CASE WHEN status IN ('open', 'escalating') "
                "THEN 'acknowledged' ELSE status END, acknowledged_at = COALESCE(acknowledged_at, NOW()), "
                "next_action_at = CASE WHEN status IN ('open', 'escalating') THEN NULL ELSE next_action_at END "
                "WHERE id = $1 AND status <> 'resolved'", alert_id)
    return await get_alert(principal, alert_id)


async def resolve_alert(principal: Principal, alert_id: uuid.UUID, resolution: str, *,
                        outcome: str = "other") -> Optional[dict]:
    """Close the alert with its outcome. Idempotent: a second close at the same
    moment finds it closed and returns it unchanged (the first outcome stands)."""
    async with workspace_connection(principal) as conn:
        exists = await conn.fetchval("SELECT 1 FROM care_alerts WHERE id = $1", alert_id)
        if not exists:
            return None
        await conn.execute(
            "UPDATE care_alerts SET status = 'resolved', resolved_at = NOW(), resolution = $2, outcome = $3, "
            "next_action_at = NULL, follow_up_due_at = NULL WHERE id = $1 AND status <> 'resolved'",
            alert_id, resolution, outcome)
    return await get_alert(principal, alert_id)


# ── Routines ─────────────────────────────────────────────────────────────────

async def add_routine(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        return _d(await conn.fetchrow(
            "INSERT INTO care_routines (person_id, name, kind, time_of_day, days_of_week, active) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            person_id, body["name"], body["kind"], body.get("time_of_day"),
            body.get("days_of_week", [1, 2, 3, 4, 5, 6, 7]), body.get("active", True)))


async def list_routines(principal: Principal, person_id: uuid.UUID) -> list[dict]:
    async with workspace_connection(principal) as conn:
        return _ds(await conn.fetch(
            "SELECT * FROM care_routines WHERE person_id = $1 ORDER BY time_of_day NULLS LAST, name",
            person_id))


_ROUTINE_FIELDS = ("name", "kind", "time_of_day", "days_of_week", "active")


async def update_routine(principal: Principal, routine_id: uuid.UUID, changes: dict) -> Optional[dict]:
    sets, args = [], []
    for field in _ROUTINE_FIELDS:
        if field in changes and changes[field] is not None:
            args.append(changes[field])
            sets.append(f"{field} = ${len(args)}")
    async with workspace_connection(principal) as conn:
        if not sets:
            return _d(await conn.fetchrow("SELECT * FROM care_routines WHERE id = $1", routine_id))
        args.append(routine_id)
        return _d(await conn.fetchrow(
            f"UPDATE care_routines SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE id = ${len(args)} RETURNING *", *args))


async def delete_routine(principal: Principal, routine_id: uuid.UUID) -> bool:
    async with workspace_connection(principal) as conn:
        return (await conn.execute("DELETE FROM care_routines WHERE id = $1", routine_id)).endswith(" 1")


# ── Medication ───────────────────────────────────────────────────────────────

async def add_medication(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        return _d(await conn.fetchrow(
            "INSERT INTO care_medications (person_id, name, dose, times, notes, active) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            person_id, body["name"], body.get("dose", ""), body.get("times", []),
            body.get("notes", ""), body.get("active", True)))


async def list_medications(conn, person_id: uuid.UUID) -> list[dict]:
    return _ds(await conn.fetch(
        "SELECT * FROM care_medications WHERE person_id = $1 ORDER BY active DESC, name", person_id))


_MED_FIELDS = ("name", "dose", "times", "notes", "active")


async def update_medication(principal: Principal, medication_id: uuid.UUID, changes: dict) -> Optional[dict]:
    sets, args = [], []
    for field in _MED_FIELDS:
        if field in changes and changes[field] is not None:
            args.append(changes[field])
            sets.append(f"{field} = ${len(args)}")
    async with workspace_connection(principal) as conn:
        if not sets:
            return _d(await conn.fetchrow("SELECT * FROM care_medications WHERE id = $1", medication_id))
        args.append(medication_id)
        return _d(await conn.fetchrow(
            f"UPDATE care_medications SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE id = ${len(args)} RETURNING *", *args))


async def delete_medication(principal: Principal, medication_id: uuid.UUID) -> bool:
    async with workspace_connection(principal) as conn:
        return (await conn.execute("DELETE FROM care_medications WHERE id = $1", medication_id)).endswith(" 1")


async def record_medication_event(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval(
                "SELECT 1 FROM care_medications WHERE id = $1 AND person_id = $2",
                body["medication_id"], person_id):
            return None
        return _d(await conn.fetchrow(
            """
            INSERT INTO care_medication_events (medication_id, person_id, due_at, status, confirmed_via)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (medication_id, due_at) DO UPDATE
               SET status = EXCLUDED.status, confirmed_via = EXCLUDED.confirmed_via, recorded_at = NOW()
            RETURNING *
            """, body["medication_id"], person_id, body["due_at"], body["status"],
            body.get("confirmed_via", "carer")))


async def list_medication_events(conn, person_id: uuid.UUID, *, limit: int) -> list[dict]:
    return _ds(await conn.fetch(
        "SELECT * FROM care_medication_events WHERE person_id = $1 ORDER BY due_at DESC LIMIT $2",
        person_id, limit))


# ── Transcripts and consents ─────────────────────────────────────────────────

async def fetch_transcripts(conn, person_id: uuid.UUID, *, limit: int) -> list[dict]:
    return _ds(await conn.fetch(
        "SELECT * FROM care_transcripts WHERE person_id = $1 ORDER BY created_at DESC LIMIT $2",
        person_id, limit))


async def fetch_consents(conn, person_id: uuid.UUID) -> list[dict]:
    return _ds(await conn.fetch(
        "SELECT * FROM care_consents WHERE person_id = $1 ORDER BY recorded_at", person_id))


async def record_consent(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        return _d(await conn.fetchrow(
            """
            INSERT INTO care_consents
                (person_id, kind, granted, granted_by, granted_by_name, retention_months, note, recorded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """, person_id, body["kind"], body["granted"], body["granted_by"],
            body.get("granted_by_name", ""), body.get("retention_months"), body.get("note", ""),
            principal.user_id))


async def revoke_consent(principal: Principal, consent_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        return _d(await conn.fetchrow(
            "UPDATE care_consents SET revoked_at = NOW() WHERE id = $1 AND revoked_at IS NULL RETURNING *",
            consent_id))


# ── Access log ───────────────────────────────────────────────────────────────

async def fetch_access_log(principal: Principal, person_id: uuid.UUID, *, limit: int) -> Optional[list[dict]]:
    """None when the person is not visible to the caller: a 404, not an empty log."""
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        rows = _ds(await conn.fetch(
            "SELECT * FROM care_access_log WHERE person_id = $1 ORDER BY at DESC LIMIT $2",
            person_id, limit))
        for r in rows:
            if isinstance(r.get("detail"), str):
                try:
                    r["detail"] = json.loads(r["detail"])
                except ValueError:
                    r["detail"] = {}
        return rows


# ── Status ───────────────────────────────────────────────────────────────────

async def person_status(conn, person_id: uuid.UUID) -> Optional[dict[str, Any]]:
    person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
    if person is None:
        return None
    devices = await conn.fetch(
        "SELECT id, platform, status, battery_percent, on_wrist, background_sensor_permission, "
        "last_sync_at, last_seen_at FROM care_devices WHERE person_id = $1 AND status = 'paired'",
        person_id)
    open_alerts = await conn.fetch(
        "SELECT id, kind, severity, status, opened_at, current_tier FROM care_alerts "
        "WHERE person_id = $1 AND status IN ('open', 'escalating', 'acknowledged') ORDER BY opened_at DESC",
        person_id)
    last_checkin = await conn.fetchrow(
        "SELECT id, scheduled_at, status, question_kind, answered_at, answer_summary "
        "FROM care_checkins WHERE person_id = $1 ORDER BY scheduled_at DESC LIMIT 1", person_id)
    return {
        "person": _person(person),
        "devices": _ds(devices),
        "latest_readings": await latest_vitals(conn, person_id),
        "open_alerts": _ds(open_alerts),
        "last_checkin": _d(last_checkin),
    }
