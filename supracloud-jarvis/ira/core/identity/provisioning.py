"""Give the private edition's configured owner the identity rows it needs.

`023_tenancy.sql` seeds a `users` row, a `tenant_memberships` row and (via
`035_memberships.sql`) a `workspace_memberships` row for **one** hard-coded
UUID: `private_user_id("admin")`. Its own comment says what happens otherwise —

    Real deployments with a different IRA_ADMIN_USERNAME get their row from
    the application on first login; this seeds the common case.

— and that application-side path was never written. This module is it.

── Why it matters now and did not before ───────────────────────────────────

Nothing used to read those rows, so a deployment with `IRA_ADMIN_USERNAME=ira`
had a principal with no membership and never noticed. Batch 5C changes that:
`legacy_owner_workspace()` verifies tenant membership, workspace membership and
both statuses before a route may touch content. Without these rows, every
workspace-scoped endpoint would answer 403 on such a deployment — correctly,
which is the problem. The verification is right; the missing rows are the bug,
and the fix is to create them rather than to weaken the check.

── What it deliberately does not do ────────────────────────────────────────

* It does not run in the cloud edition. There is no ".env owner" in a
  multi-tenant process, and minting one would be the private-tenant fallback
  that every other module here refuses.
* It does not create a tenant, an organisation or a workspace. Those come from
  the migrations; if the default workspace is absent, that is a schema problem
  and this reports it rather than papering over it.
* It does not touch an existing row's role or status. A membership an operator
  suspended stays suspended — `ON CONFLICT DO NOTHING`, never `DO UPDATE`.
  Re-granting access on restart would make suspension mean nothing.
* It does not fail startup, with one exception below. A read-only replica, a
  permission difference or a schema older than `039` should not stop the
  process from serving the endpoints that do not need a workspace. It logs at
  ERROR and the affected endpoints refuse individually, which is the
  fail-closed direction.

── The exception: an owner login that is a reserved memory name ────────────

Chat files the owner's messages under their login. `owner`, `system`,
`self_knowledge`, `meeting` and `technical_library` are namespaces IRA keeps
for its own memory (`memory/namespaces.py`), and deletion and retention never
touch them, so that the Memory Vault (`owner`) can never be deleted as somebody's
own. An owner whose login is one of those names would find that "delete my
conversations" keeps everything chat remembered from them. Delegated usernames
are already refused for the same reason (`security/delegated.py`).

* **A new install** (no account yet for the configured login) refuses to start
  with `ReservedOwnerLogin`, before anything is written. Choosing another login
  costs nothing at that point.
* **An install that already has an account under that login** keeps starting
  and logs an ERROR. Refusing would lock the owner out, and changing the login
  would change the account id (`private_user_id`) and separate it from its
  conversations, voice profile and second factor. Erasure counts the kept rows
  instead of reporting success (`core/privacy/erasure.py::erase_vectors`).
  Moving such an install to a new login is a data migration nobody has
  approved; `docs/security/RESERVED_OWNER_LOGIN_2026-10-19.md` records it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger("ira.identity.provisioning")


class ReservedOwnerLogin(RuntimeError):
    """The owner's login is a reserved memory name and no account exists for it yet.

    Raised before anything is written; `app/lifespan.py` lets it stop startup.
    """


@dataclass(frozen=True, slots=True)
class OwnerProvisioning:
    """What was found and what was created. Identifiers and booleans only."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    created_user: bool
    created_tenant_membership: bool
    created_workspace_membership: bool

    def summary(self) -> str:
        made = [
            name
            for name, done in (
                ("user", self.created_user),
                ("tenant membership", self.created_tenant_membership),
                ("workspace membership", self.created_workspace_membership),
            )
            if done
        ]
        what = ", ".join(made) if made else "nothing (already present)"
        return (
            f"private owner identity: created {what} "
            f"(user={self.user_id} workspace={self.workspace_id})"
        )


async def ensure_private_owner_identity() -> OwnerProvisioning | None:
    """Idempotently give the configured private owner their identity rows.

    Returns None in the cloud edition. Raises `ReservedOwnerLogin` on a new
    install whose owner login is a reserved memory name, and nothing else the
    caller must handle; see the module docstring.
    """
    from core.editions import EDITION_PRIVATE, current_edition

    if current_edition() != EDITION_PRIVATE:
        logger.debug("owner provisioning skipped: not the private edition")
        return None

    from config import get_settings
    from core.identity.db_context import owner_connection
    from core.identity.principal import PRIVATE_TENANT_ID, private_user_id

    username = get_settings().ira_admin_username
    user_id = private_user_id(username)

    # `owner_connection` binds the private tenant, which is what the
    # `tenant_scoped_insert` policies on both membership tables check. It sets
    # no `app.user_id`, and none is needed: those policies predicate on the
    # tenant alone. Running provisioning through the same bound path the
    # application uses means it cannot succeed against a boundary the requests
    # will then fail — if the INSERT is refused here, it would have been
    # refused there too, and that is worth learning at startup.
    async with owner_connection("core/identity/provisioning.py") as conn:
        await _check_login_is_not_reserved(conn, user_id, username)
        created_user = await _ensure_user(conn, user_id, username)
        created_membership = await _ensure_tenant_membership(conn, user_id)
        workspace_id = await conn.fetchval(
            "SELECT id FROM workspaces "
            " WHERE tenant_id = $1 AND is_default AND status = 'active'",
            PRIVATE_TENANT_ID,
        )
        if workspace_id is None:
            logger.error(
                "private tenant %s has no active default workspace — "
                "workspace-scoped endpoints will refuse. Expected from "
                "039_workspace_substrate.sql; check the migration ledger.",
                PRIVATE_TENANT_ID,
            )
            created_workspace_membership = False
        else:
            created_workspace_membership = await _ensure_workspace_membership(
                conn, user_id, workspace_id
            )

    result = OwnerProvisioning(
        user_id=user_id,
        tenant_id=PRIVATE_TENANT_ID,
        workspace_id=workspace_id,
        created_user=created_user,
        created_tenant_membership=created_membership,
        created_workspace_membership=created_workspace_membership,
    )
    logger.info(result.summary())
    return result


async def _check_login_is_not_reserved(conn, user_id: uuid.UUID, username: str) -> None:
    """Refuse a reserved owner login on a new install; warn on an existing one.

    "New" means no `users` row for this login's id, which is what
    `_ensure_user` would otherwise create. `users` has no RLS, so the bound
    connection reads it.
    """
    from memory.namespaces import RESERVED_NAMESPACES, is_reserved_namespace

    if not is_reserved_namespace(username):
        return
    names = ", ".join(sorted(RESERVED_NAMESPACES))
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE id = $1)", user_id)
    if not exists:
        raise ReservedOwnerLogin(
            f"IRA_ADMIN_USERNAME is {username!r}, a name IRA reserves for its own "
            f"memory ({names}). Chat would file your conversations under it, where "
            f"deleting them cannot reach. Set another owner login in .env and "
            f"start IRA again. Nothing was created."
        )
    logger.error(
        "IRA_ADMIN_USERNAME is %r, a name IRA reserves for its own memory (%s). "
        "This install already has an owner account under it, so IRA starts, but "
        "chat memories filed under this login are not removed when conversations "
        "are deleted or by retention, and erasure reports them as not deleted. "
        "Changing the login would separate the account from its existing "
        "conversations; see docs/security/RESERVED_OWNER_LOGIN_2026-10-19.md.",
        username, names,
    )


async def _ensure_user(conn, user_id: uuid.UUID, username: str) -> bool:
    """The `users` row. `external_subject` is the .env account name.

    Not the email column: an account name is not an email address, and putting
    it in one would collide with the `users_email_key` unique index the moment
    a real address arrives.
    """
    row = await conn.fetchrow(
        """INSERT INTO users (id, display_name, auth_provider, external_subject)
           VALUES ($1, 'Owner', 'password', $2)
           ON CONFLICT (id) DO NOTHING
           RETURNING id""",
        user_id,
        username,
    )
    return row is not None


async def _ensure_tenant_membership(conn, user_id: uuid.UUID) -> bool:
    """Ownership of the private tenant — primary if that seat is free.

    `035_memberships.sql` enforces `tenant_memberships_one_primary_owner`: at
    most one membership per tenant may be `primary_owner` among those that
    actually grant access. `023` already seeded that seat for
    `private_user_id("admin")`, so on a deployment that renamed the account the
    seat is taken by a row nobody can log in as.

    Rather than fight that — deleting or demoting a seeded row on every boot is
    exactly the kind of self-healing that quietly undoes an operator's decision
    — the configured account takes `owner` when the seat is occupied and
    `primary_owner` when it is free. Nothing in the access path distinguishes
    them: `select_workspace` reads `membership_grants_access`, and the
    workspace capabilities come from the workspace role. The distinction
    matters to tenant administration, which is Phase 2's problem and should be
    an explicit transfer rather than a side effect of a rename.

    The role is decided in SQL against the same bound transaction that does the
    INSERT, so there is no window in which a concurrent boot could read "free"
    and then insert into a seat that has since been taken — the unique index
    would refuse the second one, which is the correct outcome.

    `tenant_id` likewise comes from the transaction's own GUC rather than from
    Python, so the value stored and the value `tenant_scoped_insert` checks are
    read from the same place and cannot disagree.
    """
    row = await conn.fetchrow(
        """INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at)
           SELECT t.id, $1,
                  CASE WHEN EXISTS (SELECT 1 FROM tenant_memberships m
                                     WHERE m.tenant_id = t.id
                                       AND m.role = 'primary_owner'
                                       AND m.membership_grants_access)
                       THEN 'owner' ELSE 'primary_owner' END,
                  'active', NOW()
             FROM (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid AS id) t
           ON CONFLICT (tenant_id, user_id) DO NOTHING
           RETURNING id""",
        user_id,
    )
    return row is not None


async def _ensure_workspace_membership(
    conn, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """Administration of the default workspace.

    `workspace_admin` rather than `member`, because the private owner is the
    only account there is and the destructive endpoints gate on
    `workspace.delete_content`, which only that role carries.
    """
    row = await conn.fetchrow(
        """INSERT INTO workspace_memberships
                (tenant_id, workspace_id, user_id, role, status)
           VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                   $1, $2, 'workspace_admin', 'active')
           ON CONFLICT (workspace_id, user_id) DO NOTHING
           RETURNING id""",
        workspace_id,
        user_id,
    )
    return row is not None


__all__ = ["ensure_private_owner_identity", "OwnerProvisioning"]
