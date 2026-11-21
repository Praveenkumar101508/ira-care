"""security/delegated.py — delegated user accounts + audit log (PR #67).

Backs the secure access wizard. Hard rules enforced at this layer:

  - Creating/changing/revoking access ALWAYS requires the owner's password,
    re-verified here with NO DEV_MODE bypass (verify_owner_password) — a live
    session token, or words spoken near a microphone, are never enough.
  - Every new person gets their OWN account with their OWN bcrypt-hashed
    password. The owner's credentials are never shared or returned.
  - The primary owner is the .env admin account. It is never stored in
    delegated_users, never grantable, and no function here can touch it.
  - Every grant / revoke / role change / denied attempt lands in
    access_audit_log.
  - No account may carry the name of a reserved memory namespace
    (memory/namespaces.py). Chat stores and recalls memory under the signed-in
    username, so an account named ``owner`` would read the Memory Vault as its
    own chat history. New names are refused; an account created before this
    rule existed is refused at login, resolves to no role, and is flagged in
    the listing so the owner can revoke it. It is never renamed silently.
  - Nor may an account carry, in any case, the owner's login or the voice
    service's name (`reserved_identity`). Chat believes `is_voice_owner` from
    the request body for the voice service's name alone, so an account with
    that name could claim owner authority. Such an account is refused and
    flagged exactly as above.

Where an account lives. `040_membership_grants.sql` closed `delegated_users` to
new rows ("Create a user and a tenant_membership instead"), and this module kept
inserting into it, so every grant on a migrated database failed. A grant now
writes a `users` row at `private_user_id(username)`, the account's own
`user_credentials` row, and a `tenant_memberships` row in the private tenant
carrying the private role (`092`). Rows that predate 040 stay in
`delegated_users` and are still read, revoked and re-roled.

Both models are tenant-scoped under FORCE ROW LEVEL SECURITY, so every query
runs on a connection bound to the private tenant. They ran on a bare
`acquire()`, which binds nothing, so legacy accounts matched zero rows: they
could not log in, held no role and were missing from the owner's list.
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from config import get_settings
from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
from memory.namespaces import RESERVED_NAMESPACES, is_reserved_namespace
from security.roles import GRANTABLE_ROLES, ROLE_VIEWER
from utils.db import acquire

logger = logging.getLogger("ira.delegated")

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]{2,31}$")
_MIN_PASSWORD_LEN = 8


# ── Owner password re-verification (never bypassed) ─────────────────────────

def verify_owner_password(password: str) -> bool:
    """Constant-time check of the OWNER's password for security-sensitive flows.

    Deliberately ignores DEV_MODE: granting access is a security event even on
    a dev box, and a voice/UI session must never stand in for the password.
    """
    from api.middleware.auth import verify_password, _admin_password_hash

    if not password:
        return False
    try:
        return verify_password(password, _admin_password_hash())
    except Exception:  # unknown hash error → fail closed
        return False


def is_primary_owner_username(username: str) -> bool:
    """The primary owner is exactly the configured admin account."""
    try:
        return (username or "") == get_settings().ira_admin_username
    except Exception:
        return False


def reserved_identity(username: str) -> Optional[str]:
    """The deployment identity `username` names, ignoring case, or None.

    Two settings name identities that are not household accounts:

      * ``ira_admin_username``, the primary owner. `is_primary_owner_username`
        compares exactly, and account names are stored lower-cased, so beside
        an owner called ``Praveen`` an account could take ``praveen``.
      * ``ira_voice_service_username``, the voice service. `api/routes/chat.py`
        believes the request body's ``is_voice_owner`` for this name alone
        (`api.middleware.auth.is_voice_service`), so an account holding it
        could claim owner tool authority in its own chat requests.

    **Used only to refuse:** at the grant, at login and in the role lookup.
    `tests/tenancy/test_3c04_owner_authority.py` pins its callers. If the
    settings cannot be read, every name counts as reserved.
    """
    name = (username or "").strip().casefold()
    if not name:
        return None
    try:
        cfg = get_settings()
        identities = (
            (cfg.ira_admin_username, "the primary owner"),
            (cfg.ira_voice_service_username, "the voice service"),
        )
    except Exception:
        return "a deployment identity (the settings could not be read)"
    for configured, label in identities:
        if (configured or "").strip().casefold() == name:
            return label
    return None


def _holds_reserved_name(username: str) -> bool:
    """A reserved memory namespace, or a deployment identity."""
    return is_reserved_namespace(username) or reserved_identity(username) is not None


# ── Validation helpers ───────────────────────────────────────────────────────

def validate_new_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "username must be 3-32 chars: lowercase letters, digits, '.', '-' or '_'"
        )
    if is_primary_owner_username(username):
        raise ValueError("that username belongs to the primary owner")
    identity = reserved_identity(username)
    if identity is not None:
        raise ValueError(f"that username belongs to {identity} — choose another")
    if is_reserved_namespace(username):
        raise ValueError(
            "that username is reserved for IRA's own memory "
            f"({', '.join(sorted(RESERVED_NAMESPACES))}) — choose another"
        )
    return username


def validate_new_password(password: str) -> str:
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"the new user's password must be at least {_MIN_PASSWORD_LEN} characters")
    return password


def validate_grantable_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in GRANTABLE_ROLES:
        raise ValueError(
            f"role must be one of {', '.join(GRANTABLE_ROLES)} — primary_owner is never grantable"
        )
    return role


# ── Account store ────────────────────────────────────────────────────────────
#
# Two places an account can live, read in a fixed order:
#
#   1. the membership model — `users`, `user_credentials` and a
#      `tenant_memberships` row in the private tenant whose `private_role`
#      (092) is set. Every account granted since 092 lives here.
#   2. `delegated_users` — rows that predate 040. Read, revoked and re-roled;
#      never inserted into.
#
# A membership account is authoritative for its name. The legacy table is
# consulted only when the name has no membership account, so a revoked
# membership account can never come back through a stale legacy row. Granting
# refuses a name present in either place, so no name is in both.


class AccountExists(ValueError):
    """The name is already an account (or an identity) and cannot be granted."""


def _tenant_role_for(role: str) -> str:
    """The tenant role a private account holds, from its private role.

    Never above `member`: a delegated account is not a tenant administrator.
    `092` enforces the same mapping with a CHECK, and `035` used it for the
    legacy backfill.
    """
    return "viewer" if role == ROLE_VIEWER else "member"


def _private_edition() -> bool:
    from core.editions import EDITION_PRIVATE, current_edition

    return current_edition() == EDITION_PRIVATE


@asynccontextmanager
async def _account_bound(user_id: uuid.UUID):
    """A transaction bound to the private tenant AND to one account.

    Both models are FORCE ROW LEVEL SECURITY: `tenant_memberships` and
    `delegated_users` on `app.tenant_id`, `user_credentials` on `app.user_id`.
    These queries used to run on a bare `acquire()`, which binds neither, so
    every one of them matched zero rows.

    The principal built here carries no roles and no scopes. It binds the two
    settings and authorises nothing, the construction `password_reset` uses to
    reach one tenant's sessions. It refuses outside the private edition: binding
    the private tenant in a cloud process is the fallback `owner_connection`
    exists to refuse.
    """
    from core.editions import EDITION_PRIVATE
    from core.identity.db_context import MissingTenantContext, tenant_connection

    if not _private_edition():
        raise MissingTenantContext(
            "delegated accounts exist only in the private edition; refusing to "
            "bind the private tenant in this process"
        )
    principal = Principal(
        user_id=user_id, tenant_id=PRIVATE_TENANT_ID, session_id=uuid.uuid4(),
        edition=EDITION_PRIVATE, username="", is_owner=False,
    )
    async with tenant_connection(principal) as conn:
        yield conn


@asynccontextmanager
async def _tenant_bound():
    """A transaction bound to the private tenant and to no user.

    For the owner's view across every account: the listing, revoke and role
    change. No user is bound, so no account's credential is reachable here.
    """
    from core.identity.db_context import owner_connection

    async with owner_connection("security/delegated.py") as conn:
        yield conn


#: A membership account: the membership, the user it belongs to, and nothing
#: secret. The tenant is predicated explicitly as well as by RLS, so a query
#: that ran on a wrongly bound connection would still find only private rows.
_ACCOUNT_SELECT = """
    SELECT m.user_id, u.external_subject AS username, m.private_role AS role,
           m.person_id, m.membership_grants_access, u.status AS user_status,
           u.deleted_at, COALESCE(m.joined_at, m.created_at) AS created_at
      FROM tenant_memberships m
      JOIN users u ON u.id = m.user_id
     WHERE m.tenant_id = $1 AND m.private_role IS NOT NULL
"""


def _account_active(row) -> bool:
    """A membership account grants access only while all three hold.

    `membership_grants_access` is the generated predicate (`035`); reading
    `status` alone is how a suspended member keeps access.
    """
    return bool(
        row["membership_grants_access"]
        and row["user_status"] == "active"
        and row["deleted_at"] is None
    )


def _row_out(row) -> dict:
    """Public shape of a legacy row. The password hash NEVER leaves this module."""
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "role": row["role"],
        "person_id": str(row["person_id"]) if row["person_id"] else None,
        "is_primary_owner": bool(row["is_primary_owner"]),
        "active": bool(row["active"]),
        "voice_enrolled": bool(row["voice_enrolled"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        # True only for an account created before reserved names were refused.
        # Such an account cannot log in or hold a role; the owner should revoke
        # it and grant the person access again under another name.
        "reserved_name": _holds_reserved_name(row["username"]),
    }


def _account_out(row) -> dict:
    """Public shape of a membership account: the same keys as `_row_out`.

    `id` is the user id, which is what the revoke and role routes send back.
    `is_primary_owner` is False by construction: `092` refuses
    `primary_owner` as a private role and a primary-owner membership carries
    none. No credential column is selected, so none can leak.
    """
    return {
        "id": str(row["user_id"]),
        "username": row["username"],
        "role": row["role"],
        "person_id": str(row["person_id"]) if row["person_id"] else None,
        "is_primary_owner": False,
        "active": _account_active(row),
        "voice_enrolled": False,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "reserved_name": _holds_reserved_name(row["username"]),
    }


async def _account_by_id(conn, user_id: uuid.UUID):
    return await conn.fetchrow(_ACCOUNT_SELECT + " AND m.user_id = $2",
                               PRIVATE_TENANT_ID, user_id)


async def create_delegated_user(
    *, username: str, password: str, role: str, person_id: Optional[uuid.UUID] = None
) -> dict:
    """Create an account with its own bcrypt hash. Never primary owner.

    One transaction, three rows: the `users` row at the id the runtime
    principal already carries (`private_user_id`), with the account name in
    `external_subject` as provisioning does for the owner; the credential; and
    the membership in the private tenant. Either all three exist or none does.
    """
    from api.middleware.auth import hash_password

    username = validate_new_username(username)
    password = validate_new_password(password)
    role = validate_grantable_role(role)
    user_id = private_user_id(username)
    password_hash = hash_password(password)

    try:
        async with _account_bound(user_id) as conn:
            # The name may already be an identity that is not a delegated
            # account: `023` seeds `private_user_id("admin")` as the primary
            # owner, so on a deployment whose owner is renamed, "admin" is that
            # seeded row. Writing a credential for it would give the seat a
            # password. Refused here, and by the primary key if two grants race.
            taken = await conn.fetchval(
                """SELECT EXISTS (SELECT 1 FROM users
                                   WHERE id = $1
                                      OR (auth_provider = 'password' AND external_subject = $2))
                       OR EXISTS (SELECT 1 FROM delegated_users WHERE username = $2)""",
                user_id, username,
            )
            if taken:
                raise AccountExists(f"an account named {username!r} already exists")
            await conn.execute(
                """INSERT INTO users (id, display_name, auth_provider, external_subject)
                   VALUES ($1, $2, 'password', $2)""",
                user_id, username,
            )
            await conn.execute(
                "INSERT INTO user_credentials (user_id, password_hash) VALUES ($1, $2)",
                user_id, password_hash,
            )
            await conn.execute(
                """INSERT INTO tenant_memberships
                       (tenant_id, user_id, role, private_role, person_id, status, joined_at)
                   VALUES ($1, $2, $3, $4, $5, 'active', NOW())""",
                PRIVATE_TENANT_ID, user_id, _tenant_role_for(role), role, person_id,
            )
            row = await _account_by_id(conn, user_id)
    except AccountExists:
        raise
    except Exception as exc:
        # 23505: a concurrent grant took the name between the check and the
        # INSERT. The transaction has rolled back; report what it means.
        if getattr(exc, "sqlstate", None) == "23505":
            raise AccountExists(f"an account named {username!r} already exists") from exc
        raise
    return _account_out(row)


async def get_delegated_user(username: str) -> Optional[dict]:
    username = (username or "").strip().lower()
    if not username or not _private_edition():
        return None
    async with _account_bound(private_user_id(username)) as conn:
        account = await _account_by_id(conn, private_user_id(username))
        if account is not None:
            return _account_out(account)
        row = await conn.fetchrow(
            "SELECT * FROM delegated_users WHERE username = $1", username
        )
    return _row_out(row) if row else None


async def list_delegated_users() -> list[dict]:
    """Every account, both models, newest first."""
    if not _private_edition():
        return []
    async with _tenant_bound() as conn:
        accounts = await conn.fetch(_ACCOUNT_SELECT, PRIVATE_TENANT_ID)
        rows = await conn.fetch("SELECT * FROM delegated_users")
    both = [(a["created_at"], _account_out(a)) for a in accounts]
    both += [(r["created_at"], _row_out(r)) for r in rows]
    both.sort(key=lambda pair: pair[0].timestamp() if pair[0] else 0.0, reverse=True)
    return [out for _, out in both]


async def authenticate_delegated(username: str, password: str) -> bool:
    """Password check for a delegated account (login fallback after the admin
    check). Inactive accounts and the admin username always fail here."""
    from api.middleware.auth import verify_password

    from api.middleware.auth import _dummy_hash

    username = (username or "").strip().lower()
    if not username or not password or is_primary_owner_username(username):
        return False
    if not _private_edition():
        return False

    found = False
    usable = False
    password_hash: Optional[str] = None
    try:
        user_id = private_user_id(username)
        async with _account_bound(user_id) as conn:
            account = await _account_by_id(conn, user_id)
            if account is not None:
                found = True
                # Reachable only because this connection is bound to this
                # account: `user_credentials` is FORCE RLS on `app.user_id`.
                password_hash = await conn.fetchval(
                    "SELECT password_hash FROM user_credentials WHERE user_id = $1",
                    user_id,
                )
                usable = _account_active(account) and password_hash is not None
            else:
                row = await conn.fetchrow(
                    "SELECT password_hash, active FROM delegated_users WHERE username = $1",
                    username,
                )
                if row is not None:
                    found = True
                    password_hash = row["password_hash"]
                    usable = bool(row["active"])
    except Exception:
        return False  # DB down → fail closed

    # P2-01: verify a hash even when there is nothing to verify against.
    #
    # This used to `return False` here, before `verify_password`. bcrypt costs
    # orders of magnitude more than the query, so a name with no row — or one
    # whose access had been revoked — was refused measurably faster than a real
    # account, which answers "does this account exist?" on a public,
    # unauthenticated endpoint.
    #
    # `authenticate_user` has always done this for the owner (`_dummy_hash`);
    # the delegated fallback beneath it did not, so the login route as a whole
    # was uniform for one account and not for the rest. Comparing against the
    # dummy hash makes the work performed identical and the result always False,
    # since no supplied password verifies against it.
    #
    # A reserved memory namespace name takes the same path as a revoked
    # account: hash work identical, result always False. Its chat would
    # otherwise recall and write the namespace's rows as its own memory.
    if found and is_reserved_namespace(username):
        logger.warning(
            "refusing login for delegated account %r: the name is a reserved "
            "memory namespace. Revoke it and grant access under another name.",
            username,
        )
        usable = False
    # The same for a name that belongs to a deployment identity. An account
    # called after the voice service would otherwise sign in as it.
    identity = reserved_identity(username) if found else None
    if identity is not None:
        logger.warning(
            "refusing login for delegated account %r: the name belongs to %s. "
            "Revoke it and grant access under another name.",
            username, identity,
        )
        usable = False
    candidate = password_hash if (usable and password_hash) else _dummy_hash()
    try:
        return verify_password(password, candidate) and usable
    except Exception:
        return False


async def get_delegated_role(username: str) -> Optional[str]:
    """Role for an ACTIVE delegated account, else None (fail closed).

    A reserved memory namespace name, or a name that belongs to a deployment
    identity (`reserved_identity`), never resolves to a role, whatever its row
    says. Refusing login alone would leave a token issued before the rule
    existed working until it expired; with no role the scope middleware denies
    it everywhere and the principal carries no scopes.
    """
    if _holds_reserved_name(username):
        return None
    username = (username or "").strip().lower()
    if not username or not _private_edition():
        return None
    try:
        user_id = private_user_id(username)
        async with _account_bound(user_id) as conn:
            account = await _account_by_id(conn, user_id)
            if account is not None:
                return account["role"] if _account_active(account) else None
            row = await conn.fetchrow(
                "SELECT role, active FROM delegated_users WHERE username = $1",
                username,
            )
    except Exception:
        return None
    if not row or not row["active"]:
        return None
    return row["role"]


async def revoke_delegated_user(user_id: uuid.UUID) -> Optional[dict]:
    """Deactivate an account. Refuses primary-owner rows at the SQL level too.

    A membership account is suspended, which is what `035` made of an inactive
    legacy row: `membership_grants_access` turns false, so the role lookup
    returns None and the scope middleware denies every token the account holds.
    `private_role IS NOT NULL` is this model's primary-owner guard: the owner's
    membership carries no private role, so no statement here can reach it.
    """
    async with _tenant_bound() as conn:
        hit = await conn.fetchval(
            """UPDATE tenant_memberships
                  SET status = 'suspended', suspended_at = COALESCE(suspended_at, NOW())
                WHERE tenant_id = $1 AND user_id = $2
                  AND private_role IS NOT NULL AND status <> 'removed'
                RETURNING user_id""",
            PRIVATE_TENANT_ID, user_id,
        )
        if hit is not None:
            return _account_out(await _account_by_id(conn, user_id))
        row = await conn.fetchrow(
            """UPDATE delegated_users SET active = FALSE, updated_at = NOW()
               WHERE id = $1 AND is_primary_owner IS FALSE
               RETURNING *""",
            user_id,
        )
    return _row_out(row) if row else None


async def change_delegated_role(user_id: uuid.UUID, role: str) -> Optional[dict]:
    """Change a delegated account's role. primary_owner is not assignable and
    primary-owner rows are untouchable (WHERE clause)."""
    role = validate_grantable_role(role)
    async with _tenant_bound() as conn:
        hit = await conn.fetchval(
            """UPDATE tenant_memberships
                  SET private_role = $3, role = $4
                WHERE tenant_id = $1 AND user_id = $2 AND private_role IS NOT NULL
                RETURNING user_id""",
            PRIVATE_TENANT_ID, user_id, role, _tenant_role_for(role),
        )
        if hit is not None:
            return _account_out(await _account_by_id(conn, user_id))
        row = await conn.fetchrow(
            """UPDATE delegated_users SET role = $2, updated_at = NOW()
               WHERE id = $1 AND is_primary_owner IS FALSE
               RETURNING *""",
            user_id, role,
        )
    return _row_out(row) if row else None


# ── Audit log ────────────────────────────────────────────────────────────────

async def audit(actor: str, action: str, target: str = "", **details) -> None:
    """Record an access change (or denied attempt). Fail-soft: auditing must
    never break the flow, but failures are logged loudly."""
    import json

    try:
        async with acquire() as conn:
            await conn.execute(
                """INSERT INTO access_audit_log (actor, action, target, details)
                   VALUES ($1, $2, $3, $4::jsonb)""",
                (actor or "")[:80], (action or "")[:80], (target or "")[:120],
                json.dumps({k: str(v)[:200] for k, v in details.items()}),
            )
    except Exception as e:  # noqa: BLE001
        logger.error("access audit write FAILED (%s %s→%s): %s", actor, action, target, e)


async def list_audit_log(limit: int = 100) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM access_audit_log ORDER BY created_at DESC LIMIT $1", limit
        )
    out = []
    for r in rows:
        details = r["details"]
        if isinstance(details, str):
            import json
            try:
                details = json.loads(details)
            except ValueError:
                details = {}
        out.append({
            "id": r["id"],
            "actor": r["actor"],
            "action": r["action"],
            "target": r["target"],
            "details": details or {},
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


__all__ = [
    "AccountExists",
    "verify_owner_password", "is_primary_owner_username",
    "validate_new_username", "validate_new_password", "validate_grantable_role",
    "create_delegated_user", "get_delegated_user", "list_delegated_users",
    "authenticate_delegated", "get_delegated_role",
    "revoke_delegated_user", "change_delegated_role",
    "audit", "list_audit_log",
]
