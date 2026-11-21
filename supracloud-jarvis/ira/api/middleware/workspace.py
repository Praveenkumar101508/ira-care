"""The single place an HTTP request acquires a verified workspace.

Batch 5A built the verification (`core.identity.workspace.select_workspace`) and
the binding (`core.identity.db_context.workspace_connection`) but wired neither
to a route. This is the wire. Every route that touches one of the seventeen
workspace-scoped tables takes its context from here and from nowhere else, so
there is exactly one function to audit when the question is "how does a request
end up bound to a workspace".

── What the client may say, and what it may decide ──────────────────────────

A client may send `X-Workspace-ID`. That is a **request to select a workspace**,
not a claim to be in one: it goes straight into `select_workspace`, which asks
the database about tenant membership, workspace ownership by that tenant,
workspace membership and both statuses before returning anything. A header
naming another tenant's workspace produces exactly the same refusal as a header
naming a workspace that does not exist, because telling those two apart is a
tenant-enumeration oracle.

Nothing else in the request is consulted. Not the body, not a query parameter,
not a cookie, not the username.

── When no workspace is named ───────────────────────────────────────────────

The two editions answer this differently and deliberately:

  * **Private.** Falls back to `legacy_owner_workspace()`, which serves the
    verified legacy owner of the deterministic private tenant and nobody else.
    IRA shipped as a single-owner application with no workspace concept in its
    API or its UI; refusing every unheadered request would break the running
    product to enforce a boundary that has exactly one side. The fallback is
    still a full verification — it defers to `select_workspace` — so it is a
    narrower entry point, not a hole.

  * **Cloud.** Refused. There is no default-workspace fallback, no first
    workspace and no private-tenant fallback in a multi-tenant process. A cloud
    request that does not say which workspace it means is a request whose
    intent is unknown, and guessing writes one customer's content somewhere
    they did not choose. `WorkspaceRequired` is a 400, not a 403: the caller is
    not forbidden, they are incomplete.

── Why the connection is opened here ────────────────────────────────────────

Verification needs a tenant-bound connection (the membership tables have forced
RLS), and binding needs the verified result. Doing both in the dependency means
a route handler receives a Principal that is already carrying a verified
workspace and cannot receive one that is not. The alternative — hand the route a
bare Principal and trust it to verify — is a rule enforced by whoever remembers
it, which is the thing this whole retrofit is replacing.

The dependency does NOT hold a connection open for the life of the request. It
opens one to verify, closes it, and returns the Principal. The route then opens
`workspace_connection(principal)` for its own work. Two short transactions
rather than one long one: a pooled connection held across an LLM call is a pool
exhausted under load.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from api.middleware.auth import require_auth
from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal
from core.identity.workspace import (
    WorkspaceSelectionError,
    legacy_owner_workspace,
    select_workspace,
)
from core.runtime.request_context import set_principal

logger = logging.getLogger("ira.api.workspace")

#: The header a client uses to ask for a workspace. Named here so the string
#: appears once and the frontend, the tests and the docs can import it rather
#: than retype it.
WORKSPACE_HEADER = "X-Workspace-ID"

#: The header a client uses to ask for a TENANT (P9-03, finding F9-5).
#:
#: A user with two active memberships could not reach any of these surfaces:
#: `resolve_principal` refuses to choose between them — correctly, since
#: guessing applies one customer's rules to another — and nothing gave the
#: caller a way to say which they meant. The refusal was right and the product
#: was unusable for exactly the users a multi-tenant product exists for.
#:
#: Like `WORKSPACE_HEADER`, this is a REQUEST, never an assertion. Membership is
#: verified against `tenant_memberships` inside `resolve_principal` before it is
#: honoured, so a forged value resolves to nothing. Nothing downstream reads the
#: header; they read the resolved principal.
TENANT_HEADER = "X-Tenant-ID"


class WorkspaceRequired(HTTPException):
    """No workspace was named and this edition will not choose one."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This request must name a workspace in the {WORKSPACE_HEADER} "
                f"header. The server does not select one on your behalf."
            ),
        )


class TenantRequired(HTTPException):
    """The caller belongs to several tenants and named none.

    400 rather than 503, which is what this used to be. `AmbiguousTenant` is not
    an outage — it is a request the client can fix by adding one header, and
    reporting it as "service unavailable" told the user to wait for something
    that was never going to happen.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{detail} Name the tenant in the {TENANT_HEADER} header."
            ).strip(),
        )


class TenantForbidden(HTTPException):
    """The caller named a tenant they hold no access-granting membership in.

    403, and deliberately the SAME 403 whether the tenant exists or not —
    `resolve_principal` refuses both identically so a response cannot be used to
    discover which tenants exist. Only the status code is new here; the message
    is still the uniform one.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access-granting membership in the requested tenant.",
        )


def _parse_tenant_header(raw: Optional[str]):
    """The header value as a UUID, or None if absent. Malformed → 400.

    Parsed HERE rather than passed on as a string, because `resolve_principal`
    compares it to `tenant_memberships.tenant_id`, which asyncpg returns as a
    `uuid.UUID`. A string would compare unequal to every membership and be
    refused as "not a member" — a legitimate caller told they do not belong to
    their own tenant, with nothing in the log saying why.
    """
    import uuid as _uuid

    if raw is None or not raw.strip():
        return None
    try:
        return _uuid.UUID(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{TENANT_HEADER} must be a UUID.",
        ) from exc


async def principal_from_username(
    username: str, requested_tenant_id: Optional[str] = None
) -> Principal:
    """An authenticated username → a Principal.

    `requested_tenant_id` is the raw `X-Tenant-ID` header. It is a request for a
    tenant, not a claim to one: `resolve_principal` checks it against membership
    and refuses if it does not hold.

    Three failures, three answers, and the split matters:

      * **ambiguous** — several memberships, none named. 400: the client can fix
        it, and it must be told how.
      * **not permitted** — a tenant was named and the caller is not in it. 403,
        uniform, indistinguishable from "no such tenant".
      * **anything else** — unknown, suspended, no membership at all, or the
        identity register unreachable. 503, unchanged.

    That last group stays deliberately lumped together. `_resolve_cloud_principal`
    funnels a database outage into the same exception as a refusal on purpose —
    *"'I could not tell' must never be a different code path from 'no'"* — and
    splitting it here would undo that one layer up. Only the two cases the
    resolver ALREADY distinguishes by type get their own status.
    """
    from core.identity.resolver import AmbiguousTenant, TenantNotPermitted

    tenant = _parse_tenant_header(requested_tenant_id)
    try:
        return await resolve_principal(username, tenant_id=tenant)
    except AmbiguousTenant as exc:
        raise TenantRequired(str(exc)) from exc
    except TenantNotPermitted as exc:
        logger.warning("tenant refused user=%s tenant=%s", username, tenant)
        raise TenantForbidden() from exc
    except CloudIdentityNotAvailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


async def resolve_workspace_principal(
    username: str, requested_workspace_id: Optional[str],
    requested_tenant_id: Optional[str] = None,
) -> Principal:
    """The whole decision, as one testable function.

    Separated from the FastAPI dependency below so tests can drive it with a
    username and a header value instead of standing up an app.

    The tenant is resolved BEFORE the workspace, and has to be: a workspace is
    verified against the principal's tenant, so with several memberships and no
    tenant named there is no tenant to verify it against — the request was
    refused before the workspace header was ever read.
    """
    # Imported here rather than at module scope so the edition is read at call
    # time. A module-level `from core.editions import current_edition` binds the
    # function object once, which makes the branch below untestable and makes
    # the module's behaviour depend on import order — the same reason
    # `db_context.owner_connection` resolves it inside the function.
    from core.editions import EDITION_CLOUD, current_edition

    principal = await principal_from_username(username, requested_tenant_id)

    async with tenant_connection(principal) as conn:
        try:
            if requested_workspace_id:
                context = await select_workspace(conn, principal, requested_workspace_id)
            elif current_edition() == EDITION_CLOUD:
                raise WorkspaceRequired()
            else:
                context = await legacy_owner_workspace(conn, principal)
        except WorkspaceSelectionError as exc:
            # `reason`/`detail` name the actual cause and stay in the log. The
            # caller gets the uniform public message, which is identical for
            # "not a member", "another tenant's workspace" and "no such
            # workspace" — see WorkspaceSelectionError.PUBLIC_MESSAGE.
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

    scoped = context.apply_to(principal)

    # ── Phase 8: the model gateway needs to know whose request this is ──────
    #
    # Bound HERE, and nowhere earlier, because here is the first point at which
    # membership has been verified and a workspace selected. Binding it from the
    # authenticated username alone would put an unverified scope where metering
    # reads it.
    #
    # Nothing downstream reads this to decide access — RLS and
    # `require_capability_or_403` do that, unchanged. What it carries is an
    # answer authorisation has already reached, so a quota check forty-three
    # modules away can attribute a call without every one of them remembering
    # to pass a principal.
    set_principal(scoped)
    return scoped


async def require_capability_or_403(conn, principal: Principal, capability: str) -> None:
    """Refuse unless the principal's workspace role carries `capability`.

    A thin HTTP wrapper over `core.identity.workspace.require_capability`, here
    so that every route renders a denied capability the same way: 403 with the
    uniform public message, and the real reason in the log rather than the
    response. A route that let `WorkspaceSelectionError` escape would produce a
    500 whose stack trace names tables.

    This is a check *on top of* RLS, never instead of it. Batch 5C's policy
    stage is what makes the database refuse; this makes the refusal legible and
    early. A capability check that is the only boundary is one forgotten call
    away from being no boundary.
    """
    from core.identity.workspace import require_capability

    try:
        await require_capability(conn, principal, capability)
    except WorkspaceSelectionError as exc:
        logger.warning(
            "capability refused user=%s workspace=%s capability=%s reason=%s detail=%s",
            principal.user_id,
            principal.workspace_id,
            capability,
            exc.reason,
            exc.detail,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=WorkspaceSelectionError.PUBLIC_MESSAGE,
        ) from exc


async def governed_principal(
    username: str = Depends(require_auth),
    x_tenant_id: Optional[str] = Header(default=None, alias=TENANT_HEADER),
) -> Optional[Principal]:
    """The principal a governed action is decided for — or None in private.

    Added by P9-07, for the action surfaces (`/actions`, `/notes`,
    `/calendar/dav`) that stage and confirm side effects. They need a verified
    principal in cloud, where the tenant's Constitution decides, and must keep
    working in the private edition, where there is one owner, no tenant
    identity substrate and nothing to load rules from.

    `require_tenant_principal` cannot serve both: in the private edition it
    raises, because there is no cloud identity to resolve. Returning None there
    is not a fallback or a degraded principal — it is the honest answer to
    "which tenant is this action for" in a deployment that has no tenants, and
    `gated_execution` reads it as "use the owner gate", which is that edition's
    actual authority.

    In CLOUD it never returns None. A missing or unverifiable tenant raises
    through `principal_from_username`, so the fail-closed direction is kept:
    the one thing this must never do is hand the cloud edition a None and let
    the owner gate answer, because `is_owner` is False there and the action
    would be refused for the wrong reason — or, if that ever changed, allowed
    for no reason at all.
    """
    from core.editions import is_cloud

    if not is_cloud():
        return None
    return await principal_from_username(username, x_tenant_id)


async def require_tenant_principal(
    username: str = Depends(require_auth),
    x_tenant_id: Optional[str] = Header(default=None, alias=TENANT_HEADER),
) -> Principal:
    """FastAPI dependency: a Principal carrying a VERIFIED TENANT, no workspace.

    The right dependency for the tenant-scoped surfaces — governance, the model
    catalogue, connectors, companions. Their tables carry `tenant_id` and no
    `workspace_id`, so `require_workspace_principal` would demand a workspace
    header those routes have no use for, and P9-01 withdrew a finding that said
    they should.

    It exists mainly so that a multi-tenant user can reach them at all: the six
    route modules that each grew a private copy of

        try: return await resolve_principal(username)
        except CloudIdentityNotAvailable: raise HTTPException(503, ...)

    had no parameter to pass a tenant through and reported the ambiguity as an
    outage. One dependency, one place the header is read, one place the statuses
    are decided.
    """
    return await principal_from_username(username, x_tenant_id)


async def require_workspace_principal(
    username: str = Depends(require_auth),
    x_workspace_id: Optional[str] = Header(default=None, alias=WORKSPACE_HEADER),
    x_tenant_id: Optional[str] = Header(default=None, alias=TENANT_HEADER),
) -> Principal:
    """FastAPI dependency: a Principal carrying a verified workspace.

    Wire it as::

        from api.middleware.workspace import require_workspace_principal

        @router.get("/things")
        async def list_things(p: Principal = Depends(require_workspace_principal)):
            async with workspace_connection(p) as conn:
                ...

    A route using this cannot accidentally run unscoped: `workspace_connection`
    raises when `workspace_id` is None, and the only way to get a non-None one
    is to have come through the verification above.
    """
    return await resolve_workspace_principal(username, x_workspace_id, x_tenant_id)


__all__ = [
    "WORKSPACE_HEADER",
    "TENANT_HEADER",
    "WorkspaceRequired",
    "TenantRequired",
    "TenantForbidden",
    "principal_from_username",
    "resolve_workspace_principal",
    "require_tenant_principal",
    "require_workspace_principal",
    "require_capability_or_403",
]
