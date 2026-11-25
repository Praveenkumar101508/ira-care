"""Who may do what in a care workspace.

Three levels, read from the principal's VERIFIED workspace role (never from a
header), plus the deployment owner in the private edition:

    read    viewer, member, workspace_admin, owner   see status, alerts, summaries
    act     member, workspace_admin, owner           acknowledge and resolve alerts,
                                                     confirm medication, record answers
    manage  workspace_admin, owner                   people, circle, devices, safety
                                                     settings, routines, medication
                                                     lists, consents, access log

Who: the workspace role says what a caller may do; WHICH PEOPLE they may do it for is a
second rule (D-076). A workspace administrator (the care manager) and the deployment
owner see every person in the workspace. Everyone else, family, carers and clinicians
invited into one person's circle, sees only the people whose circle they are an active
member of: one care organisation's workspace holds many people, and being invited for
your mother must not show you anybody else. Refused as "not found", so it does not even
reveal that the person exists.

An absent role denies. This is a check ON TOP of row-level security: the
database already refuses rows outside the caller's workspace, so what these
functions add is a legible 403 before any query runs, and the distinction
between looking and changing that RLS cannot express.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException

from core.identity.principal import Principal

READ_ROLES = frozenset({"viewer", "member", "workspace_admin"})
ACT_ROLES = frozenset({"member", "workspace_admin"})
MANAGE_ROLES = frozenset({"workspace_admin"})

_DENIED = "You do not have permission to do that in this care circle."


def can_read(principal: Principal) -> bool:
    return principal.is_owner or (principal.workspace_role in READ_ROLES)


def can_act(principal: Principal) -> bool:
    return principal.is_owner or (principal.workspace_role in ACT_ROLES)


def can_manage(principal: Principal) -> bool:
    return principal.is_owner or (principal.workspace_role in MANAGE_ROLES)


def _refuse() -> HTTPException:
    return HTTPException(status_code=403, detail=_DENIED)


def ensure_can_read(principal: Principal) -> None:
    if not can_read(principal):
        raise _refuse()


def ensure_can_act(principal: Principal) -> None:
    if not can_act(principal):
        raise _refuse()


def ensure_can_manage(principal: Principal) -> None:
    if not can_manage(principal):
        raise _refuse()


async def visible_people(conn, principal: Principal) -> Optional[set]:
    """The people this caller may see: None means every person in the workspace (a manager);
    otherwise the ids of the circles they are an active member of."""
    if can_manage(principal):
        return None
    rows = await conn.fetch(
        "SELECT DISTINCT person_id FROM care_circle_members WHERE user_id = $1 AND active", principal.user_id)
    return {r["person_id"] for r in rows}


async def may_see_person(conn, principal: Principal, person_id) -> bool:
    if can_manage(principal):
        return True
    if principal.user_id is None:
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM care_circle_members WHERE person_id = $1 AND user_id = $2 AND active LIMIT 1",
        uuid.UUID(str(person_id)), principal.user_id))
