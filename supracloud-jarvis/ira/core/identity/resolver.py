"""Resolve an authenticated caller into a :class:`Principal`.

This is the compatibility layer between the existing username-string world and
the Principal world. It reuses the CURRENT authorities rather than inventing a
second one:

  * owner-ness       → api.middleware.auth.is_owner (the .env admin account)
  * delegated role   → security.delegated.get_delegated_role
  * role → scopes    → security.roles.scopes_for

so a role change or revoke made through the existing access wizard is reflected
here immediately, and there is no parallel permission model to drift out of sync.

Private edition maps the owner onto one stable tenant and a deterministic user
id (see principal.py). Cloud edition raises: its identities come from the cloud
authentication system, which Phase 2 builds. Refusing is deliberate — silently
minting a private-shaped principal in a multi-tenant process is precisely the
bug class that produces cross-tenant reads.
"""
from __future__ import annotations

import logging
import uuid

from core.editions import EDITION_CLOUD, EDITION_PRIVATE, EditionName, current_edition
from core.identity.principal import (
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
)

logger = logging.getLogger("ira.identity")

#: Scopes the primary owner holds. The owner has every scope by definition — the
#: owner gate and the approval guardrail (not the scope set) are what constrain
#: what actually executes for them.
_OWNER_SCOPES: frozenset[str] = frozenset()


def _owner_scopes() -> frozenset[str]:
    global _OWNER_SCOPES
    if not _OWNER_SCOPES:
        from security.roles import SCOPES

        _OWNER_SCOPES = frozenset(SCOPES)
    return _OWNER_SCOPES


class CloudIdentityNotAvailable(RuntimeError):
    """Raised when a cloud identity cannot be resolved.

    Originally raised for *every* cloud resolution, because cloud identity did
    not exist. It now means the narrower and more useful thing: this particular
    caller could not be resolved to a principal — unknown, suspended, deleted,
    holding no membership that grants access, or naming a tenant they do not
    belong to.

    Still fail-closed and still loud. The one direction that must never happen
    is falling back to the private single-owner mapping, which would mint a
    principal carrying `PRIVATE_TENANT_ID` inside a multi-tenant process.
    """


class AmbiguousTenant(CloudIdentityNotAvailable):
    """The user holds more than one access-granting membership and named none.

    A subclass, so a caller that already fails closed on an unresolvable
    identity also fails closed here rather than letting it through a narrower
    `except`. It is separated because it is the one refusal with an obvious
    remedy — name the tenant — and a caller may want to say so.

    **This must never be resolved by choosing.** Picking the first membership,
    the oldest, or the one that happens to sort first is how a request lands in
    a tenant the user did not mean; `select_workspace` refuses on zero or more
    than one for the same reason, and this mirrors it deliberately.
    """


class TenantNotPermitted(CloudIdentityNotAvailable):
    """A tenant was named and the caller holds no access-granting membership in it.

    A subclass for the same fail-closed reason as :class:`AmbiguousTenant`, and
    separated for one narrow purpose: it is the case where the identity resolved
    perfectly and only the requested tenant did not hold, so an HTTP layer can
    answer 403 instead of 503. A refusal reported as an outage tells the user to
    wait for something that will never change.

    **It does not widen what the response reveals.** The message is unchanged and
    is identical whether the tenant exists, belongs to somebody else, or was
    invented — so this cannot be used to discover which tenants exist. What is
    deliberately NOT given its own type is the register being unreachable:
    `_resolve_cloud_principal` funnels that into the plain base class on purpose,
    because *"I could not tell"* must not be a different code path from *"no"*.
    """


async def resolve_principal(
    username: str,
    *,
    session_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    edition: EditionName | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Principal:
    """Build a Principal for an ALREADY-AUTHENTICATED subject.

    This function does not authenticate. It must only be called with a subject
    that `require_auth` (or an equivalent verified path) has returned — passing
    an unverified string here would mint a principal for an unauthenticated
    caller.

    In the private edition `username` is the owner's account name. In cloud it
    is the user's **verified, normalised email**, which is what
    `users_verified_email_key` makes unique — see `_resolve_cloud_principal`.

    `tenant_id` is only meaningful in cloud, and it is a REQUEST for a tenant,
    never an assertion of one: membership is verified against the database
    before it is honoured, so a forged value resolves to nothing.
    """
    ed = edition or current_edition()

    if ed == EDITION_CLOUD:
        return await _resolve_cloud_principal(
            username, session_id=session_id, device_id=device_id,
            tenant_id=tenant_id,
        )

    return await _resolve_private_principal(
        username, session_id=session_id, device_id=device_id
    )


async def _resolve_cloud_principal(
    subject: str,
    *,
    session_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Principal:
    """Resolve a cloud caller from `users` + `tenant_memberships`.

    The Phase 2 deliverable named in `PHASE_2_AUTH_INVENTORY.md` §2: until now
    this raised, and *"cloud does not serve authenticated traffic"*.

    ── What is authoritative, and what is merely asked for ─────────────────

    Everything comes from the database. `subject` selects a row; it does not
    describe one. The caller cannot supply a tenant, a role, a scope or an
    owner flag — `tenant_id` is checked against membership, and every other
    field is read, never accepted.

    ── The lookup ──────────────────────────────────────────────────────────

    1. The user, by verified normalised email. `users_verified_email_key` is
       UNIQUE on `(normalised_email) WHERE normalised_email IS NOT NULL AND
       email_verified_at IS NOT NULL AND deleted_at IS NULL`, so an unverified
       or deleted account cannot be resolved and two accounts cannot collide on
       one address. The predicate is repeated in the query rather than left to
       the index: an index makes a lookup fast, and a WHERE clause makes it
       correct.
    2. `status = 'active'`. Suspended and deleted users are refused here rather
       than left to a downstream check that may not exist.
    3. The memberships that grant access, read on a connection bound to this
       user and to NO tenant — see `identity_bootstrap_connection`.
       `membership_grants_access` is a GENERATED column
       (`status = 'active' AND revoked_at IS NULL`), so "does this membership
       grant access" is computed by the database and cannot be got wrong by
       re-implementing it here.
    4. The tenant. Named → membership required. Not named → exactly one
       membership is unambiguous and is used; zero or several refuse.

    ── `is_owner` is hard-false, and stays that way ────────────────────────

    A tenant's `primary_owner` is not the deployment owner. 3C-04, 3C-08 and
    P2-07 were each a version of that confusion, and `is_owner` is a field
    rather than a name comparison precisely so it cannot be reintroduced. It is
    passed as `False` explicitly below rather than left to the default, so
    deleting the line is a visible change rather than a silent one.
    """
    from core.identity.cloud_roles import cloud_scopes_for
    from core.identity.db_context import identity_bootstrap_connection

    if not subject or not subject.strip():
        raise CloudIdentityNotAvailable("no subject to resolve")

    from utils.db import acquire

    # `users` carries no RLS — it is the identity register, readable before any
    # tenant is known. That is what makes it usable as the bootstrap, and it is
    # also why the WHERE clause below has to be complete.
    #
    # `normalise_email()` is the DATABASE's function, and calling it here rather
    # than normalising in Python is deliberate. `users.normalised_email` is a
    # GENERATED column defined as `normalise_email(email)`, and that function
    # lowercases only the DOMAIN — RFC-correctly, since the local-part is
    # case-sensitive. A Python `.lower()` would therefore turn `Solo@x.com` into
    # a subject that matches no row, refusing a legitimate user; a Python
    # reimplementation would work until someone changed one of the two copies.
    # One definition, used by both the column and the lookup, cannot drift.
    try:
        async with acquire() as conn:
            user = await conn.fetchrow(
                """
                SELECT id, status, normalised_email
                  FROM users
                 WHERE normalised_email = normalise_email($1)
                   AND email_verified_at IS NOT NULL
                   AND deleted_at IS NULL
                """,
                subject.strip(),
            )
    except Exception as exc:  # noqa: BLE001 — an unreachable register is a refusal
        # Fail CLOSED, and in the type the caller already handles. Without this
        # an unavailable database escaped as a raw RuntimeError from the pool,
        # which `except CloudIdentityNotAvailable` does not catch — so a caller
        # written to refuse on an unresolvable identity would instead see an
        # unhandled error. "I could not tell" must never be a different code
        # path from "no", which is the P2-05 lesson one layer up.
        #
        # The exception TYPE only: a connection error's body can carry the DSN.
        logger.warning("cloud identity lookup failed: %s", type(exc).__name__)
        raise CloudIdentityNotAvailable(
            "the identity register could not be reached"
        ) from exc

    if user is None:
        # Deliberately does not distinguish "no such user" from "unverified" or
        # "deleted": the caller is unauthenticated-adjacent at this point and
        # the difference is an account-enumeration oracle.
        raise CloudIdentityNotAvailable("no resolvable cloud identity")
    if user["status"] != "active":
        raise CloudIdentityNotAvailable("this account is not active")

    user_id = user["id"]

    try:
        async with identity_bootstrap_connection(user_id) as conn:
            memberships = await conn.fetch(
                """
                SELECT tenant_id, role
                  FROM tenant_memberships
                 WHERE user_id = $1
                   AND membership_grants_access
                 ORDER BY tenant_id
                """,
                user_id,
            )
    except Exception as exc:  # noqa: BLE001 — same fail-closed reasoning
        logger.warning("cloud membership lookup failed: %s", type(exc).__name__)
        raise CloudIdentityNotAvailable(
            "this identity's memberships could not be read"
        ) from exc

    if not memberships:
        raise CloudIdentityNotAvailable(
            "this identity holds no membership that grants access"
        )

    if tenant_id is not None:
        chosen = next(
            (m for m in memberships if m["tenant_id"] == tenant_id), None
        )
        if chosen is None:
            # The forged-header case. Refused identically to "no such tenant",
            # so the response cannot be used to discover which tenants exist.
            # The TYPE is narrower than the message (see TenantNotPermitted):
            # it lets the HTTP layer answer 403 rather than 503 without the body
            # saying anything it did not say before.
            raise TenantNotPermitted(
                "no access-granting membership in the requested tenant"
            )
    elif len(memberships) == 1:
        chosen = memberships[0]
    else:
        raise AmbiguousTenant(
            f"this identity holds {len(memberships)} access-granting "
            f"memberships and named no tenant. Refusing to choose one: "
            f"picking a tenant on the user's behalf is how a request lands in "
            f"the wrong one."
        )

    role = chosen["role"]
    return Principal(
        user_id=user_id,
        tenant_id=chosen["tenant_id"],
        session_id=session_id or uuid.uuid4(),
        edition=EDITION_CLOUD,
        roles=frozenset({role}),
        scopes=cloud_scopes_for(role),
        device_id=device_id,
        # The stored normalised form, not the string the caller sent. What ends
        # up in logs and audit rows should be the identity the database knows.
        username=user["normalised_email"],
        # Explicit, not defaulted. See the docstring: a tenant owner is not the
        # deployment owner, and this is the line that says so.
        is_owner=False,
    )


async def _resolve_private_principal(
    username: str,
    *,
    session_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
) -> Principal:
    from api.middleware.auth import is_owner
    from security.roles import ROLE_PRIMARY_OWNER, scopes_for

    owner = is_owner(username)

    if owner:
        roles = frozenset({ROLE_PRIMARY_OWNER})
        scopes = _owner_scopes()
    else:
        role = await _delegated_role(username)
        # Fail closed: an account we cannot resolve a role for gets no scopes,
        # so every scope check against it denies.
        roles = frozenset({role}) if role else frozenset()
        scopes = scopes_for(role) if role else frozenset()

    return Principal(
        user_id=private_user_id(username),
        tenant_id=PRIVATE_TENANT_ID,
        session_id=session_id or uuid.uuid4(),
        edition=EDITION_PRIVATE,
        roles=roles,
        scopes=scopes,
        device_id=device_id,
        username=username,
        is_owner=owner,
    )


async def _delegated_role(username: str) -> str | None:
    """Look up a delegated account's role. Any failure → None (no scopes)."""
    try:
        from security.delegated import get_delegated_role

        return await get_delegated_role(username)
    except Exception as e:  # DB down, table missing, import error
        logger.warning(
            "Delegated role lookup failed for a non-owner principal — "
            "resolving with NO scopes (fail closed): %s", e,
        )
        return None


# ── FastAPI dependency ───────────────────────────────────────────────────────

async def require_principal(username: str) -> Principal:
    """FastAPI-friendly wrapper.

    Wire it as::

        from fastapi import Depends
        from api.middleware.auth import require_auth
        from core.identity import require_principal

        async def principal_dep(u: str = Depends(require_auth)) -> Principal:
            return await require_principal(u)

    `require_auth` is intentionally left returning a string: changing its return
    type would touch every route at once. New routes take the Principal; old
    routes migrate individually.
    """
    return await resolve_principal(username)


__all__ = [
    "resolve_principal",
    "require_principal",
    "CloudIdentityNotAvailable",
    "AmbiguousTenant",
    "TenantNotPermitted",
]
