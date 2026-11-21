"""Sessions and devices (Phase 2 batch 8).

`user_sessions` and `user_devices` have existed since `023` and have never been
written to. `057` fixed what their policies permitted; this is what fills them.

── A session row is not the session ────────────────────────────────────────

The refresh token is the session. This table records it so a person can SEE
where they are signed in and END one of those places, and so a revocation
survives the token's own lifetime. `token_hash` holds SHA-256 of the refresh
`jti`, never the token: a session table containing usable tokens turns a read of
that table into account takeover, which is the reason `023` wrote the column
that way and the reason nothing here changes it.

SHA-256 rather than bcrypt, which is the opposite of the choice `056` made and
for the stated reason: a `jti` is a 256-bit random value, so the only property
needed is that the stored form cannot be presented, and this lookup happens on
every single refresh. `056`'s codes are typed by a human and therefore
low-entropy, which is what a slow hash is for.

── Revocation is a fact about the row, not about the token ─────────────────

`revoke_session` writes `revoked_at`; it does not try to reach into the token.
A refresh that presents a revoked session's `jti` is refused because the lookup
says so. That means revocation works for tokens already in an attacker's hands,
which is the entire point — and it means the check has to happen on the refresh
path or the row is decoration.

── Where the addresses go ──────────────────────────────────────────────────

`ip_hash` is an HMAC keyed on the application secret, not a bare SHA-256. The
IPv4 space is 2^32 and a plain digest of it is a lookup table somebody builds
once; keying makes the stored value useless to anybody who does not also hold
the secret. It is stored at all so that "sign me out of the session that is not
me" can show something a person recognises, which is also why `user_agent` is
kept in the clear — it is self-describing and low-sensitivity, and hashing it
would make the list useless without protecting anything.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from core.identity import auth_audit

logger = logging.getLogger("ira.identity.sessions")


class SessionError(RuntimeError):
    """The operation was refused. Safe to show to the caller."""


class SessionUnavailable(SessionError):
    """The store could not be reached. Fail closed — never fail open."""


@dataclass(frozen=True, slots=True)
class Session:
    session_id: uuid.UUID
    device_id: uuid.UUID | None
    issued_at: datetime
    expires_at: datetime | None
    last_seen_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    user_agent: str
    is_current: bool = False


@dataclass(frozen=True, slots=True)
class Device:
    device_id: uuid.UUID
    label: str
    platform: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


def hash_session_token(jti: str) -> str:
    """SHA-256 of a refresh token's `jti`.

    Deliberately not salted. A salt defends a low-entropy secret against a
    dictionary; a `jti` is 256 bits of randomness, so there is no dictionary,
    and a per-row salt would make the lookup impossible without reading every
    row first — turning every refresh into a table scan.
    """
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def hash_ip(address: str | None) -> str | None:
    """Keyed hash of a client address, or None if there is none.

    HMAC rather than a bare digest: the whole IPv4 space fits in a table
    somebody precomputes once, so an unkeyed hash of an address is the address.
    """
    if not address:
        return None
    from config import get_settings

    secret = getattr(get_settings(), "ira_secret_key", None) or ""
    if not secret:
        # No key means no keyed hash. Storing an unkeyed one "for now" is how a
        # reversible identifier ends up in a table nobody revisits.
        return None
    return hmac.new(
        str(secret).encode("utf-8"), address.encode("utf-8"), hashlib.sha256
    ).hexdigest()


async def record_session(
    principal,
    *,
    refresh_jti: str,
    expires_at: datetime | None = None,
    device_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
) -> Session:
    """Record a newly issued refresh token as a session.

    `tenant_id` and `user_id` are taken from the bound principal, which is the
    same pair the policies check — so the row this writes and the row the
    connection may write are one statement, not two agreeing values.
    """
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_sessions
                       (tenant_id, user_id, device_id, token_hash,
                        expires_at, ip_hash, user_agent, last_seen_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                RETURNING id, device_id, issued_at, expires_at, last_seen_at,
                          revoked_at, revoked_reason, user_agent
                """,
                principal.tenant_id, principal.user_id, device_id,
                hash_session_token(refresh_jti), expires_at,
                hash_ip(ip_address), (user_agent or "")[:400],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record a session: %s", type(exc).__name__)
        raise SessionUnavailable("the session store could not be reached") from exc

    return _session(row, current=True)


async def session_is_live(principal, refresh_jti: str) -> bool:
    """True if this token's session exists, is unrevoked and unexpired.

    Called on the refresh path, so it is the check that makes revocation mean
    anything. A session that cannot be found is NOT live: a token whose row was
    erased, or which was never recorded, has nothing vouching for it.

    Raises rather than returning False when the store is unreachable. Both are
    closed, but the caller needs to tell "this session was revoked" from "I
    cannot say", because the first should sign a user out and the second should
    not.
    """
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as conn:
            found = await conn.fetchval(
                """
                SELECT 1 FROM user_sessions
                 WHERE token_hash = $1
                   AND NOT session_is_revoked
                   AND (expires_at IS NULL OR expires_at > NOW())
                """,
                hash_session_token(refresh_jti),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not check a session: %s", type(exc).__name__)
        raise SessionUnavailable("the session store could not be reached") from exc

    return found is not None


async def touch_session(principal, refresh_jti: str) -> None:
    """Record that this session was used. Best effort by design.

    `last_seen_at` is what makes a session list readable — "this one was active
    a minute ago, this one two months" is how somebody spots the entry that is
    not them. It is not a security control, so a failure here must never refuse
    a refresh that is otherwise valid.
    """
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                "UPDATE user_sessions SET last_seen_at = NOW() "
                " WHERE token_hash = $1 AND NOT session_is_revoked",
                hash_session_token(refresh_jti),
            )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("could not touch a session", exc_info=True)


async def list_sessions(principal, *, current_jti: str | None = None) -> list[Session]:
    """This user's sessions, newest first, revoked ones included.

    Revoked sessions are shown rather than hidden: "I signed that out" is
    information, and a list that silently drops entries cannot answer "did my
    revocation work".
    """
    from core.identity.db_context import tenant_connection

    current_hash = hash_session_token(current_jti) if current_jti else None
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, device_id, issued_at, expires_at, last_seen_at,
                   revoked_at, revoked_reason, user_agent, token_hash
              FROM user_sessions
             ORDER BY issued_at DESC
            """,
        )
    return [_session(r, current=(current_hash is not None
                                 and r["token_hash"] == current_hash))
            for r in rows]


async def revoke_session(principal, session_id, *, reason: str = "revoked") -> bool:
    """End one session. True if it was live and is now revoked.

    Conditional on the row still being live, so the write is what decides — the
    same reasoning `056` records for spending a recovery code. Returns False for
    an unknown id rather than raising: under RLS "not mine" and "does not exist"
    are the same answer, and they must stay the same answer or this becomes a
    way to enumerate other people's session ids.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        ended = await conn.fetchval(
            """
            UPDATE user_sessions
               SET revoked_at = NOW(), revoked_reason = $2
             WHERE id = $1 AND NOT session_is_revoked
            RETURNING id
            """,
            session_id, reason[:200],
        )
    if ended is not None:
        logger.info("a session was revoked")
        # Only on the branch that actually revoked something. The False branch
        # is "unknown id, or not yours", which under RLS are the same answer —
        # recording it would make the audit trail able to distinguish what the
        # return value deliberately cannot, and a log somebody can drive by
        # guessing session ids is an enumeration oracle with a delay on it.
        await auth_audit.record(auth_audit.SESSION_REVOKED,
                                user_id=principal.user_id,
                                tenant_id=principal.tenant_id,
                                detail=reason[:200])
    return ended is not None


async def revoke_all_sessions(
    principal, *, except_jti: str | None = None, reason: str = "revoked all",
) -> int:
    """End every live session, optionally sparing the caller's own.

    This is "sign out everywhere", and sparing the current session is what makes
    it usable: a person reacting to a suspicious entry should not be logged out
    of the device they are reacting from. Passing nothing ends everything,
    which is what an account recovery wants.
    """
    from core.identity.db_context import tenant_connection

    spare = hash_session_token(except_jti) if except_jti else None
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            UPDATE user_sessions
               SET revoked_at = NOW(), revoked_reason = $1
             WHERE NOT session_is_revoked
               AND ($2::text IS NULL OR token_hash IS DISTINCT FROM $2)
            RETURNING id
            """,
            reason[:200], spare,
        )
    logger.info("%d session(s) revoked", len(rows))
    # Recorded even when it ended nothing, unlike `revoke_session` above. This
    # one takes no id, so there is nothing to guess and nothing to enumerate —
    # and "sign out everywhere found nothing to sign out" is a fact worth
    # having, because it is what a session store that stopped recording looks
    # like.
    await auth_audit.record(auth_audit.SESSIONS_REVOKED_ALL,
                            user_id=principal.user_id,
                            tenant_id=principal.tenant_id,
                            detail=f"{len(rows)} session(s), reason: {reason}"[:200])
    return len(rows)


# ── Devices ─────────────────────────────────────────────────────────────────


async def register_device(principal, *, label: str, platform: str = "") -> Device:
    """Record a device this user signs in from."""
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_devices (tenant_id, user_id, label, platform,
                                          last_seen_at)
                VALUES ($1, $2, $3, $4, NOW())
                RETURNING id, label, platform, created_at, last_seen_at, revoked_at
                """,
                principal.tenant_id, principal.user_id,
                (label or "")[:200], (platform or "")[:80],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not register a device: %s", type(exc).__name__)
        raise SessionUnavailable("the device store could not be reached") from exc

    return _device(row)


async def list_devices(principal) -> list[Device]:
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, label, platform, created_at, last_seen_at, revoked_at
              FROM user_devices ORDER BY created_at DESC
            """,
        )
    return [_device(r) for r in rows]


async def revoke_device(principal, device_id, *, reason: str = "device revoked") -> int:
    """Revoke a device AND every live session on it.

    One transaction, and the sessions are the point. Marking a lost phone
    revoked while its sessions keep refreshing is a checkbox, not a revocation —
    and the sessions have to go first, because if the two were split and the
    second half failed, the half that survived would be the useless one.

    Returns the number of sessions ended. Revoking an already-revoked device is
    not an error: it may still carry live sessions, and refusing would leave the
    only way to end them behind a state check.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE user_sessions
                   SET revoked_at = NOW(), revoked_reason = $2
                 WHERE device_id = $1 AND NOT session_is_revoked
                RETURNING id
                """,
                device_id, reason[:200],
            )
            await conn.execute(
                "UPDATE user_devices SET revoked_at = COALESCE(revoked_at, NOW()) "
                " WHERE id = $1",
                device_id,
            )
    logger.info("a device was revoked, ending %d session(s)", len(rows))
    # SESSIONS_REVOKED_ALL rather than an event of its own: what the log is for
    # is "sessions ended here, and this is why", and `061`'s vocabulary is a
    # fixed CHECK rather than a free-text column precisely so it does not grow
    # a synonym per call site. The reason phrase carries the device.
    await auth_audit.record(
        auth_audit.SESSIONS_REVOKED_ALL,
        user_id=principal.user_id, tenant_id=principal.tenant_id,
        detail=f"device revoked, {len(rows)} session(s) ended"[:200])
    return len(rows)


def _session(row, *, current: bool) -> Session:
    return Session(
        session_id=row["id"],
        device_id=row["device_id"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
        user_agent=row["user_agent"] or "",
        is_current=current,
    )


def _device(row) -> Device:
    return Device(
        device_id=row["id"],
        label=row["label"] or "",
        platform=row["platform"] or "",
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
    )


__all__ = [
    "Device",
    "Session",
    "SessionError",
    "SessionUnavailable",
    "hash_ip",
    "hash_session_token",
    "list_devices",
    "list_sessions",
    "record_session",
    "register_device",
    "revoke_all_sessions",
    "revoke_device",
    "revoke_session",
    "session_is_live",
    "touch_session",
]
