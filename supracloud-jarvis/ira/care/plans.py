"""The care plan: what a person's care is for and how it is given (migration 116).

A plan is versioned and never rewritten: saving a change inserts the next version with the
reason for it, so the history of the plan is the table and an earlier version can always be
shown. Only a care manager writes a plan (authz.ACTIONS "edit_care_plan"); anyone who may
see the person may read it, and each read is written to the access log. No plan text goes
to the application log or the access log: only which version was read or written.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import asyncpg

from core.identity.principal import Principal

from . import audit

_COLUMNS = ("id, person_id, version, goals, support, routine, risks, emergency, review_on, reason, "
            "author_user_id, author_name, created_at")


class PlanConflict(Exception):
    """Someone else saved a version first: the caller's change was based on an older plan."""


def _row(r) -> dict:
    d = dict(r)
    if isinstance(d.get("routine"), str):
        d["routine"] = json.loads(d["routine"])
    return d


async def current(conn, principal: Principal, person_id: uuid.UUID) -> Optional[dict]:
    r = await conn.fetchrow(f"SELECT {_COLUMNS} FROM care_plans WHERE person_id = $1 "  # noqa: S608 - fixed columns
                            "ORDER BY version DESC LIMIT 1", person_id)
    await audit.record_access(conn, principal, person_id=person_id, resource_kind="care_plan",
                              resource_id=r["id"] if r else None, action="view",
                              detail={"version": r["version"] if r else None})
    return _row(r) if r else None


async def history(conn, principal: Principal, person_id: uuid.UUID, limit: int = 50) -> list[dict]:
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM care_plans WHERE person_id = $1 "  # noqa: S608 - fixed columns
                            "ORDER BY version DESC LIMIT $2", person_id, limit)
    await audit.record_access(conn, principal, person_id=person_id, resource_kind="care_plan", action="view",
                              detail={"history": True, "versions": len(rows)})
    return [_row(r) for r in rows]


async def save(conn, principal: Principal, person_id: uuid.UUID, body: dict, author_name: str) -> dict:
    """Insert the next version. `based_on` is the version the editor started from (0 for the
    first plan); if someone saved in between, the change is refused rather than overwriting theirs."""
    latest = await conn.fetchval("SELECT coalesce(max(version), 0) FROM care_plans WHERE person_id = $1", person_id)
    if body["based_on"] != latest:
        raise PlanConflict(latest)
    try:
        r = await conn.fetchrow(
            f"""
            INSERT INTO care_plans (person_id, version, goals, support, routine, risks, emergency, review_on,
                                    reason, author_user_id, author_name)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
            RETURNING {_COLUMNS}
            """,  # noqa: S608 - fixed columns
            person_id, latest + 1, body["goals"], body["support"], json.dumps(body["routine"]), body["risks"],
            body["emergency"], body["review_on"], body["reason"], principal.user_id, author_name[:80])
    except asyncpg.UniqueViolationError as e:
        raise PlanConflict(latest) from e
    await audit.record_access(conn, principal, person_id=person_id, resource_kind="care_plan", resource_id=r["id"],
                              action="change", detail={"version": r["version"]})
    return _row(r)
