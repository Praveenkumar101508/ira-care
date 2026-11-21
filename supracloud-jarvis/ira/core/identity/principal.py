"""The request Principal — who is asking, in which tenant, under which edition.

Today the codebase threads a bare username string from `require_auth` through
roughly 276 call sites, and answers "is this the owner?" by comparing it to
`Settings.ira_admin_username`. That works for exactly one deployment shape: one
box, one owner, identity supplied by the environment.

A `Principal` replaces that string. It carries the facts an authorisation
decision actually needs — user, tenant, session, roles, scopes, edition — as one
immutable value, resolved ONCE from authenticated state.

Two rules this type exists to enforce:

  1. **Tenant is never client-supplied.** A `Principal` is built from an
     authenticated token, never from a request body, query parameter or header.
     Phase 2's isolation tests assert a forged tenant identifier is ignored.
  2. **Owner-ness is a property of the principal, not of a string comparison
     against an environment variable.** `Principal.is_owner` is a field, so a
     cloud request cannot accidentally become the owner by matching a username.

Phase 1 introduces the type and the private-edition mapping. It does not rewrite
the 276 call sites: `username` is retained precisely so existing code can be
migrated incrementally behind adapters, and `require_auth` keeps returning a
string so nothing breaks in the meantime.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Optional

from core.editions import EDITION_PRIVATE, EditionName

# ── Stable identifiers for the single-owner private edition ──────────────────
#
# IRA Private has no tenant table and no user table — the owner is the .env
# admin account. Phase 2 backfills existing private data into ONE tenant, so the
# single-owner install becomes a one-tenant case of the general model rather
# than a permanent special case in the code.
#
# These UUIDs are derived deterministically (UUIDv5) from a fixed namespace, so
# they are the same on every machine and across restarts without being stored
# anywhere. Phase 2's migration can rely on that.

#: Namespace for all IRA-derived deterministic UUIDs. Do not change — the
#: private tenant/user ids are derived from it and Phase 2 migrates against them.
IRA_UUID_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")

PRIVATE_TENANT_ID = uuid.uuid5(IRA_UUID_NAMESPACE, "tenant:private")


def private_user_id(username: str) -> uuid.UUID:
    """Deterministic user id for a private-edition account name."""
    return uuid.uuid5(IRA_UUID_NAMESPACE, f"user:private:{username}")


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    Immutable by design: a request's identity must not be mutated part-way
    through handling it. Use :meth:`with_scopes` to derive a narrowed copy.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    edition: EditionName
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    device_id: Optional[uuid.UUID] = None

    # ── Workspace context (Phase 3B batch 5A) ────────────────────────────────
    #
    # Every field here is optional, and that is the design rather than a
    # concession. A principal with no active workspace is a legitimate state:
    # the user has authenticated and belongs to a tenant but has not selected a
    # workspace, and tenant-wide endpoints are perfectly valid in that state.
    #
    # The direction that must never be optional is the other one. A
    # workspace-scoped operation with `workspace_id is None` must FAIL, not pick
    # something — see `require_workspace()` below and `workspace_connection()`
    # in db_context. There is deliberately no "default workspace" fallback
    # anywhere in this dataclass: the fallback is what turns a missing context
    # into a silent write into somebody else's workspace.
    #
    # These are never set from a request. They are produced by
    # `core.identity.workspace.select_workspace()`, which verifies tenant
    # membership, workspace ownership and workspace membership against the
    # database before it will build them.

    #: The verified active workspace, or None if none has been selected.
    workspace_id: Optional[uuid.UUID] = None

    #: `tenant_memberships.id` — the row that proves tenant membership.
    membership_id: Optional[uuid.UUID] = None

    #: `workspace_memberships.id` — the row that proves workspace access.
    #: Named for the membership rather than the grant because a grant is
    #: currently expressed as a membership row; if grants ever become their own
    #: table this holds whichever identifier carried the authority.
    workspace_membership_id: Optional[uuid.UUID] = None

    #: The role held IN THE WORKSPACE — `viewer`, `member`, `workspace_admin`.
    #: Deliberately separate from `roles`, which are tenant-level. A tenant
    #: owner is not automatically a workspace administrator; see
    #: `workspace_role_capabilities` and CLOUD-ADR-0016.
    workspace_role: Optional[str] = None

    #: How strongly this session was authenticated — `password`, `mfa`,
    #: `biometric`, `token`. Recorded so a later phase can require step-up for
    #: destructive workspace actions without inventing the field then.
    authentication_strength: str = ""

    #: The account name this principal was resolved from.
    #:
    #: NOT in the target shape from the architecture brief, and deliberately so:
    #: it is the bridge that lets ~276 username-based call sites migrate one at a
    #: time instead of in a single unreviewable diff. It is an ADAPTER FIELD.
    #: New authorisation logic must branch on roles/scopes/is_owner — never on
    #: this string. Phase 2 keeps it; a later phase may retire it.
    username: str = ""

    #: Whether this principal is the deployment's primary owner. A field rather
    #: than a computed comparison against Settings.ira_admin_username, so cloud
    #: principals can never become the owner by name collision.
    is_owner: bool = False

    def __post_init__(self) -> None:
        # Fail closed on a principal that could not name its tenant. A blank
        # tenant is the precondition for a cross-tenant read.
        if self.tenant_id is None:
            raise ValueError("Principal requires a tenant_id — refusing to build one without it")
        if self.user_id is None:
            raise ValueError("Principal requires a user_id")

    # ── Derivation ───────────────────────────────────────────────────────────

    def with_scopes(self, scopes: frozenset[str] | set[str]) -> "Principal":
        """A copy narrowed to `scopes`. Cannot widen beyond what it already has
        unless the principal is unscoped (empty = 'not yet resolved')."""
        requested = frozenset(scopes)
        if self.scopes:
            requested = requested & self.scopes
        return replace(self, scopes=requested)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_role(self, role: str) -> bool:
        return role in self.roles

    # ── Workspace ────────────────────────────────────────────────────────────

    @property
    def has_workspace(self) -> bool:
        """Whether an active workspace has been verified and selected."""
        return self.workspace_id is not None

    def require_workspace(self) -> uuid.UUID:
        """The active workspace id, or raise.

        The only supported way to read `workspace_id` from a workspace-scoped
        code path. Reading the attribute directly invites
        `principal.workspace_id or default` — one short line that turns every
        unscoped request into a write into the default workspace, which is the
        exact failure Batch 5A exists to make impossible.
        """
        if self.workspace_id is None:
            from core.identity.db_context import MissingWorkspaceContext

            raise MissingWorkspaceContext(
                f"Principal {self.user_id} has no active workspace. This "
                f"operation is workspace-scoped and there is no default: a "
                f"workspace must be selected through "
                f"core.identity.workspace.select_workspace(), which verifies "
                f"membership before it will grant one."
            )
        return self.workspace_id

    def without_workspace(self) -> "Principal":
        """A copy with the workspace context cleared.

        For dropping from a workspace-scoped context to a tenant-wide one —
        narrowing, never widening. There is no `with_workspace()` counterpart on
        purpose: widening is the selection service's job, because widening
        requires database verification and a dataclass method cannot do that.
        """
        return replace(
            self,
            workspace_id=None,
            workspace_membership_id=None,
            workspace_role=None,
        )

    # ── Logging ──────────────────────────────────────────────────────────────

    def safe_repr(self) -> str:
        """Identifiers only — never the username, which is personal data."""
        return (
            f"Principal(user={self.user_id}, tenant={self.tenant_id}, "
            f"workspace={self.workspace_id}, workspace_role={self.workspace_role}, "
            f"edition={self.edition}, owner={self.is_owner})"
        )


def system_principal(
    *,
    edition: EditionName = EDITION_PRIVATE,
    tenant_id: uuid.UUID | None = None,
    reason: str = "system",
) -> Principal:
    """A principal for work with no human caller — schedulers, migrations, workers.

    Explicitly NOT the owner: background work must not inherit owner privilege
    just because nobody is logged in. It carries no roles and no scopes, so any
    scope check against it denies. Callers that legitimately need privilege must
    grant it deliberately and visibly via :meth:`Principal.with_scopes`.
    """
    return Principal(
        user_id=uuid.uuid5(IRA_UUID_NAMESPACE, f"system:{reason}"),
        tenant_id=tenant_id or PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(),
        edition=edition,
        roles=frozenset(),
        scopes=frozenset(),
        username="",
        is_owner=False,
    )


__all__ = [
    "Principal",
    "PRIVATE_TENANT_ID",
    "IRA_UUID_NAMESPACE",
    "private_user_id",
    "system_principal",
]
