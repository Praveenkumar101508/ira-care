"""Tenant invitations and the membership lifecycle (Phase 2 batch 5).

Batch 4 founds a tenant with exactly one member and no way to add a second.
This is how an organisation gets the rest of its people.

    an authorised member creates an invitation
      → an opaque 256-bit token, stored only as SHA-256
      → the invitee authenticates as themselves
      → the server validates tenant, identity, expiry and prior use
      → membership + default workspace membership, in one transaction
      → the token is consumed atomically

── The invitation names the tenant; the invitee never does ─────────────────

`invitation_connection` binds `app.tenant_id` from the invitation row that the
presented token selects. So "which organisation am I joining" is answered by
possession of a secret that an authorised member sent to a specific address —
never by a field in the request. There is no parameter here that names a tenant.

── One conditional UPDATE decides acceptance ───────────────────────────────

Expiry, prior consumption, revocation and identity match are all predicates on
the single UPDATE that consumes the invitation. Checking them and then writing
would leave a window in which two presentations of the same link both pass the
check — and `verify_email` records the same reasoning for the same reason.

The identity predicate is the one worth naming: the accepting account's stored
`normalised_email` must equal the invitation's. A link that reaches the wrong
mailbox, or is forwarded, or is pasted into a chat, is worthless to anybody but
the person it was addressed to.

── Authority to invite comes from the database, every time ─────────────────

`INVITE_CAPABLE_ROLES` is the allow-list, checked against the inviter's ACTIVE
membership read under their own tenant binding. The role a caller claims is
never consulted; there is no parameter for it. And the role an invitation may
GRANT is bounded twice — by `ROLE_GRANT_LIMITS` here, so an admin cannot invite
an owner, and by 055's CHECK constraint, so no invitation can name
`primary_owner` at all.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("ira.identity.invitations")

#: Roles whose ACTIVE membership may issue an invitation. Deliberately not
#: every role: `member`, `viewer` and `billing_admin` may not grow the tenant.
#: `security_admin` is excluded too — it is a tenant-security role in name only
#: until the surfaces it administers exist (see cloud_roles.py), and granting it
#: membership control now would be inventing a capability rather than mapping
#: one.
INVITE_CAPABLE_ROLES: frozenset[str] = frozenset({
    "primary_owner", "owner", "admin",
})

#: What each inviting role may GRANT. An admin cannot mint an owner: privilege
#: must not be creatable sideways by somebody who does not already hold it.
#: `primary_owner` appears in no value — 055's CHECK refuses it in the column,
#: and this refuses it before the write is attempted so the failure is a clear
#: refusal rather than a constraint violation at redemption time.
ROLE_GRANT_LIMITS: dict[str, frozenset[str]] = {
    "primary_owner": frozenset({"owner", "admin", "member", "viewer",
                                "billing_admin", "security_admin"}),
    "owner": frozenset({"owner", "admin", "member", "viewer",
                        "billing_admin", "security_admin"}),
    "admin": frozenset({"member", "viewer", "billing_admin"}),
}

DEFAULT_INVITATION_TTL_HOURS = 168  # seven days

#: The workspace role an accepted invitation grants in the tenant's default
#: workspace. `member`, never `workspace_admin`: joining an organisation is not
#: the same as being trusted to administer its workspace.
JOINING_WORKSPACE_ROLE = "member"


class InvitationError(RuntimeError):
    """Safe to show to the caller."""


class InvitationRefused(InvitationError):
    """This invitation cannot be issued, or cannot be accepted.

    One type for every acceptance failure — unknown token, expired, already
    consumed, revoked, or issued to a different address. The holder of a stolen
    link must not be able to tell which, because "already used" confirms the
    address is real and the organisation exists.
    """


class InvitationUnavailable(InvitationError):
    """The database could not be reached. Fail closed."""


@dataclass(frozen=True, slots=True)
class Invitation:
    """An issued invitation. `token` is present exactly once, at creation."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    #: In the clear, only ever returned from `create_invitation`, only for the
    #: mail path. Never stored, never logged, never returned again.
    token: Optional[str] = None


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    workspace_id: Optional[uuid.UUID]


def _hash_token(token: str) -> str:
    """SHA-256 hex — the same choice and the same reasoning as `registration`.

    A 256-bit random token is not guessable, so the only property needed is
    that the stored form cannot be presented. Acceptance happens on a path
    where a deliberately slow hash would be a denial-of-service lever.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _inviter_membership(conn, principal) -> str:
    """The inviter's ACTIVE role, read from the database under their binding.

    `membership_grants_access` is the generated column, so "is this membership
    active" is the database's answer rather than a re-implementation of it.
    """
    role = await conn.fetchval(
        """
        SELECT role FROM tenant_memberships
         WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           AND user_id  = $1
           AND membership_grants_access
        """,
        principal.user_id,
    )
    if role is None:
        raise InvitationRefused("no active membership in this tenant")
    return role


async def create_invitation(
    principal,
    email: str,
    role: str = "member",
    *,
    ttl_hours: int = DEFAULT_INVITATION_TTL_HOURS,
) -> Invitation:
    """Invite `email` into `principal`'s tenant with `role`.

    The tenant is `principal.tenant_id` and there is no parameter for it —
    an authenticated member invites into the organisation they are acting in,
    which is the only one their connection can write to anyway.

    `role` is checked against the inviter's ACTIVE database role twice: they
    must hold a role that may invite at all, and the role they are granting
    must be within what that role may grant.
    """
    from core.identity.db_context import tenant_connection

    address = (email or "").strip()
    if not address or "@" not in address:
        raise InvitationError("a valid email address is required")
    if role not in ROLE_GRANT_LIMITS.get("primary_owner", frozenset()):
        # The widest grant set doubles as the list of invitable roles. A role
        # outside it is refused here rather than at the CHECK constraint, so
        # the caller gets a refusal instead of a database error.
        raise InvitationError(f"{role!r} is not an invitable role")

    ttl = max(1, min(int(ttl_hours or DEFAULT_INVITATION_TTL_HOURS), 24 * 30))
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl)

    try:
        async with tenant_connection(principal) as conn:
            inviter_role = await _inviter_membership(conn, principal)

            if inviter_role not in INVITE_CAPABLE_ROLES:
                raise InvitationRefused(
                    "this role may not invite members to the organisation"
                )
            if role not in ROLE_GRANT_LIMITS.get(inviter_role, frozenset()):
                raise InvitationRefused(
                    "this role may not grant that level of access"
                )

            # An existing ACTIVE member does not need an invitation, and
            # issuing one would produce a link that can never be redeemed —
            # `tenant_memberships_tenant_id_user_id_key` refuses the second
            # membership. Refusing now makes that a clear answer rather than a
            # mystery a week later.
            already = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tenant_memberships m
                      JOIN users u ON u.id = m.user_id
                     WHERE m.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                       AND u.normalised_email = normalise_email($1)
                       AND m.membership_grants_access
                )
                """,
                address,
            )
            if already:
                raise InvitationRefused("that address is already a member")

            # No tenant_id column: it DEFAULTs to the bound GUC, so the row's
            # tenant and the policy's tenant are one expression.
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_invitations
                       (email, role, token_hash, invited_by, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, tenant_id, email, role, expires_at
                """,
                address, role, token_hash, principal.user_id, expires_at,
            )
    except InvitationError:
        raise
    except Exception as exc:  # noqa: BLE001
        # A unique violation here is the partial index refusing a second OPEN
        # invitation for the same address. Reported as a refusal, and the type
        # only — never the exception body, which can carry the DSN.
        logger.warning("invitation could not be issued: %s", type(exc).__name__)
        raise InvitationUnavailable("the invitation could not be issued") from exc

    # The token is NOT logged. It is a bearer credential for admission to an
    # organisation.
    logger.info("issued a tenant invitation")
    return Invitation(
        id=row["id"], tenant_id=row["tenant_id"], email=row["email"],
        role=row["role"], expires_at=row["expires_at"], token=token,
    )


async def accept_invitation(token: str, accepting_user_id: uuid.UUID) -> AcceptedInvitation:
    """Redeem `token` as `accepting_user_id`, creating the membership.

    `accepting_user_id` must come from an authenticated session — this function
    does not authenticate, exactly as `resolve_principal` does not.

    Everything happens in one transaction: the consuming UPDATE, the tenant
    membership and the default workspace membership. A consumed invitation with
    no membership would be unrecoverable, since the token is single-use.
    """
    from core.identity.db_context import (
        MissingTenantContext, invitation_connection,
    )

    if not token or not token.strip():
        raise InvitationRefused("this invitation link is not valid")
    if accepting_user_id is None:
        raise InvitationRefused("this invitation link is not valid")

    token_hash = _hash_token(token.strip())

    try:
        async with invitation_connection(token_hash, accepting_user_id) as conn:
            # ONE conditional UPDATE. Every reason an invitation may not be
            # redeemed is a predicate on it, so two simultaneous presentations
            # of the same link cannot both succeed — the second updates zero
            # rows and is refused.
            #
            # The identity predicate joins `users` on the ACCEPTING id and
            # compares stored normalised addresses. The accepting account must
            # also be verified and live: an unverified address is not proof of
            # anything, and 034 lets several unverified rows share one address.
            row = await conn.fetchrow(
                """
                UPDATE tenant_invitations i
                   SET accepted_at = NOW(), accepted_by = $2
                  FROM users u
                 WHERE i.token_hash        = $1
                   AND i.invitation_is_open
                   AND i.expires_at        > NOW()
                   AND u.id                = $2
                   AND u.deleted_at        IS NULL
                   AND u.status            = 'active'
                   AND u.email_verified_at IS NOT NULL
                   AND u.normalised_email  = i.normalised_email
                RETURNING i.tenant_id, i.role
                """,
                token_hash, accepting_user_id,
            )
            if row is None:
                # Unknown, expired, already consumed, revoked, wrong identity,
                # or an unverified/suspended accepting account — one answer.
                raise InvitationRefused("this invitation link is not valid")

            role = row["role"]

            # The membership. `tenant_id` is read back from the bound GUC
            # rather than taken from the row, so the value written and the
            # value the policy checks are the same expression.
            #
            # ON CONFLICT handles the case where a membership row already
            # exists in a non-granting state — `invited`, `suspended` or
            # `removed` — which is how re-inviting somebody who was removed
            # works. It does NOT silently re-grant an active membership,
            # because `create_invitation` refuses to issue one for an existing
            # active member and the conditional UPDATE above has already
            # consumed a single-use token.
            await conn.execute(
                """
                INSERT INTO tenant_memberships
                       (tenant_id, user_id, role, status, joined_at, invited_by)
                VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                        $1, $2, 'active', NOW(),
                        (SELECT invited_by FROM tenant_invitations
                          WHERE token_hash = $3))
                ON CONFLICT (tenant_id, user_id) DO UPDATE
                   SET status     = 'active',
                       role       = EXCLUDED.role,
                       joined_at  = COALESCE(tenant_memberships.joined_at, NOW()),
                       revoked_at = NULL,
                       removed_at = NULL,
                       updated_at = NOW()
                """,
                accepting_user_id, role, token_hash,
            )

            # The default workspace. Without it the new member holds a tenant
            # membership and `select_workspace` still refuses them, which is an
            # account that looks joined and can do nothing — the failure mode
            # this batch's brief names explicitly.
            workspace_id = await conn.fetchval(
                """
                SELECT id FROM workspaces
                 WHERE tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                   AND is_default AND status = 'active'
                """
            )
            if workspace_id is None:
                # A tenant with no default workspace is a batch-4 invariant
                # violation. Refusing rolls the whole acceptance back rather
                # than admitting somebody to an organisation they cannot use.
                raise InvitationRefused("this organisation is not ready for members")

            await conn.execute(
                """
                INSERT INTO workspace_memberships
                       (tenant_id, workspace_id, user_id, role, status)
                VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                        $1, $2, $3, 'active')
                ON CONFLICT (workspace_id, user_id) DO UPDATE
                   SET status = 'active', removed_at = NULL, updated_at = NOW()
                """,
                workspace_id, accepting_user_id, JOINING_WORKSPACE_ROLE,
            )

            tenant_id = row["tenant_id"]
    except InvitationError:
        raise
    except MissingTenantContext as exc:
        # No invitation matches that token. Same answer as every other
        # acceptance failure.
        raise InvitationRefused("this invitation link is not valid") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("invitation acceptance failed: %s", type(exc).__name__)
        raise InvitationUnavailable(
            "the invitation could not be accepted"
        ) from exc

    logger.info("an invitation was accepted")
    return AcceptedInvitation(
        tenant_id=tenant_id, user_id=accepting_user_id,
        role=role, workspace_id=workspace_id,
    )


async def revoke_invitation(principal, invitation_id: uuid.UUID) -> bool:
    """Withdraw an open invitation. Returns False if there was nothing to do."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        inviter_role = await _inviter_membership(conn, principal)
        if inviter_role not in INVITE_CAPABLE_ROLES:
            raise InvitationRefused(
                "this role may not manage invitations"
            )
        # Tenant-scoped by policy as well as by predicate: another tenant's
        # invitation is not visible on this connection at all.
        updated = await conn.fetchval(
            """
            UPDATE tenant_invitations
               SET revoked_at = NOW(), revoked_by = $2
             WHERE id = $1 AND invitation_is_open
            RETURNING id
            """,
            invitation_id, principal.user_id,
        )
    return updated is not None


async def list_invitations(principal) -> list[dict]:
    """Open invitations for the caller's tenant. Never includes a token hash.

    Returning the hash would make a read of this list equivalent to holding the
    links — the stored form is not presentable, but it is the lookup key, and
    handing it out invites a caller to try.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        await _inviter_membership(conn, principal)
        rows = await conn.fetch(
            """
            SELECT id, email, role, expires_at, created_at
              FROM tenant_invitations
             WHERE invitation_is_open
             ORDER BY created_at DESC
            """
        )
    return [dict(r) for r in rows]


__all__ = [
    "AcceptedInvitation",
    "DEFAULT_INVITATION_TTL_HOURS",
    "INVITE_CAPABLE_ROLES",
    "Invitation",
    "InvitationError",
    "InvitationRefused",
    "InvitationUnavailable",
    "JOINING_WORKSPACE_ROLE",
    "ROLE_GRANT_LIMITS",
    "accept_invitation",
    "create_invitation",
    "list_invitations",
    "revoke_invitation",
]
