"""Who may do what in a care workspace.

Three levels, read from the principal's VERIFIED workspace role (never from a
header), plus the deployment owner in the private edition:

    read    viewer, member, workspace_admin, owner   see status, alerts, summaries
    act     member, workspace_admin, owner           acknowledge and resolve alerts,
                                                     confirm medication, record answers
    manage  workspace_admin, owner                   people, circle, devices, safety
                                                     settings, routines, medication
                                                     lists, consents, access log

An absent role denies. This is a check ON TOP of row-level security: the
database already refuses rows outside the caller's workspace, so what these
functions add is a legible 403 before any query runs, and the distinction
between looking and changing that RLS cannot express.
"""
from __future__ import annotations

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
