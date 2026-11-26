"""
JWT authentication middleware.

Token model (P2.1):
  - Access tokens: 30-min expiry, carry `jti` (JWT ID) and per-user `ver`
  - Refresh tokens: 7-day expiry, carry `jti` and `ver`; only valid at /auth/refresh
  - Revocation:  PostgreSQL f1_token_revocations (SHA-256 of the jti, migration
                 091), in both editions; Redis ira:revoked:{jti} is a deny cache
  - Revoke-all:  PostgreSQL auth_token_versions, keyed by users.id (migration
                 096). Redis is not read for it on any request.

Two credential types:
  - Bearer JWT  → standard API access (issued at /auth/token)
  - API Key     → for service-to-service calls (X-API-Key header)

Dev mode: when DEV_MODE=true, require_auth returns the admin username
without checking any token. NEVER enable in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import lru_cache as _lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
# P16-R1: was `from jose import JWTError, jwt`. python-jose was removed because
# it is the only thing pulling `ecdsa` (Minerva timing attack, PYSEC-2026-1325,
# no fix planned upstream) and the only thing pinning `pyasn1<0.5.0`, which made
# six further advisories unfixable.
#
# The swap is behaviour-preserving here: every token this module mints and reads
# is HS256 over a symmetric secret, with no `aud` or `iss` claim, which is the
# case where python-jose and PyJWT agree exactly. `PyJWTError` is PyJWT's base
# exception and covers what `JWTError` covered — signature failure, expiry, and
# malformed input.
import jwt
from jwt import PyJWTError
from passlib.context import CryptContext
from typing import Optional

from pydantic import BaseModel, ValidationError

from config import get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer(auto_error=False)

# Redis key prefixes
_REVOKED_PREFIX = "ira:revoked:"
_TOKEN_VER_PREFIX = "ira:token_ver:"


# ── Token models ──────────────────────────────────────────────────────────────

class TokenPayload(BaseModel):
    sub: str
    exp: datetime
    jti: str = ""
    tok: str = "access"   # "access" | "refresh"
    #: The SELECTED TENANT, and a selector — never authority.
    #:
    #: A cloud user who belongs to several organisations picks one at login;
    #: this records which. It is NOT proof of membership, role, scope or
    #: ownership, and nothing may read it as such: `resolve_principal` takes it
    #: as a *request* for a tenant and verifies it against `tenant_memberships`
    #: on every single request, so a token edited to name another tenant
    #: resolves to nothing.
    #:
    #: Optional, because the private edition has no tenants and a cloud user
    #: with exactly one membership needs no selection. A missing value means
    #: "resolve whatever the user's single membership is", which is what the
    #: resolver already did before this claim existed.
    tid: Optional[str] = None
    #: Per-user version counter (revoke-all). **Required, with no default**
    #: (P2-04): `ver: int = 0` meant a token that omitted the claim decoded as
    #: one asserting version zero — a claim the issuer never made, read as one
    #: it did. Required here makes a missing or malformed value a pydantic
    #: ValidationError, which `decode_token` turns into a 401.
    ver: int
    #: The account's `users.id` (migration 096). Optional, because a required
    #: claim would refuse every token issued before it existed: those are
    #: resolved from `sub`, exactly as their account was created. Signed with
    #: the rest of the token, so it is used as given.
    uid: Optional[str] = None
    #: When the holder last proved who they are with a credential, Unix seconds
    #: (D-095). Set at login and at `/auth/step-up`; COPIED, never re-stamped,
    #: by `/auth/refresh`, so a session kept alive by refreshes for a week still
    #: says it was authenticated a week ago. Optional so a token issued before
    #: the claim decodes; `security/stepup.py` reads None as "never recently".
    auth_time: Optional[int] = None
    #: The methods used then, OIDC `amr` names: "pwd", "otp", "recovery".
    amr: Optional[list[str]] = None


class TokenResponse(BaseModel):
    """Slim response — single access token (used by /auth/refresh)."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LoginResponse(BaseModel):
    """Full login response — access + refresh tokens."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int          # access token seconds remaining
    refresh_expires_in: int  # refresh token seconds remaining


# ── Password utilities ────────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


# ── Redis revocation helpers ──────────────────────────────────────────────────

def _auth_fail_closed() -> bool:
    """When true, a Redis error in a revocation/version check DENIES access.

    Defaults to False (fail open, current behaviour). Set AUTH_FAIL_CLOSED_ON_REDIS=true
    for a hardened, security-over-availability posture.
    """
    try:
        return bool(get_settings().auth_fail_closed_on_redis)
    except Exception:
        return False


async def _is_revoked(jti: str) -> bool:
    """True if this token was signed out ("log out", one device).

    PostgreSQL is the authority, in both editions (`f1_token_revocations`,
    migration 091): a revocation is recorded there before it is acknowledged,
    so losing Redis cannot revive a signed-out token. That check **fails
    closed**: a database error is a 503, never a pass.

    Redis is a deny cache only. It still answers for revocations made before
    the private edition wrote to PostgreSQL, until the start-up seeder has
    copied them (`f1_revocation.seed_revocations_from_redis`). On a Redis error
    this part fails OPEN by default; AUTH_FAIL_CLOSED_ON_REDIS=true makes it
    fail closed too.
    """
    if not jti:
        return False
    from core.identity.f1_revocation import is_revoked
    try:
        if await is_revoked(jti):
            return True
    except Exception as e:  # noqa: BLE001 - every failure is the same answer
        logger.error("durable revocation check failed; refusing the request: %s",
                     type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Your sign-in could not be checked. Please try again.",
        ) from e
    try:
        from utils.redis_client import get_redis
        return bool(await get_redis().exists(f"{_REVOKED_PREFIX}{jti}"))
    except Exception as e:
        if _auth_fail_closed():
            logger.error("Redis revocation check failed — failing CLOSED (token treated as revoked): %s", e)
            return True
        logger.warning("Redis revocation check failed (fail-open): %s", e)
        return False


class RevocationNotDurable(RuntimeError):
    """The durable (PostgreSQL) revocation record could not be written."""


async def revoke_token(jti: str, ttl_seconds: int) -> None:
    """Add jti to the revocation list. TTL should match remaining token life.

    PostgreSQL is the authority, in both editions, and is written first, so a
    revocation is never acknowledged before its COMMIT. Until R08 the private
    edition wrote only Redis, fail-soft: a restart without persistence, a
    flush or an outage at sign-out left the signed-out token working. If the
    PostgreSQL write fails (a lock timeout behind an evidence save, or any
    database error), the Redis deny entry is still written below, so the token
    is never less revoked than it would otherwise be, and RevocationNotDurable
    is raised so the caller cannot report a sign-out that was not made durable.
    """
    if not jti or ttl_seconds <= 0:
        return
    durable_error: Exception | None = None
    from core.identity.f1_revocation import record_revocation
    try:
        await record_revocation(jti)
    except Exception as e:  # noqa: BLE001 - re-raised below, after the Redis write
        durable_error = e
    try:
        from utils.redis_client import get_redis
        await get_redis().setex(f"{_REVOKED_PREFIX}{jti}", ttl_seconds, "1")
    except Exception as e:
        logger.warning("Redis revoke_token failed: %s", e)
    if durable_error is not None:
        logger.error("durable token revocation failed; only the Redis deny entry "
                     "was attempted: %s", durable_error)
        raise RevocationNotDurable("durable revocation record not written") from durable_error


def _version_error(exc: Exception) -> HTTPException:
    """The HTTP answer for a failed version read: 401 for no account, else 503."""
    from core.identity.token_versions import UnknownAccount

    if isinstance(exc, UnknownAccount):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account is not recognised — please log in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication backend unavailable",
    )


async def _get_token_version(username: str, uid: str | None = None) -> int:
    """Current per-account token version (0 = never bumped), from PostgreSQL.

    **Fails closed, always** (migration 096). It used to read Redis, where a
    lost key read as 0 and revived every token issued before the last "log out
    everywhere", and by default it failed open. Now any database failure is a
    503 and a subject with no account is a 401. `AUTH_FAIL_CLOSED_ON_REDIS`
    still governs the per-token (jti) check, and only that.
    """
    from core.identity import token_versions

    try:
        return await token_versions.current(
            await token_versions.resolve_user_id(username, uid))
    except (token_versions.UnknownAccount, token_versions.TokenVersionUnavailable) as e:
        logger.warning("token-version check refused for %r: %s", username, e)
        raise _version_error(e) from e


async def bump_token_version(username: str, uid: str | None = None) -> None:
    """Raise the token version — invalidates every token issued to the account.

    **Raises 503 when the counter cannot be raised** (P2-04), and returns only
    after the new value is committed to PostgreSQL (migration 096), so a caller
    that then reports success has a revocation that a Redis restart, flush or
    restore cannot undo.

    This is the one operation where a partial success has no useful meaning: the
    caller is revoking access, and an unperformed revocation reported as done is
    worse than an error they can retry.
    """
    from core.identity import token_versions

    try:
        await token_versions.bump_subject(username, uid)
    except token_versions.UnknownAccount as e:
        # No `users` row means no counter, and `_get_token_version` refuses
        # every token of such a subject (401). There is nothing left alive to
        # revoke, so this is not a failure, and refusing it would stop an
        # owner from revoking a legacy account the seeder has not reached.
        logger.warning("bump_token_version: no account row for %r; its tokens are "
                       "already refused: %s", username, e)
    except Exception as e:  # noqa: BLE001 - every other failure is a 503
        logger.error("bump_token_version failed — revocation NOT applied: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not revoke tokens — the revocation backend is unavailable. "
                   "Nothing has been invalidated; please retry.",
        ) from e


# ── JWT issue helpers ─────────────────────────────────────────────────────────

def _authentication_claims(auth_time: int | None, amr) -> dict:
    """The `auth_time` and `amr` claims (D-095), or nothing when the moment of
    authentication is unknown. Both or neither: an `amr` without a time would
    say how without saying when, which no check could use."""
    if auth_time is None:
        return {}
    methods = [str(m) for m in (amr or ())]
    return {"auth_time": int(auth_time), "amr": methods}


def _make_access_token(username: str, *, ver: int, tid: str | None = None,
                       uid: str | None = None, auth_time: int | None = None,
                       amr=None) -> tuple[str, str, datetime]:
    """Returns (encoded_token, jti, expiry_utc).

    `ver` is required and keyword-only (P2-04). It defaulted to 0, which is what
    made an unversioned token expressible: a caller who forgot the argument
    minted a token asserting a version nobody had looked up. A caller who
    forgets it now gets a TypeError instead of a credential.

    `auth_time` and `amr` (D-095) record when and how the holder last proved
    who they are. Login and `/auth/step-up` pass the present moment; a refresh
    passes the refresh token's own values through unchanged.
    """
    cfg = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=cfg.access_token_expire_minutes)
    jti = str(uuid.uuid4())
    payload = {"sub": username, "exp": expire, "jti": jti, "tok": "access", "ver": ver}
    # Omitted entirely when absent rather than sent as null, so a private-edition
    # token is byte-identical to the one it was before this claim existed.
    if tid is not None:
        payload["tid"] = str(tid)
    if uid is not None:
        payload["uid"] = str(uid)
    payload.update(_authentication_claims(auth_time, amr))
    token = jwt.encode(payload, cfg.ira_secret_key, algorithm=ALGORITHM)
    return token, jti, expire


def _make_refresh_token(username: str, *, ver: int, tid: str | None = None,
                        uid: str | None = None, auth_time: int | None = None,
                        amr=None) -> tuple[str, str, datetime]:
    """Returns (encoded_token, jti, expiry_utc).

    The refresh token now carries `ver` (P2-04). It did not, and `/auth/refresh`
    did not check one, so `bump_token_version` invalidated every short-lived
    access token and left the seven-day credential that mints replacements
    entirely intact — and each replacement was stamped with the *current*
    version, so nothing downstream could tell it from a fresh login.
    """
    cfg = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=cfg.refresh_token_expire_days)
    jti = str(uuid.uuid4())
    payload = {"sub": username, "exp": expire, "jti": jti, "tok": "refresh", "ver": ver}
    # The refresh token carries the same selection. Without it, refreshing a
    # multi-tenant session would silently drop back to "no tenant selected" and
    # then fail to resolve — the tenant would have to be re-chosen every thirty
    # minutes.
    if tid is not None:
        payload["tid"] = str(tid)
    if uid is not None:
        payload["uid"] = str(uid)
    # The refresh token carries the authentication time too, because it is the
    # only thing a refresh has to copy it from (D-095).
    payload.update(_authentication_claims(auth_time, amr))
    token = jwt.encode(payload, cfg.ira_secret_key, algorithm=ALGORITHM)
    return token, jti, expire


async def assert_token_version_current(payload: TokenPayload) -> None:
    """Raise 401 if this token predates a revoke-all.

    One function for both verification paths — `require_auth` and
    `/auth/refresh` — so the two cannot drift. They had drifted: only the first
    of them checked, which is the whole of P2-04.

    The comparison is `current > claimed`, not `!=`. A token whose version is
    *higher* than the stored counter is not evidence of a revocation; it is what
    a lost or evicted Redis key looks like, and rejecting it would turn a cache
    eviction into a forced logout for every user at once.
    """
    current = await _get_token_version(payload.sub, payload.uid)
    if current > payload.ver:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="All tokens invalidated — please log in again",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def create_login_tokens(username: str, *, tenant_id=None, amr=("pwd",)) -> LoginResponse:
    """Issue access + refresh tokens. Call this at /auth/token login.

    `tenant_id` records which organisation the caller selected. It is carried
    as a selector and re-verified against membership on every request — see
    `TokenPayload.tid`. Passing one here does not grant anything.

    `amr` names the credentials the login just checked ("pwd", plus "otp" or
    "recovery" when a code was verified); both tokens are stamped with it and
    with the present moment as `auth_time` (D-095).
    """
    from core.identity import token_versions

    # The counter row is created at 0 here if the account has none, so every
    # account that has signed in has a row the per-request check can read.
    try:
        user_id = await token_versions.resolve_user_id(username)
        ver = await token_versions.current_for_login(user_id)
    except token_versions.UnknownAccount as e:
        # Authenticated, but with no `users` row: provisioning has not run for
        # it (a legacy account before its first start after 096). Refused
        # rather than issued a token nothing could check.
        logger.error("login refused for %r: no account row: %s", username, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This account is not ready yet — restart IRA, then sign in again.",
        ) from e
    except token_versions.TokenVersionUnavailable as e:
        raise _version_error(e) from e
    tid = str(tenant_id) if tenant_id is not None else None
    uid = str(user_id)
    now = datetime.now(timezone.utc)
    auth_time = int(now.timestamp())
    methods = [str(m) for m in amr] or ["pwd"]
    access_token, _, access_exp = _make_access_token(username, ver=ver, tid=tid, uid=uid,
                                                     auth_time=auth_time, amr=methods)
    refresh_token, _, refresh_exp = _make_refresh_token(username, ver=ver, tid=tid, uid=uid,
                                                        auth_time=auth_time, amr=methods)
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=max(0, int((access_exp - now).total_seconds())),
        refresh_expires_in=max(0, int((refresh_exp - now).total_seconds())),
    )


def create_token(username: str, *, ver: int) -> TokenResponse:
    """Synchronous single-token issue (backward compat / testing only).

    **The caller supplies `ver`** (P2-04). This function is synchronous and has
    no Redis, so it cannot resolve the authoritative version — which is exactly
    why it must not supply one. It used to pass `ver=0` on the caller's behalf
    and say so in this docstring, making a public, exported function that mints
    tokens claiming a version nobody had checked.

    Requiring the argument is a worse API and a correct one: whoever issues a
    session token has to have looked its version up first. For login use
    `create_login_tokens()`, which performs the lookup itself.
    """
    token, _, expire = _make_access_token(username, ver=ver)
    now = datetime.now(timezone.utc)
    return TokenResponse(
        access_token=token,
        expires_in=max(0, int((expire - now).total_seconds())),
    )


# ── JWT decode ────────────────────────────────────────────────────────────────

def decode_token(token: str) -> TokenPayload:
    """Validate JWT signature + expiry, and that the claims are the ones IRA issues.

    `ValidationError` is caught alongside `JWTError` (P2-04). Only `JWTError` was,
    so a correctly signed token whose claims did not fit `TokenPayload` — a
    missing `sub`, a missing or malformed `ver` — raised pydantic's exception
    straight through the dependency and became a 500 with a traceback instead of
    a refusal. A token this process cannot model is a token it must not accept.
    """
    cfg = get_settings()
    try:
        data = jwt.decode(token, cfg.ira_secret_key, algorithms=[ALGORITHM])
        return TokenPayload(**data)
    except (PyJWTError, ValidationError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── WebSocket credentials (P2-06) ─────────────────────────────────────────────
#
# A WebSocket cannot use `require_auth`: there is no dependency to hang off, and
# more importantly the check has to keep happening. An HTTP request re-presents
# its token every time; a socket presents it once and then streams for as long
# as it likes.
#
# Both sockets did `decode_token(token)` and nothing else, which is weaker than
# what every HTTP route gets — a **refresh** token opened a stream, and so did
# one that had already been logged out. So `/auth/logout` and `/auth/logout/all`,
# the two controls a person has when a token leaks, did not reach a socket at
# all: a stream opened with a stolen token kept delivering until the attacker
# chose to hang up.

#: Close code for a socket whose credential died mid-stream. Application-defined
#: (4000–4999) and deliberately distinct from 4001 "no/invalid token" and 4003
#: "refused", so a client can tell "re-authenticate" from "you may not have this".
SOCKET_CREDENTIAL_REVOKED = 4401

#: How often an open socket's credential is re-checked (migration 096: was 30 s).
SOCKET_REVALIDATION_ENV = "IRA_SOCKET_REVALIDATION_SECONDS"
DEFAULT_SOCKET_REVALIDATION_SECONDS = 10.0

#: Consecutive failed re-checks after which the socket is closed. A re-check
#: that cannot reach PostgreSQL proves nothing about the credential, so it is
#: never counted as "still valid"; two in a row close the socket.
SOCKET_REVALIDATION_ERRORS_ENV = "IRA_SOCKET_REVALIDATION_ERRORS"
DEFAULT_SOCKET_REVALIDATION_ERRORS = 2


def socket_revalidation_seconds() -> float:
    """Re-check interval, from the environment, never raising.

    Read like `client_ip.trusted_proxy_hops` rather than from `Settings`,
    because this runs in a long-lived background task where an exception is a
    silently dead watchdog rather than a visible error. Anything invalid falls
    back to the default — the fallback direction that keeps checking.
    """
    raw = os.getenv(SOCKET_REVALIDATION_ENV)
    if not raw or not raw.strip():
        return DEFAULT_SOCKET_REVALIDATION_SECONDS
    try:
        value = float(raw.strip())
    except ValueError:
        return DEFAULT_SOCKET_REVALIDATION_SECONDS
    if value <= 0 or value > 3600:
        return DEFAULT_SOCKET_REVALIDATION_SECONDS
    return value


def socket_revalidation_errors() -> int:
    """Consecutive failed re-checks before closing, from the environment, never raising."""
    raw = os.getenv(SOCKET_REVALIDATION_ERRORS_ENV)
    if not raw or not raw.strip():
        return DEFAULT_SOCKET_REVALIDATION_ERRORS
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_SOCKET_REVALIDATION_ERRORS
    if value < 1 or value > 100:
        return DEFAULT_SOCKET_REVALIDATION_ERRORS
    return value


async def authorise_socket_token(token: str) -> TokenPayload:
    """The connect-time gate for a WebSocket — the same checks `require_auth` runs.

    Raises `HTTPException(401)`; callers close the socket rather than returning
    it, since there is no response to attach a status to.

    One function for both sockets, and it deliberately mirrors `require_auth`
    step for step. The two hand-written versions had already drifted from it:
    that is how a refresh token came to open a stream, and 3C-03 → 3C-05 is the
    same lesson one layer up — fixing an instance is not fixing a class.
    """
    payload = decode_token(token)

    if payload.tok == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cannot open a socket — use an access token",
        )

    if payload.jti and await _is_revoked(payload.jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    await assert_token_version_current(payload)
    return payload


#: Outcomes of one socket re-check.
SOCKET_ALIVE, SOCKET_DEAD, SOCKET_UNKNOWN = "alive", "dead", "unknown"


async def _socket_credential_state(payload: TokenPayload) -> str:
    """Is this credential still valid? `SOCKET_UNKNOWN` when that cannot be told.

    It used to **fail open**: a re-check that could not reach Redis kept the
    socket, so a stream opened with a stolen token outlived "log out
    everywhere" for as long as the store was down. The version now lives in
    PostgreSQL (migration 096) and a failed read is reported as unknown, never
    as alive. The watchdog closes after `socket_revalidation_errors()` unknowns
    in a row, so one blip does not disconnect everyone and a lasting outage
    does not keep a dead credential streaming.
    """
    if payload.exp and datetime.now(timezone.utc) >= payload.exp:
        return SOCKET_DEAD
    try:
        if payload.jti and await _is_revoked(payload.jti):
            return SOCKET_DEAD
        if await _get_token_version(payload.sub, payload.uid) > payload.ver:
            return SOCKET_DEAD
    except HTTPException as e:
        if e.status_code == status.HTTP_401_UNAUTHORIZED:
            return SOCKET_DEAD
        logger.warning("socket revalidation could not reach the database: %s", e.detail)
        return SOCKET_UNKNOWN
    except Exception as e:  # noqa: BLE001 - unknown is the answer, never alive
        logger.warning("socket revalidation failed: %s", e)
        return SOCKET_UNKNOWN
    return SOCKET_ALIVE


async def _socket_credential_is_dead(payload: TokenPayload) -> bool:
    """True only when the credential is known to be dead (kept for callers)."""
    return await _socket_credential_state(payload) == SOCKET_DEAD


async def watch_socket_credential(
    websocket, payload: TokenPayload, interval: float | None = None,
    max_errors: int | None = None,
) -> None:
    """Close `websocket` once the credential that opened it stops being valid.

    Run it alongside the socket's own loops. It returns after closing, and is
    cancelled normally when the socket ends first.

    Expiry is checked as well as revocation, and that is the common case rather
    than the attack: an access token lives thirty minutes and a socket can live
    for days, so without this the expiry means nothing to anyone holding one.

    `max_errors` consecutive re-checks that cannot tell (the database is
    unreachable) close the socket too, with the same code: the client
    re-authenticates, which fails until the database is back.
    """
    period = interval if interval is not None else socket_revalidation_seconds()
    limit = max_errors if max_errors is not None else socket_revalidation_errors()
    unknown_in_a_row = 0
    try:
        while True:
            await asyncio.sleep(period)
            state = await _socket_credential_state(payload)
            if state == SOCKET_ALIVE:
                unknown_in_a_row = 0
                continue
            if state == SOCKET_UNKNOWN:
                unknown_in_a_row += 1
                if unknown_in_a_row < limit:
                    continue
                reason = f"{unknown_in_a_row} re-checks in a row could not reach the database"
            else:
                reason = "the credential that opened it is no longer valid"
            logger.info("closing socket for %r: %s", payload.sub, reason)
            try:
                await websocket.close(code=SOCKET_CREDENTIAL_REVOKED)
            except Exception:  # noqa: BLE001 — it may already be gone
                pass
            return
    except asyncio.CancelledError:
        raise


# ── The request's own token (D-095) ──────────────────────────────────────────
#
# `require_auth` returns a username, and `resolve_principal` builds a Principal
# from it; neither carries the token's `auth_time` or `amr`. Rather than change
# the return type of a dependency ~276 call sites depend on, the decoded
# payload is bound here for the length of the request and read back by
# `principal_from_username`, exactly as `f1_revocation.bind_request_token`
# already keeps the token's identifier for the evidence commit guard. It is
# cleared at the start of every `require_auth`, so one request's token cannot
# be read by the next; a request that never passes through `require_auth` (a
# device token, a test override) has none.

_request_token: ContextVar[Optional[TokenPayload]] = ContextVar("ira_request_token", default=None)


def current_token_payload() -> Optional[TokenPayload]:
    """The access token `require_auth` verified for this request, or None."""
    return _request_token.get()


def current_authentication() -> tuple[Optional[int], tuple[str, ...]]:
    """`(auth_time, amr)` of this request's token; `(None, ())` when unknown."""
    payload = _request_token.get()
    if payload is None:
        return None, ()
    return payload.auth_time, tuple(payload.amr or ())


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Dependency: returns the authenticated username or raises 401.

    Checks (in order):
      1. DEV_MODE bypass (local only)
      2. P5.2: Canary token check (fires CRITICAL then rejects as 401)
      3. JWT signature + expiry
      4. Token type (refresh tokens may not be used for API access)
      5. jti revocation list (Redis)
      6. Per-user token version (revoke-all)

    Note: IP blocklist check (P6.2) runs in the IPBlockMiddleware in main.py,
    before this dependency fires, so blocked IPs never reach this function.
    """
    from core.identity.f1_revocation import clear_request_token, bind_request_token
    clear_request_token()
    _request_token.set(None)
    cfg = get_settings()

    # P2-02: edition-gated. This returns the owner without inspecting a token,
    # so in a multi-tenant deployment it would authenticate every caller as the
    # owner. `dev_mode_active()` is False for anything but the private edition.
    if dev_mode_active():
        return cfg.ira_admin_username

    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # P5.2: canary token check — fires CRITICAL event, then rejects as normal 401
    from utils.canary import check_canary_token
    if await check_canary_token(creds.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(creds.credentials)

    if payload.tok == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cannot be used for API access — use /auth/refresh",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.jti and await _is_revoked(payload.jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await assert_token_version_current(payload)

    from core.editions import is_cloud
    if is_cloud():
        if not payload.jti:
            raise HTTPException(401, 'Token identifier required')
        bind_request_token(payload)
    _request_token.set(payload)
    return payload.sub


def dev_mode_active() -> bool:
    """Whether the DEV_MODE authentication bypass may be honoured (P2-02).

    `DEV_MODE=true` makes `require_auth` return the owner username without
    inspecting a token and `authenticate_user` return True without checking a
    password. On one person's machine that is a convenience. In a multi-tenant
    deployment it is not a degraded state — it is **no authentication at all**,
    for everyone, simultaneously.

    Four call sites honoured the flag and none asked which edition it was
    serving. `config.py::_forbid_dev_mode_on_public_domain` already refuses dev
    mode on a non-local domain, and that is a different control: it constrains
    where the process may *run* and says nothing about which *edition* it
    serves. A cloud deployment reachable on an internal hostname satisfies it.

    **The test is `!= EDITION_PRIVATE`, not `== EDITION_CLOUD`,** and the
    asymmetry with `is_owner` above is deliberate. An authentication bypass
    should be opt-in by name: only the edition explicitly known to be
    single-owner honours it, and cloud, an unknown value or a misconfigured one
    all deny. A third edition added later inherits the safe branch instead of
    the permissive one.
    """
    from core.editions import EDITION_PRIVATE, current_edition

    if current_edition() != EDITION_PRIVATE:
        return False
    return bool(get_settings().dev_mode)


def is_owner(username: str) -> bool:
    """True when `username` is the verified system owner (admin).

    **Never true in the cloud edition, whatever the name is.**

    `ira_admin_username` is one deployment-wide string. A username is
    per-tenant — two tenants' `alice` are different people, which is the same
    property 3C-02 turned into a cache-key collision. Comparing the two is a
    sound authorisation decision in a single-owner deployment and is not one in
    a multi-tenant deployment: it grants deployment-wide owner authority to
    whichever tenant's user happens to hold that name.

    That authority is not decorative. It gates `/backup/download` (a plain
    `pg_dump` of every tenant) and `/backup/restore` (arbitrary SQL applied with
    the database's own credentials, which can drop RLS outright), and it is
    passed into the agent layer as `is_owner=` — the flag that decides which
    tools the model may execute.

    `Principal.is_owner` is the field this comparison was supposed to have been
    replaced by; its docstring has said so since Phase 1. Until the call sites
    migrate, this function fails closed for cloud so the string comparison
    cannot grant anything there.
    """
    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() == EDITION_CLOUD:
        return False
    return secrets.compare_digest(
        username.encode("utf-8"),
        get_settings().ira_admin_username.encode("utf-8"),
    )


def is_voice_service(username: str) -> bool:
    """True when `username` is the trusted voice service.

    **Never true in the cloud edition** — for exactly the reason `is_owner`
    is not, and this one is sharper.

    On a match, `api/routes/chat.py` trusts `is_voice_owner` **from the request
    body** and treats the caller as the owner. So this comparison does not just
    grant what `is_owner` grants; it lets the request assert it. A tenant's user
    holding this name could send `{"is_voice_owner": true}` and receive owner
    tool authority, walking straight around the fix in `is_owner` above.

    The voice service is a single-owner construct: one enrolled biometric
    profile, one owner to verify against. There is no multi-tenant meaning for
    it, so cloud refuses rather than guessing one.
    """
    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() == EDITION_CLOUD:
        return False
    return secrets.compare_digest(
        username.encode("utf-8"),
        get_settings().ira_voice_service_username.encode("utf-8"),
    )


#: The refusal every owner-gated route gives. One string, so a caller cannot
#: tell "you are not the owner" from "this deployment has no owner to be".
OWNER_ACCESS_REQUIRED = "Owner access required"


def require_owner(username: str = Depends(require_auth)) -> str:
    """FastAPI dependency: the authenticated owner, or 403.

    Thirteen route modules each wrote this out by hand as
    `if username != get_settings().ira_admin_username`. Thirteen copies of an
    authorisation rule is thirteen places to miss when the rule changes — and
    the rule just changed, because in cloud the comparison must not grant
    anything. One dependency means the next route to need an owner gate
    inherits the edition check instead of re-deriving it.
    """
    if not is_owner(username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_ACCESS_REQUIRED
        )
    return username


# ── Login endpoint helper ──────────────────────────────────────────────────────

@_lru_cache(maxsize=1)
def _admin_password_hash() -> str:
    """Hash the admin password once on first use and cache it in-process."""
    return hash_password(get_settings().ira_admin_password)


@_lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Stable dummy hash for constant-time comparison when username is wrong."""
    return hash_password("dummy_constant_time_placeholder_xK9mQ2")


def authenticate_user(username: str, password: str) -> bool:
    """Validate credentials against the configured admin account.

    Always runs bcrypt regardless of whether the username matches to prevent
    username enumeration via response-time differences.
    """
    cfg = get_settings()
    # P2-02: edition-gated — this returns True for any password.
    if dev_mode_active():
        return True

    username_matches = secrets.compare_digest(
        username.encode("utf-8"),
        cfg.ira_admin_username.encode("utf-8"),
    )
    hash_to_check = _admin_password_hash() if username_matches else _dummy_hash()
    password_matches = verify_password(password, hash_to_check)
    return username_matches and password_matches
