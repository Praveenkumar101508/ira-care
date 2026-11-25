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


async def is_coordinator(conn, principal: Principal) -> bool:
    """A care coordinator (D-080): a `member` of the workspace whom a care manager has named
    coordinator. The designation does nothing for any other role: a viewer stays a viewer."""
    if principal.workspace_role != "member" or principal.user_id is None:
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM care_staff_designations WHERE user_id = $1 AND designation = 'coordinator' LIMIT 1",
        principal.user_id))


async def sees_everyone(conn, principal: Principal) -> bool:
    """A care manager, the deployment owner and a care coordinator see every person in the
    workspace; everyone else only the people whose circle they are in (D-076)."""
    return can_manage(principal) or await is_coordinator(conn, principal)


async def visible_people(conn, principal: Principal) -> Optional[set]:
    """The people this caller may see: None means every person in the workspace (a manager
    or a coordinator); otherwise the ids of the circles they are an active member of."""
    if await sees_everyone(conn, principal):
        return None
    rows = await conn.fetch(
        "SELECT DISTINCT person_id FROM care_circle_members WHERE user_id = $1 AND active", principal.user_id)
    return {r["person_id"] for r in rows}


async def may_see_person(conn, principal: Principal, person_id) -> bool:
    if await sees_everyone(conn, principal):
        return True
    if principal.user_id is None:
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM care_circle_members WHERE person_id = $1 AND user_id = $2 AND active LIMIT 1",
        uuid.UUID(str(person_id)), principal.user_id))


# ── Staff roles: what a care organisation's people can do (D-078) ────────────
#
# The three workspace roles, named as a care organisation names its staff. This is a
# description of the rules above, not a second set of rules: every action below names the
# routes that enforce it and the level they check, and tests/care/test_care_staff_roles.py
# fails if a route's check and this table ever disagree. Nothing here grants anything.

STAFF_ROLES: dict[str, dict] = {
    "workspace_admin": {
        "label": "Care manager",
        "who": "Runs the service: sees everyone cared for in the organisation, writes care plans, "
               "adds people, carers and watches, and changes safety settings.",
    },
    "member": {
        "label": "Carer",
        "who": "Gives care: sees only the people whose circle they are in, answers alerts, records "
               "visits and medication, and reads the care plan.",
    },
    "coordinator": {
        "label": "Care coordinator",
        "who": "A carer the care manager has named coordinator: sees everyone cared for and every carer's "
               "caseload, and plans visits for all of them. Cannot add people, carers or watches, change "
               "safety settings or write care plans.",
    },
    "viewer": {
        "label": "Viewer",
        "who": "Can look but not change anything, for the people whose circle they are in.",
    },
}

#: action -> (level, the routes that enforce it). Levels: read, act, manage.
ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "view_care_plan": ("read", ("GET /care/people/{person_id}/plan", "GET /care/people/{person_id}/plan/history")),
    "edit_care_plan": ("manage", ("POST /care/people/{person_id}/plan",)),
    "acknowledge_alerts": ("act", ("POST /care/alerts/{alert_id}/acknowledge", "POST /care/alerts/{alert_id}/resolve")),
    "record_visits": ("act", ("POST /care/people/{person_id}/visits", "POST /care/visits/{visit_id}/arrive",
                              "POST /care/visits/{visit_id}/depart")),
    "record_medication": ("act", ("POST /care/people/{person_id}/medication-events",)),
    "set_reminders": ("act", ("POST /care/people/{person_id}/reminders",)),
    "manage_people": ("manage", ("POST /care/people", "PATCH /care/people/{person_id}", "DELETE /care/people/{person_id}")),
    "manage_circle": ("manage", ("POST /care/people/{person_id}/circle", "PATCH /care/circle/{member_id}",
                                 "DELETE /care/circle/{member_id}", "POST /care/people/{person_id}/invitations")),
    "manage_watches": ("manage", ("POST /care/people/{person_id}/devices", "POST /care/devices/{device_id}/revoke")),
    "manage_safety_settings": ("manage", ("PUT /care/people/{person_id}/settings",)),
    "manage_medication_list": ("manage", ("POST /care/people/{person_id}/medications",)),
    "manage_consents": ("manage", ("POST /care/people/{person_id}/consents",)),
    "search": ("read", ("GET /care/search",)),
    "view_caseload": ("read", ("GET /care/caseload",)),
    "view_visit_plan": ("read", ("GET /care/visits",)),
    "designate_coordinators": ("manage", ("GET /care/staff", "PUT /care/staff/{user_id}/coordinator")),
}

_LEVEL = {"read": can_read, "act": can_act, "manage": can_manage}


def permissions_for(principal: Principal, *, coordinator: bool = False) -> dict:
    """What this caller may do, for the dashboard to show only the actions that will work.
    The server still checks every request: hiding a button is a courtesy, not the control."""
    role = "workspace_admin" if principal.is_owner else (principal.workspace_role or "")
    # The designation counts only on a `member`, exactly as is_coordinator() decides it.
    coordinator = bool(coordinator) and role == "member"
    staff = STAFF_ROLES.get("coordinator" if coordinator else role)
    return {
        "role": role or None,
        "coordinator": coordinator,
        "label": staff["label"] if staff else "No access",
        "who": staff["who"] if staff else "This account has no role in this workspace.",
        "sees_everyone": can_manage(principal) or coordinator,
        "can_read": can_read(principal), "can_act": can_act(principal), "can_manage": can_manage(principal),
        "actions": {name: _LEVEL[level](principal) for name, (level, _) in ACTIONS.items()},
    }
