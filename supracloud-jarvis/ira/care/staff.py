"""The care team's working views (D-080): caseloads, the visit plan, and coordinators.

* The caseload: which people each carer covers, and each one's next visit. A care manager
  and a care coordinator see every carer's; a carer or a viewer sees only their own.
* The visit plan: every visit in a window, across the people the caller may see
  (authz.visible_people: everyone for a manager or a coordinator, otherwise their circles).
* Coordinators: a care manager names a carer coordinator, or stops. Only a `member` of the
  workspace is ever treated as one (authz.is_coordinator); naming anyone else changes nothing.

Every query runs on a workspace-bound connection, so row-level security keeps each
organisation's rows to itself. Nothing here returns what a carer or the person said.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from core.identity.principal import Principal

from . import authz


def _iso(v):
    return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else str(v) if isinstance(v, uuid.UUID) else v


async def caseload(conn, principal: Principal, *, now: Optional[dt.datetime] = None) -> dict:
    """One entry per carer with an account: the people they are a carer for, and the next
    planned visit of theirs to each."""
    now = now or dt.datetime.now(dt.timezone.utc)
    everyone = await authz.sees_everyone(conn, principal)
    rows = await conn.fetch(
        """
        SELECT m.id AS member_id, m.user_id, m.display_name AS carer_name, m.person_id,
               coalesce(nullif(pp.preferred_name, ''), pp.display_name) AS person_name,
               (SELECT min(v.planned_start) FROM care_visits v
                 WHERE v.carer_member_id = m.id AND v.status IN ('planned', 'late') AND v.planned_end >= $1) AS next_visit
          FROM care_circle_members m
          JOIN care_people pp ON pp.id = m.person_id
         WHERE m.role = 'carer' AND m.active AND pp.status = 'active' AND m.user_id IS NOT NULL
           AND ($2::boolean OR m.user_id = $3)
         ORDER BY m.display_name, person_name
        """, now, everyone, principal.user_id)
    carers: dict = {}
    for r in rows:
        c = carers.setdefault(r["user_id"], {"user_id": str(r["user_id"]), "name": r["carer_name"], "people": []})
        c["people"].append({"person_id": str(r["person_id"]), "name": r["person_name"], "member_id": str(r["member_id"]),
                            "next_visit": _iso(r["next_visit"]) if r["next_visit"] else None})
    return {"everyone": everyone, "carers": list(carers.values())}


async def visit_plan(conn, principal: Principal, *, since: dt.datetime, until: dt.datetime, limit: int) -> dict:
    """Visits whose window overlaps [since, until], soonest first, for the people the caller may see."""
    visible = await authz.visible_people(conn, principal)
    rows = await conn.fetch(
        """
        SELECT v.id, v.person_id, v.planned_start, v.planned_end, v.status, v.purpose, v.carer_name,
               v.carer_member_id, coalesce(nullif(pp.preferred_name, ''), pp.display_name) AS person_name
          FROM care_visits v JOIN care_people pp ON pp.id = v.person_id
         WHERE v.planned_end >= $1 AND v.planned_start <= $2
           AND ($3::uuid[] IS NULL OR v.person_id = ANY($3::uuid[]))
         ORDER BY v.planned_start, person_name
         LIMIT $4
        """, since, until, None if visible is None else list(visible), limit)
    return {"visits": [{k: _iso(v) for k, v in dict(r).items()} for r in rows], "since": since.isoformat(),
            "until": until.isoformat()}


async def staff(conn) -> dict:
    """Everyone with an account in a circle of this workspace, with their circle roles and
    whether they are a coordinator. For a care manager (the route checks)."""
    rows = await conn.fetch(
        """
        SELECT m.user_id, min(m.display_name) AS name, array_agg(DISTINCT m.role ORDER BY m.role) AS roles,
               count(DISTINCT m.person_id) AS people,
               EXISTS (SELECT 1 FROM care_staff_designations d
                        WHERE d.user_id = m.user_id AND d.designation = 'coordinator') AS coordinator
          FROM care_circle_members m
         WHERE m.active AND m.user_id IS NOT NULL
         GROUP BY m.user_id
         ORDER BY name
        """)
    return {"staff": [{"user_id": str(r["user_id"]), "name": r["name"], "roles": list(r["roles"]),
                       "people": int(r["people"]), "coordinator": bool(r["coordinator"])} for r in rows]}


async def set_coordinator(conn, principal: Principal, user_id: uuid.UUID, on: bool) -> Optional[dict]:
    """Name an account coordinator, or stop. None when the account is in no circle of this
    workspace: a coordinator is somebody already on the care team."""
    known = await conn.fetchval(
        "SELECT 1 FROM care_circle_members WHERE user_id = $1 AND active LIMIT 1", user_id)
    if not known:
        return None
    if on:
        await conn.execute(
            "INSERT INTO care_staff_designations (user_id, designation, granted_by) VALUES ($1, 'coordinator', $2) "
            "ON CONFLICT ON CONSTRAINT care_staff_designations_once DO NOTHING", user_id, principal.user_id)
    else:
        await conn.execute(
            "DELETE FROM care_staff_designations WHERE user_id = $1 AND designation = 'coordinator'", user_id)
    return {"user_id": str(user_id), "coordinator": bool(on)}
