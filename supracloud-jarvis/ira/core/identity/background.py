"""Tenant context for background work.

A scheduled job, worker task or migration has no request and therefore no
`Depends(require_auth)` to hand it a Principal. The tempting shortcut — let
background code run unscoped, or give it a "system" tenant that can see
everything — is precisely how a nightly job leaks one tenant's data into
another's summary.

The rule here: **background work must be handed a tenant explicitly, and runs
for exactly one tenant at a time.** Work that must span tenants iterates over
them, one scoped context each, rather than running once with the boundary off.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Optional, Sequence

from core.editions import EDITION_PRIVATE, EditionName
from core.identity.db_context import MissingTenantContext
from core.identity.principal import IRA_UUID_NAMESPACE, Principal
from core.identity.workspace import SELECTABLE_WORKSPACE_STATUSES

logger = logging.getLogger("ira.identity.background")


def background_principal(
    tenant_id: uuid.UUID,
    *,
    job: str,
    edition: EditionName = EDITION_PRIVATE,
    scopes: frozenset[str] | None = None,
) -> Principal:
    """A Principal for one job, bound to one tenant.

    Not the owner, and unscoped unless scopes are passed deliberately —
    background work must not inherit owner privilege just because nobody is
    logged in. The job name is part of the derived user id so audit rows can be
    attributed to the job that wrote them.
    """
    if tenant_id is None:
        raise MissingTenantContext(
            f"Background job {job!r} must be given a tenant_id. Background work "
            "never runs unscoped."
        )
    return Principal(
        user_id=uuid.uuid5(IRA_UUID_NAMESPACE, f"job:{job}"),
        tenant_id=tenant_id,
        session_id=uuid.uuid4(),
        edition=edition,
        roles=frozenset(),
        scopes=scopes or frozenset(),
        username="",
        is_owner=False,
    )


@asynccontextmanager
async def for_tenant(
    tenant_id: uuid.UUID, *, job: str, edition: EditionName = EDITION_PRIVATE
) -> AsyncGenerator[Principal, None]:
    """Run a block of background work scoped to one tenant."""
    principal = background_principal(tenant_id, job=job, edition=edition)
    logger.debug("background job %s scoped to %s", job, principal.safe_repr())
    yield principal


async def for_each_tenant(
    tenant_ids: Sequence[uuid.UUID], *, job: str, edition: EditionName = EDITION_PRIVATE
) -> AsyncGenerator[Principal, None]:
    """Iterate tenants, yielding a separate scoped Principal for each.

    This is the supported way to do cross-tenant work: N scoped passes, never
    one unscoped pass. Each iteration's queries are bound to that tenant by RLS,
    so a bug in the job body cannot mix rows from two tenants into one result.
    """
    for tenant_id in tenant_ids:
        yield background_principal(tenant_id, job=job, edition=edition)


# ─────────────────────────────────────────────────────────────────────────────
# Workspace context for jobs (Phase 3B batch 5A)
#
# The rule a background job must obey, and the one it is most likely to break:
#
#     A job's workspace is decided when the job is ENQUEUED, by whoever had a
#     verified context at the time. It is never decided when the job RUNS.
#
# Deciding at execution time is the tempting shortcut and it is wrong in three
# separate ways. The enqueuing user may have switched workspaces since; they may
# have lost membership since; and a retry three hours later would resolve to
# something different from the original attempt, so the same job would write to
# different places on different runs. `JobContext` is frozen and carried through
# retries for exactly that reason.
# ─────────────────────────────────────────────────────────────────────────────


class MissingJobContext(MissingTenantContext):
    """A workspace-scoped job was enqueued or run without a verified context."""


@dataclass(frozen=True, slots=True)
class JobContext:
    """The immutable identity a background job carries from enqueue to completion.

    Serialised into the job payload, restored on execution, and preserved
    verbatim across retries and into the dead-letter record. `job_id` is stable
    across retries — a retry is the same job, not a new one — which is what
    makes a dead-letter entry traceable back to the attempt that created it.
    """

    tenant_id: uuid.UUID
    workspace_id: Optional[uuid.UUID]
    requested_by: Optional[uuid.UUID]
    job: str
    job_id: uuid.UUID
    created_at: str

    #: What the enqueuing principal was allowed to do AT ENQUEUE TIME.
    #:
    #: Recorded for the audit trail, and deliberately NOT used as the
    #: authorisation decision when the job runs — see `authorise()`. A job that
    #: trusted this would keep working after the user was removed, which is the
    #: whole failure this field exists to make visible rather than to enable.
    authorisation: str = ""

    #: Ties the job to the request that created it, across the queue, the logs
    #: and the dead-letter record. Not an authorisation input.
    correlation_id: Optional[uuid.UUID] = None

    def to_payload(self) -> dict:
        """Identifiers only — never a name, an email or any customer content.

        A job payload is written to a queue, logged when it fails, and kept in a
        dead-letter record for as long as somebody might investigate it. That is
        three places customer content should not be.
        """
        return {
            "tenant_id": str(self.tenant_id),
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
            "requested_by": str(self.requested_by) if self.requested_by else None,
            "job": self.job,
            "job_id": str(self.job_id),
            "created_at": self.created_at,
            "authorisation": self.authorisation,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "JobContext":
        """Restore a context from a queue payload.

        Refuses a payload with no tenant. A job that reached the queue without
        one cannot be run safely, and running it "as somebody" is the failure
        this whole module exists to prevent — so it is rejected on the way out
        of the queue as well as on the way in.
        """
        tenant = payload.get("tenant_id")
        if not tenant:
            raise MissingJobContext(
                f"Job {payload.get('job', '?')!r} has no tenant_id in its payload. "
                "Refusing to execute: there is no safe tenant to assume."
            )
        workspace = payload.get("workspace_id")
        requested_by = payload.get("requested_by")
        return cls(
            tenant_id=uuid.UUID(tenant),
            workspace_id=uuid.UUID(workspace) if workspace else None,
            requested_by=uuid.UUID(requested_by) if requested_by else None,
            job=payload.get("job", ""),
            job_id=uuid.UUID(payload["job_id"]) if payload.get("job_id") else uuid.uuid4(),
            created_at=payload.get("created_at", ""),
            authorisation=payload.get("authorisation", ""),
            correlation_id=(
                uuid.UUID(payload["correlation_id"])
                if payload.get("correlation_id") else None
            ),
        )

    def principal(self, *, edition: EditionName = EDITION_PRIVATE) -> Principal:
        """A Principal carrying this job's verified context.

        Not the owner and unscoped, like every background principal. The
        workspace comes from the context recorded at enqueue time — the worker
        does not select one, and there is no code path here that could.
        """
        base = background_principal(self.tenant_id, job=self.job, edition=edition)
        if self.workspace_id is None:
            return base
        from dataclasses import replace as _replace

        return _replace(base, workspace_id=self.workspace_id)


def job_context(
    principal: Principal,
    *,
    job: str,
    workspace_scoped: bool,
    now: str,
    job_id: uuid.UUID | None = None,
    authorisation: str = "",
    correlation_id: uuid.UUID | None = None,
) -> JobContext:
    """Capture the enqueuing principal's verified context for a job.

    `workspace_scoped` is a required keyword because it is the decision that
    determines whether a missing workspace is acceptable or fatal, and it should
    be visible at the enqueue site rather than inferred from the job name.

    `now` is passed in rather than read from the clock here so the caller owns
    the timestamp and tests are deterministic.
    """
    if principal is None or getattr(principal, "tenant_id", None) is None:
        raise MissingJobContext(f"Job {job!r} must be enqueued with a tenant context.")
    workspace_id = getattr(principal, "workspace_id", None)
    if workspace_scoped and workspace_id is None:
        raise MissingJobContext(
            f"Job {job!r} is workspace-scoped and the enqueuing principal has no "
            f"active workspace. Refusing: a job with no workspace would have to "
            f"guess one at execution time, and the default workspace is not a "
            f"safe guess for content somebody else will read."
        )
    return JobContext(
        tenant_id=principal.tenant_id,
        workspace_id=workspace_id if workspace_scoped else None,
        requested_by=principal.user_id,
        job=job,
        job_id=job_id or uuid.uuid4(),
        created_at=now,
        authorisation=authorisation,
        correlation_id=correlation_id,
    )


async def default_workspace_job_principal(tenant_id: uuid.UUID, *, job: str) -> Principal:
    """A job principal bound to `tenant_id`'s DEFAULT workspace.

    For background work that genuinely has no enqueuing user and no request —
    the retention purge, the heartbeat, the always-on brain. It is NOT a
    general fallback and must never be reached from a request path: a request
    always has somebody to ask, and `select_workspace` is how you ask them.

    The distinction that makes this acceptable rather than the
    default-workspace fallback every other module refuses: there is no user
    here whose intent could have been consulted and was not. The job is the
    tenant's own housekeeping, and the default workspace is the only workspace
    a tenant is guaranteed to have.

    It still refuses rather than guessing when that guarantee does not hold —
    a tenant with no active default workspace raises, and the caller decides
    whether to skip that tenant or fail.
    """
    from core.identity.db_context import tenant_connection

    principal = background_principal(tenant_id, job=job)

    # Read through the tenant boundary, not a bare connection: `workspaces` is
    # tenant-scoped with forced RLS, so what comes back is this tenant's own by
    # construction rather than by a WHERE clause that has to be right.
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, status FROM workspaces WHERE tenant_id = $1 AND is_default",
            tenant_id,
        )

    # `is_default` is the CANONICAL marker `039_workspace_substrate.sql` sets
    # when it provisions a tenant, and `033`'s deferred trigger allows exactly
    # one per tenant. This is emphatically not "the first workspace": if the
    # marker is missing, or somehow duplicated, the job SKIPS. Picking one
    # would be choosing on the tenant's behalf, which is what every fallback in
    # this codebase refuses to do.
    if not rows:
        raise MissingJobContext(
            f"Job {job!r}: tenant {tenant_id} has no workspace marked "
            f"is_default, so there is no canonical place for this work. "
            f"Expected from 039_workspace_substrate.sql. Skipping is correct — "
            f"choosing a workspace here would be choosing for the customer."
        )
    if len(rows) > 1:
        raise MissingJobContext(
            f"Job {job!r}: tenant {tenant_id} has {len(rows)} workspaces marked "
            f"is_default. The canonical workspace is ambiguous, so this job "
            f"does not run. `033`'s one-default constraint should make this "
            f"unreachable; that it happened is the finding."
        )
    if rows[0]["status"] not in SELECTABLE_WORKSPACE_STATUSES:
        raise MissingJobContext(
            f"Job {job!r}: tenant {tenant_id}'s default workspace is "
            f"{rows[0]['status']!r}, not active. Housekeeping does not write "
            f"into an archived or half-provisioned workspace."
        )
    workspace_id = rows[0]["id"]
    from dataclasses import replace

    return replace(principal, workspace_id=workspace_id)


class JobAuthorisationRevoked(MissingJobContext):
    """The enqueuing user no longer has the access the job was created under."""


async def authorise(context: JobContext, *, edition: EditionName = EDITION_PRIVATE) -> Principal:
    """Re-check the enqueuing user's access NOW, and return a bound principal.

    The rule this enforces, and the reason the whole module exists:

        **A job carries its context, not its permission.**

    `JobContext` fixes WHERE the work happens — decided when the user
    authorised it, so a retry three hours later writes to the same place as the
    first attempt. It does not fix WHETHER it may still happen. Between enqueue
    and execution the user can be removed from the tenant, removed from the
    workspace, suspended, or have the workspace archived beneath them. A queue
    that trusted the enqueue-time decision would keep serving somebody who was
    revoked — for as long as the backlog is deep, which is exactly when it
    matters.

    So this asks the database four questions at execution time:

      * is the workspace still in this tenant, and still active;
      * does the user still hold an access-granting tenant membership;
      * does the user still hold an access-granting workspace membership;
      * (implicitly) does the tenant still exist.

    All four are answered on a tenant-bound connection, so the membership rows
    are read through the same boundary the policies use.

    Raises `JobAuthorisationRevoked` when any of them says no. The caller's
    correct response is to drop the job and record why — not to retry it, which
    would just ask a settled question again.

    Housekeeping jobs — no `requested_by`, because no user asked for them — go
    through `default_workspace_job_principal` instead. They have no membership
    to revalidate, and inventing one for them would be the "select the first
    workspace" move this module refuses.
    """
    from core.identity.db_context import tenant_connection

    if context.requested_by is None:
        raise JobAuthorisationRevoked(
            f"Job {context.job!r} has no initiating user, so there is no "
            f"membership to revalidate. Tenant housekeeping must use "
            f"default_workspace_job_principal() instead — it is the only path "
            f"allowed to run without a user, and it verifies the canonical "
            f"default workspace rather than choosing one."
        )
    if context.workspace_id is None:
        raise JobAuthorisationRevoked(
            f"Job {context.job!r} is workspace-scoped but carries no workspace."
        )

    probe = background_principal(context.tenant_id, job=context.job, edition=edition)
    async with tenant_connection(probe) as conn:
        workspace = await conn.fetchrow(
            "SELECT status FROM workspaces WHERE id = $1 AND tenant_id = $2",
            context.workspace_id, context.tenant_id)
        if workspace is None:
            raise JobAuthorisationRevoked(
                f"Job {context.job!r} ({context.job_id}): workspace "
                f"{context.workspace_id} is gone or not in tenant "
                f"{context.tenant_id}.")
        if workspace["status"] not in SELECTABLE_WORKSPACE_STATUSES:
            raise JobAuthorisationRevoked(
                f"Job {context.job!r} ({context.job_id}): workspace "
                f"{context.workspace_id} is {workspace['status']!r}, not active.")

        tenant_member = await conn.fetchrow(
            """SELECT membership_grants_access FROM tenant_memberships
                WHERE tenant_id = $1 AND user_id = $2""",
            context.tenant_id, context.requested_by)
        if tenant_member is None or not tenant_member["membership_grants_access"]:
            raise JobAuthorisationRevoked(
                f"Job {context.job!r} ({context.job_id}): user "
                f"{context.requested_by} no longer holds an active membership "
                f"of tenant {context.tenant_id}. Refusing to run work they "
                f"queued before losing it.")

        ws_member = await conn.fetchrow(
            """SELECT role, workspace_grants_access FROM workspace_memberships
                WHERE workspace_id = $1 AND user_id = $2 AND tenant_id = $3""",
            context.workspace_id, context.requested_by, context.tenant_id)
        if ws_member is None or not ws_member["workspace_grants_access"]:
            raise JobAuthorisationRevoked(
                f"Job {context.job!r} ({context.job_id}): user "
                f"{context.requested_by} no longer has access to workspace "
                f"{context.workspace_id}.")

    from dataclasses import replace as _replace

    # The principal runs AS THE INITIATING USER, not as an anonymous job
    # identity: rows the job writes should be attributable to the person who
    # asked for the work, and `app.user_id` drives the USER_PRIVATE policies.
    return _replace(
        probe,
        user_id=context.requested_by,
        workspace_id=context.workspace_id,
        workspace_role=ws_member["role"],
    )


def preserve_for_retry(context: JobContext) -> JobContext:
    """The context a retry must run with — the original, unchanged.

    A function rather than "just reuse the object", so the requirement is
    stated where a retry loop is written and a test can assert it directly.
    Re-deriving the context on retry is the bug; this is the shape of not doing
    that.
    """
    return context


__all__ = [
    "background_principal",
    "for_tenant",
    "for_each_tenant",
    "JobContext",
    "MissingJobContext",
    "job_context",
    "preserve_for_retry",
    "default_workspace_job_principal",
    "authorise",
    "JobAuthorisationRevoked",
]
