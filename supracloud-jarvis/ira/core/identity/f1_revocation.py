"""Authoritative PostgreSQL ordering for F1 access-token revocation.

Applied as revision 3 of the F1 revocation proposal (owner-approved
2026-10-19). Only SHA-256 JWT identifiers are persisted, in
f1_token_revocations (migration 091), which ira_app may SELECT and INSERT
and nobody may UPDATE or DELETE. No SECURITY DEFINER function is involved:
the per-token advisory lock below is taken here, in the same transaction as
the write or the check. Redis remains a deny cache, never
an authority that can resurrect a PostgreSQL-revoked token. No cross-store
atomicity is claimed. Database failures are fatal, not fail-open fallbacks.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import time

class SessionCommitDenied(RuntimeError):
    """Reason code only; adapters decide HTTP/domain error presentation."""

@dataclass(frozen=True)
class TokenContext:
    digest: str
    subject: str
    expires: int

_context = ContextVar('f1_authenticated_token', default=None)


def clear_request_token():
    _context.set(None)


def bind_request_token(payload):
    if not payload.jti or not payload.exp:
        raise SessionCommitDenied('session_identifier_required')
    _context.set(TokenContext(digest(payload.jti), payload.sub, int(payload.exp.timestamp())))


def digest(jti):
    return hashlib.sha256(jti.encode('utf-8')).hexdigest()


_LOCK_SQL = 'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))'
_INSERT_SQL = ('INSERT INTO f1_token_revocations (token_hash) VALUES ($1) '
               'ON CONFLICT (token_hash) DO NOTHING')
_EXISTS_SQL = 'SELECT EXISTS (SELECT 1 FROM f1_token_revocations WHERE token_hash = $1)'


def _lock_key(token_digest):
    return 'f1.jwt:' + token_digest


async def record_revocation(jti):
    from utils.db import acquire
    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            # The SAME transaction advisory lock guard_commit holds through an
            # evidence COMMIT, so a revocation waits for an in-flight save to
            # finish and is never acknowledged before its own COMMIT.
            await conn.execute(_LOCK_SQL, _lock_key(digest(jti)))
            await conn.execute(_INSERT_SQL, digest(jti))
    # Returning from this context means PostgreSQL COMMIT completed. Only now
    # may the caller acknowledge revocation or attempt the Redis deny cache.


#: The Redis deny-cache prefix (api.middleware.auth._REVOKED_PREFIX).
REDIS_REVOKED_PREFIX = "ira:revoked:"


async def seed_revocations_from_redis() -> tuple[int, str]:
    """Copy surviving Redis-only revocations into PostgreSQL. Run at every start.

    Until R08 the private edition recorded "log out" only in Redis. A token
    signed out then stays signed out after an upgrade only while its Redis key
    survives; copying it here makes it durable. Idempotent (ON CONFLICT DO
    NOTHING). Redis being unreachable is not an error: the next start retries.
    Returns (keys copied, Redis state).
    """
    try:
        from utils.redis_client import get_redis

        client = get_redis()
        jtis = []
        cursor = 0
        while True:
            cursor, keys = await client.scan(
                cursor=cursor, match=f"{REDIS_REVOKED_PREFIX}*", count=500)
            for key in keys:
                name = key.decode() if isinstance(key, bytes) else str(key)
                jti = name[len(REDIS_REVOKED_PREFIX):]
                if jti:
                    jtis.append(jti)
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        return 0, f"unavailable ({type(exc).__name__})"
    for jti in jtis:
        await record_revocation(jti)
    return len(jtis), "read"


async def is_revoked(jti):
    from utils.db import acquire
    async with acquire() as conn:
        return bool(await conn.fetchval(_EXISTS_SQL, digest(jti)))


async def guard_commit(conn, principal):
    context = _context.get()
    if context is None:
        # Trusted internal engine callers have no HTTP token. HTTP integration
        # binds one through require_auth; no route body can set this context.
        return
    if principal.username != context.subject:
        raise SessionCommitDenied('session_context_mismatch')
    await conn.execute(_LOCK_SQL, _lock_key(context.digest))
    if context.expires <= time.time():
        raise SessionCommitDenied('session_expired')
    if await conn.fetchval(_EXISTS_SQL, context.digest):
        raise SessionCommitDenied('session_revoked')
    # Held on the mutation connection through COMMIT, without a lease. A
    # disconnect before COMMIT rolls back; after submission its outcome may
    # be unknown to the caller. Recover with idempotency, never assume rollback.
