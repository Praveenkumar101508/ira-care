"""The workspaces a signed-in user may act in — /api/v1/workspaces.

Every edition:

GET    /api/v1/workspaces                     -> the accessible workspaces
POST   /api/v1/workspaces/select              -> verify one selection

Where `workspace_management_available` (today: cloud only) —

POST   /api/v1/workspaces                     -> create one (P12-02)
PATCH  /api/v1/workspaces/{id}                -> rename one (P12-02)
DELETE /api/v1/workspaces/{id}/membership     -> leave one (P12-02)

The split is not cosmetic. The first two are how a client finds out which
workspaces exist and whether it must choose one, so they precede everything and
belong to both products. The three below them are tenant-shaped acts — see
`core/editions.py` for why a single-owner install has no use for them and no
client that calls them.

── Where the three new verbs get their authority (P12-02) ───────────────────

Derived from the models already in the schema, never invented:

* **create** is a TENANT-structural act. There is no workspace to hold a role
  in yet, so `workspace_role_capabilities` cannot answer it. The nearest
  existing decision is `invitations.INVITE_CAPABLE_ROLES` — who may change the
  shape of the tenant — and creating a workspace is the same kind of act, so it
  reuses that set rather than inventing a parallel one.
* **rename** changes a workspace setting, which `040` already names:
  `workspace.manage_settings`, held by `workspace_admin` alone.
* **leave** removes the caller's OWN membership, so it needs no capability —
  `manage_members` is for acting on other people. What it needs instead is a
  guard against stranding: the last administrator of a workspace may not walk
  out of it.

None of the three reads its authority from the request. The tenant comes from
the authenticated principal; the workspace id in the path goes through
`select_workspace` exactly as a header would, so a path parameter is no more
trusted than a header. A caller who is not a member gets the same uniform
refusal as every other failure, which is what stops the path parameter from
being a workspace-existence oracle.

── Why this router must never require a workspace ───────────────────────────

A client cannot send `X-Workspace-ID` before it knows which workspaces exist,
and it cannot know that without asking. Putting the workspace dependency on
this router would make workspace selection require a selected workspace: an
outage that reviews well, because every route "has the check". So both
endpoints take `require_auth` and resolve a Principal WITHOUT a workspace, and
`tests/tenancy/test_workspace_scope_boundary.py` pins that by path.

That is not a hole. Neither endpoint reads a workspace-scoped table: the
membership tables are tenant-scoped with forced RLS, so the listing runs inside
`tenant_connection` and a user sees their own tenant's workspaces and nothing
else. The list is derived from `workspace_memberships`, so a user who is a
member of two of their tenant's five workspaces is told about two.

── Why /select exists when every scoped route already verifies ──────────────

Every workspace-scoped route puts the header through `select_workspace`, so a
forged or stale selection is already refused at the point of use. `/select` is
not a second authority — it calls exactly the same function — it is a way for a
client to find out BEFORE it acts, so a user whose membership was revoked while
a tab was open is told at the point of switching rather than by a 403 on their
next save.

It follows that the client's selection is never trusted because `/select`
approved it. The verification that matters is the one on the request that
actually touches data, and that one happens every time.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import principal_from_username
from core.identity.db_context import tenant_connection
from core.identity.workspace import (
    SELECTABLE_WORKSPACE_STATUSES,
    WorkspaceSelectionError,
    require_capability,
    select_workspace,
    workspace_capabilities,
)

logger = logging.getLogger("ira.api.workspaces")

#: The picker. Mounted in EVERY edition, and it must be: a client cannot send
#: `X-Workspace-ID` before it knows which workspaces exist, and it is this
#: router's `selection_required` that tells the client whether it needs to.
router = APIRouter(prefix="/workspaces", tags=["workspaces"])

#: The three P12-02 management verbs — create, rename, leave. Mounted only where
#: `workspace_management_available` says so, which today means cloud.
#:
#: A separate router rather than a per-route guard because the gate is a MOUNT:
#: in an edition without workspace management the paths do not exist, are not in
#: `/docs`, and answer 404 rather than 403. `core/editions.py` records why that
#: distinction was chosen, and `app/routers.py` is where the choice is applied.
#:
#: Splitting them here rather than into a second module keeps the five verbs and
#: the reasoning above them in one file, which is where a reader looks for them.
management_router = APIRouter(prefix="/workspaces", tags=["workspaces"])

#: Tenant roles that may create a workspace.
#:
#: Imported from `invitations` rather than restated. Creating a workspace and
#: inviting a member are the same kind of act — both change the shape of the
#: tenant — and two constants that are meant to agree will eventually not.
#: `security_admin` and `billing_admin` are absent for the reason recorded
#: there: granting them structural control now would be inventing a capability
#: rather than mapping one.
from core.identity.invitations import INVITE_CAPABLE_ROLES  # noqa: E402

WORKSPACE_CREATE_CAPABLE_ROLES = INVITE_CAPABLE_ROLES


def _slugify(name: str) -> str:
    """A tenant-unique-ish slug. `046` has the last word via its unique index."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (slug or "workspace")[:60]


async def _verified_context(conn, principal, workspace_id):
    """`select_workspace`, with the route's uniform refusal.

    The path parameter goes through exactly the verification a header would.
    Every failure — not a member, another tenant's workspace, archived, no such
    workspace — becomes one 403 with one message, because telling them apart
    would turn `/workspaces/{id}` into an oracle for which workspaces exist.
    """
    try:
        return await select_workspace(conn, principal, workspace_id)
    except WorkspaceSelectionError as exc:
        logger.warning(
            "workspace action refused user=%s reason=%s detail=%s",
            principal.user_id, exc.reason, exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=WorkspaceSelectionError.PUBLIC_MESSAGE,
        ) from exc


class AccessibleWorkspace(BaseModel):
    """One workspace the caller is a verified, active member of."""

    id: uuid.UUID
    name: str
    slug: str
    role: str
    is_default: bool
    status: str


class WorkspaceList(BaseModel):
    workspaces: list[AccessibleWorkspace]
    count: int
    #: The tenant these workspaces belong to. The client stores it alongside the
    #: selection so that a session which somehow changes tenant invalidates the
    #: selection rather than carrying it across — see the frontend's
    #: `clearOnIdentityChange`.
    tenant_id: uuid.UUID
    #: True when the caller has exactly one. A UI may pre-highlight it; it must
    #: still send the header, and the server still verifies it. "Obvious" is a
    #: presentation decision, not an authorisation one.
    single: bool
    #: Which edition is answering. Stated rather than inferred, because the two
    #: editions differ in the next field and a client guessing wrong is either
    #: an outage or an unscoped write.
    edition: str
    #: True when this server will refuse a workspace-scoped request that does
    #: not name a workspace — the cloud rule. In the private edition the server
    #: still serves the verified legacy owner their default workspace, so a
    #: client that never selects one keeps working, which is what stops this
    #: retrofit from breaking the shipped single-owner product.
    #:
    #: The client asks rather than infers. A frontend that decided this for
    #: itself would either block sign-in on the private edition or let a cloud
    #: user reach the UI with no workspace and discover it one 400 at a time.
    selection_required: bool


class SelectBody(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=64)


class SelectedWorkspace(BaseModel):
    workspace_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    is_default: bool
    capabilities: list[str]


@router.get("", response_model=WorkspaceList)
@router.get("/", response_model=WorkspaceList, include_in_schema=False)
async def list_workspaces(username: str = Depends(require_auth)) -> WorkspaceList:
    """Every workspace the caller may act in, in one call.

    The predicate is the same one `select_workspace` applies row by row:
    `workspace_grants_access` (040's generated column, which already accounts
    for tenant-membership revocation cascading down) plus a selectable
    workspace status. Written as one query rather than "list then filter in
    Python" so that the list a picker shows and the set the verifier accepts
    cannot drift apart.

    Archived and deleted workspaces are absent rather than listed-and-greyed.
    A client that keeps a selection which stops appearing here treats it as
    gone, which is the behaviour wanted for archived, deleted and revoked alike
    — three causes, one client-side rule.
    """
    principal = await principal_from_username(username)

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT w.id, w.name, w.slug, w.is_default, w.status, m.role
              FROM workspaces w
              JOIN workspace_memberships m
                ON m.workspace_id = w.id
               AND m.tenant_id = w.tenant_id
             WHERE m.user_id = $1
               AND m.tenant_id = $2
               AND m.workspace_grants_access
               AND w.status = ANY($3::text[])
             ORDER BY w.is_default DESC, w.name ASC
            """,
            principal.user_id,
            principal.tenant_id,
            sorted(SELECTABLE_WORKSPACE_STATUSES),
        )

    workspaces = [
        AccessibleWorkspace(
            id=r["id"],
            name=r["name"],
            slug=r["slug"],
            role=r["role"],
            is_default=bool(r["is_default"]),
            status=r["status"],
        )
        for r in rows
    ]
    # Identifiers and a count. Workspace NAMES are customer content and do not
    # belong in a routine log line.
    logger.debug(
        "workspace list user=%s tenant=%s count=%d",
        principal.user_id,
        principal.tenant_id,
        len(workspaces),
    )
    # Read at call time, not at import time — the same reason
    # `resolve_workspace_principal` imports it inside the function.
    from core.editions import EDITION_CLOUD, current_edition

    edition = current_edition()
    return WorkspaceList(
        workspaces=workspaces,
        count=len(workspaces),
        tenant_id=principal.tenant_id,
        single=len(workspaces) == 1,
        edition=edition,
        selection_required=edition == EDITION_CLOUD,
    )


@router.post("/select", response_model=SelectedWorkspace)
async def select(
    body: SelectBody, username: str = Depends(require_auth)
) -> SelectedWorkspace:
    """Verify one selection and report what it carries.

    Refuses with the uniform public message for every reason — not a member,
    another tenant's workspace, archived, no such workspace — because telling
    those apart is a tenant-enumeration oracle. The real reason goes to the log.
    """
    principal = await principal_from_username(username)

    async with tenant_connection(principal) as conn:
        try:
            context = await select_workspace(conn, principal, body.workspace_id)
        except WorkspaceSelectionError as exc:
            logger.warning(
                "workspace selection refused user=%s reason=%s detail=%s",
                principal.user_id,
                exc.reason,
                exc.detail,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=WorkspaceSelectionError.PUBLIC_MESSAGE,
            ) from exc
        capabilities = sorted(await workspace_capabilities(conn, context.workspace_role))

    return SelectedWorkspace(
        workspace_id=context.workspace_id,
        tenant_id=context.tenant_id,
        role=context.workspace_role,
        is_default=context.is_default,
        capabilities=capabilities,
    )


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CreatedWorkspace(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    tenant_id: uuid.UUID
    role: str
    is_default: bool


class RenamedWorkspace(BaseModel):
    id: uuid.UUID
    name: str


class LeftWorkspace(BaseModel):
    workspace_id: uuid.UUID
    left: bool


@management_router.post("", response_model=CreatedWorkspace, status_code=201)
async def create_workspace(
    body: CreateBody, username: str = Depends(require_auth)
) -> CreatedWorkspace:
    """Create a workspace in the caller's own tenant, and join it.

    The tenant is the authenticated principal's. There is deliberately no
    tenant field in `CreateBody`: a body that could name a tenant would be a
    body that could name somebody else's.

    **The creator is made a `workspace_admin` in the same transaction.** Not a
    convenience — a workspace with no members is unreachable by everybody
    including the person who just made it, because `select_workspace` requires a
    membership row and there is no path that invents one. Creating one and
    joining it are therefore a single atomic act or neither happens.

    `is_default` is never set. `033`'s partial unique index allows exactly one
    active default per tenant, and the tenant already has the one
    `provision_tenant_substrate` gave it; a second would be a constraint
    violation surfacing as a 500 rather than a decision.
    """
    principal = await principal_from_username(username)

    async with tenant_connection(principal) as conn:
        # The tenant ROLE is read from the database inside the bound
        # transaction, not taken from `principal.roles`. Same reasoning as
        # `select_workspace`: the row is the authority, and reading it through
        # the same boundary the policies use means Python cannot disagree with
        # them about what "active" means.
        membership = await conn.fetchrow(
            """SELECT role, membership_grants_access
                 FROM tenant_memberships
                WHERE tenant_id = $1 AND user_id = $2""",
            principal.tenant_id, principal.user_id,
        )
        if membership is None or not membership["membership_grants_access"]:
            logger.warning("workspace create refused user=%s reason=no_active_membership",
                           principal.user_id)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=WorkspaceSelectionError.PUBLIC_MESSAGE)
        if membership["role"] not in WORKSPACE_CREATE_CAPABLE_ROLES:
            logger.warning("workspace create refused user=%s reason=role_not_capable role=%s",
                           principal.user_id, membership["role"])
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=WorkspaceSelectionError.PUBLIC_MESSAGE)

        # The organisation the workspace belongs to. Read rather than chosen: a
        # tenant with several organisations has no obvious "the" one, and
        # picking the first would put the workspace somewhere nobody asked for.
        orgs = await conn.fetch(
            "SELECT id FROM organisations WHERE tenant_id = $1", principal.tenant_id)
        if len(orgs) != 1:
            logger.warning(
                "workspace create refused user=%s reason=organisation_ambiguous count=%d",
                principal.user_id, len(orgs))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("This tenant does not have exactly one organisation, so "
                        "there is no unambiguous place to create a workspace."),
            )

        slug = _slugify(body.name)
        try:
            row = await conn.fetchrow(
                """INSERT INTO workspaces (tenant_id, organisation_id, name, slug,
                                           is_default, status)
                   VALUES ($1, $2, $3, $4, FALSE, 'active')
                   RETURNING id, name, slug, is_default""",
                principal.tenant_id, orgs[0]["id"], body.name.strip(), slug,
            )
        except Exception as exc:  # noqa: BLE001 — the unique index is the check
            # `046`'s (tenant_id, slug) unique index. A 409 rather than a 500:
            # the caller can fix this by choosing another name.
            logger.warning("workspace create refused user=%s reason=slug_taken slug=%s: %s",
                           principal.user_id, slug, type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A workspace with a similar name already exists in this tenant.",
            ) from exc

        await conn.execute(
            """INSERT INTO workspace_memberships
                   (tenant_id, workspace_id, user_id, role, status)
               VALUES ($1, $2, $3, 'workspace_admin', 'active')""",
            principal.tenant_id, row["id"], principal.user_id,
        )

    logger.info("workspace created user=%s tenant=%s workspace=%s",
                principal.user_id, principal.tenant_id, row["id"])
    return CreatedWorkspace(
        id=row["id"], name=row["name"], slug=row["slug"],
        tenant_id=principal.tenant_id, role="workspace_admin",
        is_default=bool(row["is_default"]),
    )


@management_router.patch("/{workspace_id}", response_model=RenamedWorkspace)
async def rename_workspace(
    workspace_id: uuid.UUID, body: RenameBody,
    username: str = Depends(require_auth),
) -> RenamedWorkspace:
    """Rename a workspace the caller administers.

    Two gates, in order, and the order matters. `select_workspace` first, so a
    caller with no membership is refused identically to one naming a workspace
    that does not exist. Only then the capability, so the second refusal cannot
    reveal that the workspace was real.
    """
    principal = await principal_from_username(username)

    async with tenant_connection(principal) as conn:
        context = await _verified_context(conn, principal, workspace_id)
        scoped = context.apply_to(principal)
        try:
            await require_capability(conn, scoped, "workspace.manage_settings")
        except WorkspaceSelectionError as exc:
            logger.warning("workspace rename refused user=%s reason=%s detail=%s",
                           principal.user_id, exc.reason, exc.detail)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=WorkspaceSelectionError.PUBLIC_MESSAGE) from exc

        # The UPDATE is scoped by tenant as well as id — belt to RLS's braces,
        # the same pairing `_delete_scoped` uses in erasure.
        updated = await conn.fetchrow(
            """UPDATE workspaces SET name = $1, updated_at = now()
                WHERE id = $2 AND tenant_id = $3
                RETURNING id, name""",
            body.name.strip(), context.workspace_id, principal.tenant_id,
        )

    # A verified selection that then matched no row means the policy and the
    # verification disagree. Reporting success here would be a rename that
    # renamed nothing — D3 item 12.
    if updated is None:
        logger.error("workspace rename matched no row user=%s workspace=%s",
                     principal.user_id, context.workspace_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The workspace could not be updated.",
        )
    logger.info("workspace renamed user=%s workspace=%s",
                principal.user_id, context.workspace_id)
    return RenamedWorkspace(id=updated["id"], name=updated["name"])


@management_router.delete("/{workspace_id}/membership", response_model=LeftWorkspace)
async def leave_workspace(
    workspace_id: uuid.UUID, username: str = Depends(require_auth)
) -> LeftWorkspace:
    """Leave a workspace the caller is a member of.

    No capability is required, because this acts on the caller's own
    membership; `workspace.manage_members` is for acting on other people's.
    What it does require is that the caller is verifiably a member, which
    `select_workspace` establishes — so "leave a workspace I was never in" is
    refused by the same uniform message as everything else rather than
    answering whether it exists.

    **The last administrator may not leave.** A workspace whose only
    `workspace_admin` walks out cannot be renamed, cannot have members added,
    and cannot be administered by anybody — including a tenant owner, because
    `require_capability` deliberately does not consult the tenant role
    (CLOUD-ADR-0016). That is an unrecoverable state reachable by one ordinary
    request, so it is refused.
    """
    principal = await principal_from_username(username)

    async with tenant_connection(principal) as conn:
        context = await _verified_context(conn, principal, workspace_id)

        if context.workspace_role == "workspace_admin":
            others = await conn.fetchval(
                """SELECT count(*) FROM workspace_memberships
                    WHERE workspace_id = $1 AND tenant_id = $2
                      AND user_id <> $3 AND role = 'workspace_admin'
                      AND workspace_grants_access""",
                context.workspace_id, principal.tenant_id, principal.user_id,
            )
            if not others:
                logger.warning("workspace leave refused user=%s reason=last_admin workspace=%s",
                               principal.user_id, context.workspace_id)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=("You are the only administrator of this workspace. "
                            "Make somebody else an administrator first, or the "
                            "workspace would be left with nobody who can manage it."),
                )

        result = await conn.execute(
            """DELETE FROM workspace_memberships
                WHERE workspace_id = $1 AND tenant_id = $2 AND user_id = $3""",
            context.workspace_id, principal.tenant_id, principal.user_id,
        )

    # Counted, not assumed. A verified membership that deletes zero rows means
    # the policy hid the row the verification just read, and reporting `left`
    # would tell a user they had left a workspace they can still reach.
    removed = int(str(result).rsplit(" ", 1)[-1]) if result else 0
    if removed != 1:
        logger.error("workspace leave removed %d row(s) user=%s workspace=%s",
                     removed, principal.user_id, context.workspace_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The membership could not be removed.",
        )
    logger.info("workspace left user=%s workspace=%s",
                principal.user_id, context.workspace_id)
    return LeftWorkspace(workspace_id=context.workspace_id, left=True)


__all__ = ["router"]
