"""Verified workspace selection.

A workspace identifier arriving from a client is a **request to select a
workspace**, never an assertion of access to one. This module is the only place
that turns the former into the latter, and it does so by asking the database
seven questions rather than by trusting the caller.

    verified authenticated user
  + verified tenant membership
  + workspace belongs to that tenant
  + verified workspace membership or grant
  + both are active
  = an active workspace

Everything else is refused. In particular this module will never:

* read a workspace from a request body, query string, path segment,
  `X-Workspace-ID` header, cookie, frontend state, model output, memory content
  or tool output as anything other than a *requested* id to be verified;
* pick the first workspace it finds;
* fall back to the default workspace when none was requested, unless the caller
  explicitly asked for that behaviour AND the user is a verified member of it;
* infer workspace authority from a username.

── Why the checks are separate rather than one join ─────────────────────────

One `WHERE` clause spanning memberships, workspaces and tenants would return
zero rows for eight different reasons and report the same thing for all of
them. "You are not a member" and "that workspace belongs to another tenant" are
different facts: the first is routine, the second is a cross-tenant access
attempt and should be visible as one. So the checks are ordered and each failure
names itself internally — while the message returned to the caller stays
deliberately uniform, because a caller who is not a member must not be able to
tell "no such workspace" from "exists, but not yours". That distinction is a
tenant-enumeration oracle.

── Why this runs on an owner/tenant-bound connection ────────────────────────

The membership tables are tenant-scoped with forced RLS, so the lookup runs
inside the tenant the user is already verified against — never unscoped, and
never with the workspace GUC set, because that is what this function is trying
to decide.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from core.identity.principal import Principal

logger = logging.getLogger("ira.identity.workspace")

#: Marker for code that still needs a workspace-scoped rewrite in Batch 5C.
#: Greppable on purpose — `git grep WORKSPACE_COMPATIBILITY_REQUIRED` is the
#: Batch 5C work list.
WORKSPACE_COMPATIBILITY_REQUIRED = "WORKSPACE_COMPATIBILITY_REQUIRED"

#: Membership statuses that carry access. Anything else — `suspended`,
#: `removed`, `revoked`, `invited` — does not. Listed as an allow-list rather
#: than a deny-list so a status added later denies until somebody decides it
#: should not.
ACTIVE_MEMBERSHIP_STATUSES = frozenset({"active"})

#: Workspace lifecycle states that can be selected. `archived` and `deleted`
#: cannot; `provisioning` cannot, because content written into a workspace that
#: is not finished being built has nowhere guaranteed to live.
SELECTABLE_WORKSPACE_STATUSES = frozenset({"active"})


class WorkspaceSelectionError(PermissionError):
    """A workspace could not be selected.

    A `PermissionError` rather than a bespoke base, so a route that forgets to
    handle it produces a 403-shaped failure rather than a 500 that leaks a
    stack trace naming tables.

    The public `message` is uniform across every failure reason. `reason` is the
    machine-readable cause for logs and tests, and must not be returned to an
    untrusted caller.
    """

    #: Uniform text. Distinguishing "not found" from "not yours" tells an
    #: attacker which workspace ids exist in other tenants.
    PUBLIC_MESSAGE = "No accessible workspace matches that identifier."

    def __init__(self, reason: str, *, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(self.PUBLIC_MESSAGE)


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """The result of a successful verification. Immutable.

    Immutable because a request's workspace must not change part-way through
    handling it: a check performed against workspace A followed by a write into
    workspace B is an authorisation bypass that reads like ordinary code.
    """

    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    workspace_membership_id: uuid.UUID
    membership_id: uuid.UUID
    workspace_role: str
    is_default: bool

    def apply_to(self, principal: Principal) -> Principal:
        """Return `principal` carrying this verified workspace.

        Refuses if the principal is not the user this context was verified for,
        or is in a different tenant. Without that check a context verified for
        one user could be attached to another — the whole verification would
        then prove nothing about the principal actually making the request.
        """
        from dataclasses import replace

        if principal.user_id != self.user_id:
            raise WorkspaceSelectionError(
                "context_user_mismatch",
                detail=f"context verified for {self.user_id}, applied to {principal.user_id}",
            )
        if principal.tenant_id != self.tenant_id:
            raise WorkspaceSelectionError(
                "context_tenant_mismatch",
                detail=f"context tenant {self.tenant_id}, principal tenant {principal.tenant_id}",
            )
        return replace(
            principal,
            workspace_id=self.workspace_id,
            membership_id=self.membership_id,
            workspace_membership_id=self.workspace_membership_id,
            workspace_role=self.workspace_role,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────


async def workspace_capabilities(conn, role: str) -> frozenset[str]:
    """The capabilities `role` carries, read from `workspace_role_capabilities`.

    Read from the database rather than hardcoded here, so the table `040`
    created is the single definition and a Python constant cannot drift from the
    reference data the policies will eventually use.
    """
    rows = await conn.fetch(
        "SELECT capability FROM workspace_role_capabilities WHERE role = $1", role
    )
    return frozenset(r["capability"] for r in rows)


async def require_capability(conn, principal: Principal, capability: str) -> None:
    """Refuse unless the principal's WORKSPACE role carries `capability`.

    Three properties worth stating, because each is a way this is commonly got
    wrong:

    * The tenant role is not consulted. A tenant owner is not implicitly a
      workspace administrator — if that is wanted it must be an explicit grant
      recorded as a workspace membership, not an inference. (CLOUD-ADR-0016.)
    * An absent workspace role denies. There is no "assume member".
    * This is a server-side check on top of RLS, not instead of it. Batch 5C
      adds the database boundary; a capability check that is the only boundary
      is one forgotten decorator away from being no boundary.
    """
    if not principal.has_workspace:
        raise WorkspaceSelectionError(
            "capability_without_workspace",
            detail=f"{capability} requires an active workspace",
        )
    role = principal.workspace_role
    if not role:
        raise WorkspaceSelectionError(
            "capability_without_role", detail=f"{capability} requested with no workspace role"
        )
    if capability not in await workspace_capabilities(conn, role):
        raise WorkspaceSelectionError(
            "capability_denied",
            detail=f"workspace role {role!r} does not carry {capability!r}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Selection
# ─────────────────────────────────────────────────────────────────────────────


def _coerce_workspace_id(requested: object) -> uuid.UUID:
    """A requested identifier, or a selection error. Never an exception a route
    would render as a 500.

    Anything that is not a UUID is refused with the same uniform message as a
    workspace that exists in another tenant, so a malformed id and a forbidden
    one are indistinguishable from outside.
    """
    if isinstance(requested, uuid.UUID):
        return requested
    try:
        return uuid.UUID(str(requested))
    except (ValueError, AttributeError, TypeError):
        raise WorkspaceSelectionError(
            "invalid_workspace_id", detail=f"not a UUID: {type(requested).__name__}"
        ) from None


async def select_workspace(
    conn,
    principal: Principal,
    requested_workspace_id: object,
) -> WorkspaceContext:
    """Verify that `principal` may act in `requested_workspace_id`.

    `conn` must already be bound to `principal.tenant_id` — that is, opened
    through `tenant_connection`. This function does not open its own connection
    and does not set `app.workspace_id`: it decides *whether* there is a
    workspace to set, and `workspace_connection` sets it afterwards. Keeping
    those separate means the decision cannot be made by a connection that has
    already assumed its own answer.
    """
    workspace_id = _coerce_workspace_id(requested_workspace_id)

    # 1–2. The user and the tenant come from the authenticated principal, not
    #      from anything the caller sent. A principal cannot exist without a
    #      tenant (Principal.__post_init__ refuses), so there is nothing to
    #      re-verify here beyond its presence.
    if principal is None or principal.user_id is None or principal.tenant_id is None:
        raise WorkspaceSelectionError("unauthenticated")

    # 3. Active tenant membership. `membership_grants_access` is the generated
    #    column from `040` — status and revocation resolved in the database, so
    #    Python cannot disagree with the policies about what "active" means.
    membership = await conn.fetchrow(
        """
        SELECT id, status, membership_grants_access
          FROM tenant_memberships
         WHERE tenant_id = $1 AND user_id = $2
        """,
        principal.tenant_id,
        principal.user_id,
    )
    if membership is None:
        raise WorkspaceSelectionError("no_tenant_membership")
    if not membership["membership_grants_access"]:
        raise WorkspaceSelectionError(
            "tenant_membership_inactive", detail=f"status={membership['status']}"
        )

    # 4. The workspace exists AND belongs to this tenant. Filtering by
    #    tenant_id here is what makes a cross-tenant identifier indistinguishable
    #    from a nonexistent one — and RLS on `workspaces` means a workspace in
    #    another tenant is invisible on this connection anyway. Both, because
    #    one of them being load-bearing alone is a single point of failure.
    workspace = await conn.fetchrow(
        """
        SELECT id, tenant_id, status, is_default
          FROM workspaces
         WHERE id = $1 AND tenant_id = $2
        """,
        workspace_id,
        principal.tenant_id,
    )
    if workspace is None:
        raise WorkspaceSelectionError("workspace_not_in_tenant")
    if workspace["status"] not in SELECTABLE_WORKSPACE_STATUSES:
        raise WorkspaceSelectionError(
            "workspace_not_selectable", detail=f"status={workspace['status']}"
        )

    # 5–6. Workspace membership or grant, and it must be active.
    #      `workspace_grants_access` is `040`'s generated column and already
    #      accounts for tenant-membership revocation cascading down.
    ws_member = await conn.fetchrow(
        """
        SELECT id, role, status, workspace_grants_access
          FROM workspace_memberships
         WHERE workspace_id = $1 AND user_id = $2 AND tenant_id = $3
        """,
        workspace_id,
        principal.user_id,
        principal.tenant_id,
    )
    if ws_member is None:
        raise WorkspaceSelectionError("no_workspace_membership")
    if not ws_member["workspace_grants_access"]:
        raise WorkspaceSelectionError(
            "workspace_membership_inactive", detail=f"status={ws_member['status']}"
        )

    # 7. Nothing left to reject. Build the immutable context.
    context = WorkspaceContext(
        workspace_id=workspace["id"],
        tenant_id=workspace["tenant_id"],
        user_id=principal.user_id,
        workspace_membership_id=ws_member["id"],
        membership_id=membership["id"],
        workspace_role=ws_member["role"],
        is_default=bool(workspace["is_default"]),
    )
    # Identifiers only. A workspace NAME is customer content and does not belong
    # in a routine log line.
    logger.debug(
        "workspace selected user=%s tenant=%s workspace=%s role=%s",
        principal.user_id,
        principal.tenant_id,
        context.workspace_id,
        context.workspace_role,
    )
    return context


async def select_default_workspace(
    conn, principal: Principal, *, allow_default: bool
) -> WorkspaceContext:
    """Select the tenant's default workspace — only where that is permitted.

    `allow_default` is a required keyword with no default value, so choosing
    automatic selection is something an endpoint does on purpose and a reader
    can see at the call site. An endpoint that has not thought about it cannot
    accidentally get the permissive behaviour.

    This is still a full verification: the default workspace is looked up by its
    flag, then put through `select_workspace` like any other. A user who is not
    a member of their tenant's default workspace does not get it. That is the
    difference between "the default workspace" and "the first workspace", and
    the second is what this module refuses to implement.
    """
    if not allow_default:
        raise WorkspaceSelectionError(
            "default_selection_not_permitted",
            detail="this endpoint requires an explicitly selected workspace",
        )
    row = await conn.fetchrow(
        """
        SELECT id FROM workspaces
         WHERE tenant_id = $1 AND is_default AND status = 'active'
        """,
        principal.tenant_id,
    )
    if row is None:
        raise WorkspaceSelectionError("no_default_workspace")
    return await select_workspace(conn, principal, row["id"])


async def legacy_owner_workspace(conn, principal: Principal) -> WorkspaceContext:
    """The migration workspace, for the verified legacy single owner only.

    WORKSPACE_COMPATIBILITY_REQUIRED

    IRA began as a single-owner application, and a number of subsystems —
    LifeOS, cognition, the schedulers — have no request, no session and
    therefore no selected workspace. Until Batch 5C rewrites them they need
    somewhere to write, and this is it.

    Three conditions, all verified, none inferable from a request:

    * the principal's tenant is the deterministic private tenant;
    * the principal is that tenant's owner;
    * the workspace is that tenant's default, and the principal is a verified
      member of it — this defers to `select_workspace`, so the compatibility
      path is not a hole in the verification, it is a caller of it.

    **This is not a multi-tenant capability and must not be described as one.**
    It is a labelled bridge with an expiry date, and the label is what makes the
    Batch 5C work list a grep rather than an audit.
    """
    from core.identity.principal import PRIVATE_TENANT_ID

    if principal.tenant_id != PRIVATE_TENANT_ID:
        raise WorkspaceSelectionError(
            "legacy_path_foreign_tenant",
            detail="the compatibility path serves the private tenant only",
        )
    if not principal.is_owner:
        raise WorkspaceSelectionError(
            "legacy_path_not_owner",
            detail="the compatibility path serves the legacy owner only",
        )
    return await select_default_workspace(conn, principal, allow_default=True)


__all__ = [
    "WorkspaceContext",
    "WorkspaceSelectionError",
    "select_workspace",
    "select_default_workspace",
    "legacy_owner_workspace",
    "workspace_capabilities",
    "require_capability",
    "ACTIVE_MEMBERSHIP_STATUSES",
    "SELECTABLE_WORKSPACE_STATUSES",
    "WORKSPACE_COMPATIBILITY_REQUIRED",
]
