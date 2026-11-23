"""Scheduled care visits (109).

A visit is a promise with a window: a carer will be with the person between
`planned_start` and `planned_end`. What the system does with it:

    the watch         shows "Sravani is coming at 10:00" and, once the carer
                      taps "I've arrived", "Sravani is here"; IRA does not ask
                      scheduled check-ins while a carer is in the room
    late              no arrival `visit_late_minutes` after the window opened:
                      the visit is marked late, a NOTICE alert tells the
                      coordinator, and IRA asks the person an extra check-in
    missed            no arrival by the window's end: an URGENT alert that
                      escalates through the care plan like any other, and
                      another check-in
    departure         the carer records the outcome, notes, which tasks were
                      done and a handover for the next carer; that is the
                      record of care given and the rollback refuses to drop it

Nothing here dials anyone. All timing is decided by the server on its own
clock on every monitoring pass (care/escalation.py).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import monitoring

logger = logging.getLogger("ira.care.visits")

VISIT_STATUSES = ("planned", "late", "arrived", "departed", "missed", "cancelled")
VISIT_OUTCOMES = ("all_well", "concern_noted", "needed_help", "not_at_home", "declined", "other")
OPEN_STATUSES = ("planned", "late", "arrived")

#: How long after a late or missed visit's extra check-in IRA waits before the
#: ordinary schedule resumes. The check-in itself follows the normal missed path.
EXTRA_CHECKIN_KIND = "wellbeing"
EXTRA_CHECKIN_TEXT = "Hello {name}. Your carer has not arrived yet. How are you doing?"


@dataclass
class VisitOutcome:
    late: int = 0
    missed: int = 0
    checkins_asked: int = 0


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def _public(row: Optional[dict], tasks: Optional[list] = None) -> Optional[dict]:
    if row is None:
        return None
    out = {k: (v.isoformat() if isinstance(v, dt.datetime) else str(v) if isinstance(v, uuid.UUID) else v)
           for k, v in dict(row).items() if k not in ("tenant_id", "workspace_id")}
    if tasks is not None:
        out["tasks"] = [_public(t) for t in tasks]
    return out


# ── Pure decisions (tested without a database) ───────────────────────────────

def decide(visit: dict, now: dt.datetime, late_minutes: int) -> Optional[str]:
    """What a monitoring pass should do with one open visit: None, 'late' or 'missed'."""
    status = visit["status"]
    if status not in ("planned", "late"):
        return None
    if now >= visit["planned_end"]:
        return "missed"
    if status == "planned" and now >= visit["planned_start"] + dt.timedelta(minutes=late_minutes):
        return "late"
    return None


def watch_state(visits: list[dict], now: dt.datetime, *, soon: dt.timedelta = dt.timedelta(hours=12)) -> dict:
    """What the wrist shows about visits: the carer in the room, else the next
    one expected within `soon`, else nothing. Names are first names the circle
    chose to show; no clinical words."""
    here = [v for v in visits if v["status"] == "arrived"]
    if here:
        v = max(here, key=lambda x: x["arrived_at"] or x["planned_start"])
        return {"state": "here", "carer_name": v["carer_name"] or "Your carer", "visit_id": str(v["id"]),
                "planned_start": v["planned_start"].isoformat(), "since": (v["arrived_at"] or now).isoformat()}
    coming = [v for v in visits if v["status"] in ("planned", "late") and v["planned_end"] > now
              and v["planned_start"] <= now + soon]
    if coming:
        v = min(coming, key=lambda x: x["planned_start"])
        return {"state": "late" if v["status"] == "late" else "expected", "carer_name": v["carer_name"] or "Your carer",
                "visit_id": str(v["id"]), "planned_start": v["planned_start"].isoformat(), "since": None}
    return {"state": "none", "carer_name": None, "visit_id": None, "planned_start": None, "since": None}


def carer_in_the_room(visits: list[dict]) -> bool:
    return any(v["status"] == "arrived" for v in visits)


# ── Writes by the circle ─────────────────────────────────────────────────────

async def create(principal: Principal, person_id: uuid.UUID, body: dict) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        carer_name = body.get("carer_name") or ""
        member_id = body.get("carer_member_id")
        if member_id:
            name = await conn.fetchval("SELECT display_name FROM care_circle_members WHERE id = $1 AND person_id = $2",
                                       member_id, person_id)
            if name is None:
                return None
            carer_name = carer_name or name
        row = await conn.fetchrow(
            "INSERT INTO care_visits (person_id, carer_member_id, carer_name, planned_start, planned_end, purpose, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            person_id, member_id, carer_name[:80], body["planned_start"], body["planned_end"],
            (body.get("purpose") or "")[:200], principal.user_id)
        tasks = []
        for i, title in enumerate(body.get("tasks") or []):
            tasks.append(await conn.fetchrow(
                "INSERT INTO care_visit_tasks (visit_id, title, position) VALUES ($1, $2, $3) RETURNING *",
                row["id"], str(title)[:120], i))
    return _public(dict(row), [dict(t) for t in tasks])


async def _tasks(conn, visit_id) -> list[dict]:
    return [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_visit_tasks WHERE visit_id = $1 ORDER BY position, title", visit_id)]


async def get(principal: Principal, visit_id: uuid.UUID) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT * FROM care_visits WHERE id = $1", visit_id)
        if row is None:
            return None
        return _public(dict(row), await _tasks(conn, visit_id))


async def list_for_person(principal: Principal, person_id: uuid.UUID, *, since: Optional[dt.datetime] = None,
                          until: Optional[dt.datetime] = None, limit: int = 100) -> list[dict]:
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT * FROM care_visits WHERE person_id = $1 AND ($2::timestamptz IS NULL OR planned_end >= $2) "
            "AND ($3::timestamptz IS NULL OR planned_start <= $3) ORDER BY planned_start DESC LIMIT $4",
            person_id, since, until, limit)
        out = []
        for r in rows:
            out.append(_public(dict(r), await _tasks(conn, r["id"])))
        return out


async def update(principal: Principal, visit_id: uuid.UUID, changes: dict) -> Optional[dict]:
    """Change a planned visit's window, purpose or carer. Arrived or finished
    visits are the record of what happened and do not move."""
    allowed = {"planned_start", "planned_end", "purpose", "carer_name", "carer_member_id"}
    fields = {k: v for k, v in changes.items() if k in allowed and v is not None}
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT * FROM care_visits WHERE id = $1", visit_id)
        if row is None:
            return None
        if row["status"] not in ("planned", "late"):
            return _public(dict(row), await _tasks(conn, visit_id))
        if fields:
            sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
            # A moved window starts the lateness clock again.
            row = await conn.fetchrow(
                f"UPDATE care_visits SET {sets}, status = 'planned', late_alerted_at = NULL, updated_at = NOW() "
                f"WHERE id = $1 RETURNING *", visit_id, *fields.values())
        return _public(dict(row), await _tasks(conn, visit_id))


async def cancel(principal: Principal, visit_id: uuid.UUID, note: str = "") -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_visits SET status = 'cancelled', notes = CASE WHEN $2 = '' THEN notes ELSE $2 END, "
            "updated_at = NOW() WHERE id = $1 AND status IN ('planned', 'late', 'missed') RETURNING *",
            visit_id, note[:4000])
        if row is None:
            row = await conn.fetchrow("SELECT * FROM care_visits WHERE id = $1", visit_id)
        return _public(dict(row), await _tasks(conn, visit_id)) if row else None


async def arrive(principal: Principal, visit_id: uuid.UUID, *, now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The carer is with the person. Idempotent. A late or missed visit that is
    then attended becomes 'arrived' and its open visit alert is closed with the
    arrival as the outcome."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT * FROM care_visits WHERE id = $1", visit_id)
        if row is None:
            return None
        if row["status"] in ("planned", "late", "missed"):
            row = await conn.fetchrow(
                "UPDATE care_visits SET status = 'arrived', arrived_at = $2, updated_at = NOW() WHERE id = $1 RETURNING *",
                visit_id, now)
            await conn.execute(
                "UPDATE care_alerts SET status = 'resolved', resolved_at = $2, outcome = 'all_well', "
                "resolution = 'The carer arrived.', next_action_at = NULL, follow_up_due_at = NULL "
                "WHERE kind IN ('visit_late', 'visit_missed') AND status <> 'resolved' "
                "AND details->>'visit_id' = $1", str(visit_id), now)
        return _public(dict(row), await _tasks(conn, visit_id))


async def depart(principal: Principal, visit_id: uuid.UUID, body: dict, *,
                 now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The carer leaves: outcome, notes, handover, and which tasks were done."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT * FROM care_visits WHERE id = $1", visit_id)
        if row is None:
            return None
        if row["status"] == "arrived":
            row = await conn.fetchrow(
                "UPDATE care_visits SET status = 'departed', departed_at = $2, outcome = $3, notes = $4, handover = $5, "
                "updated_at = NOW() WHERE id = $1 RETURNING *",
                visit_id, now, body["outcome"], (body.get("notes") or "")[:4000], (body.get("handover") or "")[:4000])
            for task_id in body.get("tasks_done") or []:
                await conn.execute(
                    "UPDATE care_visit_tasks SET done = TRUE, done_at = $3 WHERE id = $1 AND visit_id = $2",
                    uuid.UUID(str(task_id)), visit_id, now)
        return _public(dict(row), await _tasks(conn, visit_id))


async def add_task(principal: Principal, visit_id: uuid.UUID, title: str) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT status FROM care_visits WHERE id = $1", visit_id)
        if row is None or row["status"] in ("departed", "cancelled"):
            return None
        position = await conn.fetchval("SELECT coalesce(max(position), -1) + 1 FROM care_visit_tasks WHERE visit_id = $1", visit_id)
        t = await conn.fetchrow(
            "INSERT INTO care_visit_tasks (visit_id, title, position) VALUES ($1, $2, $3) RETURNING *",
            visit_id, title[:120], position)
        return _public(dict(t))


async def set_task(principal: Principal, visit_id: uuid.UUID, task_id: uuid.UUID, *, done: bool,
                   note: str = "", now: Optional[dt.datetime] = None) -> Optional[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    async with workspace_connection(principal) as conn:
        t = await conn.fetchrow(
            "UPDATE care_visit_tasks SET done = $3, done_at = CASE WHEN $3 THEN $4 ELSE NULL END, "
            "note = CASE WHEN $5 = '' THEN note ELSE $5 END WHERE id = $1 AND visit_id = $2 RETURNING *",
            task_id, visit_id, done, now, note[:400])
        return _public(dict(t)) if t else None


# ── The watch and the monitoring pass ────────────────────────────────────────

async def acknowledge_from_device(principal: Principal, device: dict, visit_id: uuid.UUID,
                                  now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The person saw "[Name] is here" on the wrist. Recorded once; the first time stands."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_visits SET acknowledged_at = COALESCE(acknowledged_at, $3), updated_at = NOW() "
            "WHERE id = $1 AND person_id = $2 AND status IN ('arrived', 'departed') RETURNING *",
            visit_id, device["person_id"], now)
        return _public(dict(row)) if row else None


async def open_visits(conn, person_id, now: dt.datetime) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM care_visits WHERE person_id = $1 AND status IN ('planned', 'late', 'arrived') "
        "AND planned_end > $2::timestamptz - interval '12 hours' ORDER BY planned_start", person_id, now)
    return [dict(r) for r in rows]


async def state_for_person(conn, person_id, now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    return watch_state(await open_visits(conn, person_id, now), now)


async def _extra_checkin(conn, person: dict, now: dt.datetime) -> None:
    name = person.get("preferred_name") or person["display_name"]
    await conn.execute(
        "UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", person["id"])
    await conn.execute(
        "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind, question_text) "
        "VALUES ($1, $2, 'scheduled', $3, $4)", person["id"], now, EXTRA_CHECKIN_KIND,
        EXTRA_CHECKIN_TEXT.format(name=name))


async def run(conn, principal: Principal, now: dt.datetime) -> VisitOutcome:
    """Mark late and missed visits, open their alerts, ask the extra check-in.
    Idempotent per visit: each transition happens once."""
    out = VisitOutcome()
    rows = await conn.fetch(
        "SELECT v.*, s.visit_late_minutes FROM care_visits v "
        "LEFT JOIN care_safety_settings s ON s.person_id = v.person_id "
        "WHERE v.status IN ('planned', 'late') AND v.planned_start <= $1 ORDER BY v.planned_start", now)
    for row in rows:
        visit = dict(row)
        action = decide(visit, now, int(visit["visit_late_minutes"] or 15))
        if action is None:
            continue
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", visit["person_id"])
        if person is None:
            continue
        person = dict(person)
        name = person.get("preferred_name") or person["display_name"]
        who = visit["carer_name"] or "The carer"
        start = visit["planned_start"].astimezone(dt.timezone.utc).strftime("%H:%M")
        if action == "late":
            summary = (f"{who}'s visit to {name} planned for {start} has not started "
                       f"{int(visit['visit_late_minutes'] or 15)} minutes after its start.")
            await conn.execute(
                "UPDATE care_visits SET status = 'late', late_alerted_at = $2, updated_at = NOW() WHERE id = $1 AND status = 'planned'",
                visit["id"], now)
            await _alert(conn, visit, "visit_late", monitoring.NOTICE, summary, now)
            out.late += 1
        else:
            summary = (f"{who}'s visit to {name} planned for {start} did not happen: nobody recorded an arrival "
                       f"by the end of its window.")
            await conn.execute(
                "UPDATE care_visits SET status = 'missed', missed_alerted_at = $2, updated_at = NOW() "
                "WHERE id = $1 AND status IN ('planned', 'late')", visit["id"], now)
            await conn.execute(
                "UPDATE care_alerts SET status = 'resolved', resolved_at = $2, outcome = 'other', "
                "resolution = 'The visit was then missed; see the visit_missed alert.' "
                "WHERE kind = 'visit_late' AND status <> 'resolved' AND details->>'visit_id' = $1", str(visit["id"]), now)
            await _alert(conn, visit, "visit_missed", monitoring.URGENT, summary, now)
            out.missed += 1
        await _extra_checkin(conn, person, now)
        out.checkins_asked += 1
        logger.info("care visit %s %s for person %s", visit["id"], action, visit["person_id"])
    return out


async def _alert(conn, visit: dict, kind: str, severity: str, summary: str, now: dt.datetime) -> None:
    details = {"summary": summary, "visit_id": str(visit["id"]), "carer_name": visit["carer_name"],
               "planned_start": visit["planned_start"].isoformat(), "planned_end": visit["planned_end"].isoformat()}
    await conn.execute(
        "INSERT INTO care_alerts (person_id, kind, severity, status, opened_at, current_tier, next_action_at, details) "
        "VALUES ($1, $2, $3, 'open', $4, 0, $4, $5::jsonb)",
        visit["person_id"], kind, severity, now, json.dumps(details))


__all__ = ["VISIT_STATUSES", "VISIT_OUTCOMES", "VisitOutcome", "decide", "watch_state", "carer_in_the_room",
           "create", "get", "list_for_person", "acknowledge_from_device", "update", "cancel", "arrive", "depart", "add_task", "set_task",
           "open_visits", "state_for_person", "run"]
