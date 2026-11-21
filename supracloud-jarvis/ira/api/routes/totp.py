"""
TOTP two-factor authentication endpoints.  # Feat P26

POST /auth/totp/enroll  — re-authenticate, then start an enrolment; returns provisioning URI
POST /auth/totp/verify  — validate a TOTP code; turns a new enrolment on, or
                          completes a replacement started by /enroll
GET  /auth/totp/status  — whether THIS TENANT has a second factor, and which

── Enrolling is re-authenticated (P0-01) ───────────────────────────────────

A bearer access token used to be enough to enrol, and enrolling over an
ENABLED factor overwrote its seed and kept it enabled. So anyone holding the
owner's token — a copied one works as well as the real one for thirty minutes —
could replace the owner's live second factor with their own and lock the owner
out of their account for good.

`/enroll` now asks for the account password every time, checked against the
same credential sign-in checks (`security/reauth.py`). When a factor is already
on it also asks for a current code from that factor: the password alone is what
the factor exists to be more than. Every refused attempt counts against the
account lockout `/auth/token` uses, under the same key, so the two endpoints
share one budget of guesses rather than offering two.

A replacement does not touch the stored factor. `/enroll` returns the new seed
and a short-lived `enrolment_token`; the old factor keeps working until
`/verify` is called with that token and a code from the NEW authenticator, and
only then is the seed replaced (`security/totp_enrolment.py` explains why the
pending seed is held that way). An abandoned replacement simply expires.

── A second factor belongs to a membership, not to a person (D-23) ─────────

Decided in P4-09 and recorded in `OPEN_DECISIONS.md`. A human who belongs to two
organisations holds two enrolments, configured independently, and neither
organisation can see or use the other's. `user_mfa_keys` is keyed per
(tenant, user) by D-17, so the alternative — one factor following the human —
would mean opening key material scoped to a tenant the other has no relationship
with. That is the boundary D-17 exists to hold, so the answer is the one that
does not cross it.

The consequence is a wording obligation, which is why `/status` exists and
reports `tenant_id`: "MFA is on for your account" is a sentence this system
cannot truthfully say. It can say "MFA is on for Acme".

The login endpoint (main.py) checks totp_secrets on every successful password
auth — if an enrolled secret exists the caller must also supply a valid totp_code
form field, or the token is refused.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import pyotp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import principal_from_username
from core.identity.db_context import tenant_connection

logger = logging.getLogger("ira.api.totp")

router = APIRouter(prefix="/auth/totp", tags=["auth"])


class EnrollRequest(BaseModel):
    """What `/enroll` needs beyond the bearer token.

    `password` always. `code` only when a factor is already on: a current code
    from it, which is what proves the request comes from whoever holds it.
    """

    password: str = Field(..., min_length=1, max_length=1024)
    code: Optional[str] = Field(None, max_length=16)


class EnrollResponse(BaseModel):
    provisioning_uri: str
    secret: str  # returned once so the user can save it before scanning QR
    #: True when this enrolment would replace a factor that is on. Nothing has
    #: been replaced yet: send `enrolment_token` with a code from the new
    #: authenticator to `/verify`, within `expires_in` seconds.
    replaces_active_factor: bool = False
    enrolment_token: Optional[str] = None
    expires_in: Optional[int] = None


class VerifyRequest(BaseModel):
    code: str
    #: From `/enroll`, when it is replacing a factor that is on. Without it,
    #: `/verify` checks the stored factor, as it always has.
    enrolment_token: Optional[str] = Field(None, max_length=4096)


class TotpStatus(BaseModel):
    """Deliberately never says "account".

    `scope` is a constant rather than a computed string so a reader of the API
    cannot come away thinking the answer might sometimes be account-wide.
    """

    enrolled: bool
    enabled: bool
    scope: str = "tenant"
    tenant_id: str


def _lock_key(user: str) -> str:
    """The key `/auth/token` counts failures under (auth/routes.py).

    The same normalisation, so a wrong password here and a wrong password at
    sign-in are counted against ONE lockout rather than giving a token holder a
    second, separate budget of guesses.
    """
    return (user or "").strip().lower()


def _refuse(status_code: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, **extra})


_LOCKED = ("Account temporarily locked due to too many failed attempts. "
           "Try again later.")


async def _count_failure(lock_key: str, detail: str, **extra) -> JSONResponse:
    """Record a refused attempt against the account lockout, then refuse.

    The lockout is the same Redis-backed counter sign-in uses
    (`utils/account_lockout.py`), with the same thresholds. It fails soft when
    Redis is down — that policy is sign-in's, and is not changed here.
    """
    from utils.account_lockout import record_failure

    _count, locked = await record_failure(lock_key)
    if locked:
        return _refuse(429, "Account locked after too many failed attempts. "
                            "Try again in 15 minutes.")
    return _refuse(401, detail, **extra)


@router.post("/enroll", response_model=EnrollResponse, status_code=201)
async def totp_enroll(body: EnrollRequest, _user: str = Depends(require_auth)):
    """Re-authenticate, then start an enrolment.

    No factor on yet: store the new seed, not enabled, exactly as before; a
    code from it at `/verify` turns it on. A factor already on: store NOTHING,
    and return the new seed with an `enrolment_token` that `/verify` exchanges,
    with a code from the new authenticator, for the replacement.
    """
    from security import totp as totp_helper
    from security.mfa_crypto import MfaKeyUnavailable, seal_totp_secret
    from security.reauth import ReauthUnavailable, verify_account_password
    from utils.account_lockout import clear_failures, is_locked

    # `user_id` and `tenant_id` come from the resolved Principal — server-side,
    # never from the request body.
    principal = await principal_from_username(_user)
    lock_key = _lock_key(_user)

    # Before bcrypt, as sign-in does: a locked account costs no hashing.
    if await is_locked(lock_key):
        return _refuse(429, _LOCKED)

    try:
        password_ok = await verify_account_password(
            _user, body.password, principal=principal)
    except ReauthUnavailable:
        return _refuse(503, "Your password could not be checked right now, so "
                            "nothing was changed. Try again shortly.")
    if not password_ok:
        return await _count_failure(
            lock_key, "Password confirmation failed. Nothing was changed.",
            reauth="password")

    # The ENABLED factor, if any, and the caller's code against it. An
    # unconfirmed enrolment (`enabled=FALSE`) is not a factor anybody signs in
    # with, so replacing it needs only the password — that is the "started
    # enrolling, lost the QR code" case.
    try:
        verdict, current = await totp_helper.verify_active_factor(
            _user, body.code, principal=principal)
    except totp_helper.TotpUnavailable:
        logger.warning("refusing TOTP enrolment for %r: enrolment state is unknown", _user)
        return _refuse(503, "The current second factor could not be checked, so "
                            "nothing was changed. Try again shortly.")
    if verdict == totp_helper.FACTOR_BAD:
        return await _count_failure(
            lock_key, "A second factor is already on. Enter a current code from "
                      "it to replace it. Nothing was changed.",
            reauth="totp")

    # Password, and the current code where one was owed: the same proof a
    # successful sign-in gives, so the counter resets as it does there.
    await clear_failures(lock_key)

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=_user, issuer_name="SupraCloud IRA")
    # P2-05: encrypted before it is persisted — or, for a replacement, before it
    # is handed back sealed. The plaintext seed is returned to the caller once
    # (they have to scan it) and never written to a column.
    try:
        ciphertext, nonce, key_version = await seal_totp_secret(principal, secret)
    except MfaKeyUnavailable:
        return _refuse(503, "The second-factor key is unavailable, so nothing was "
                            "changed. Try again shortly.")

    if current is not None:
        # A factor is on and its code was just presented. Leave it in place.
        from security.totp_enrolment import (
            EnrolmentUnavailable, issue_reenrolment_token,
        )

        try:
            token, ttl = await issue_reenrolment_token(
                subject=_user, principal=principal, replaces=current,
                ciphertext=ciphertext, nonce=nonce, key_version=key_version)
        except EnrolmentUnavailable:
            return _refuse(503, "The replacement could not be started right now, so "
                                "nothing was changed. Try again shortly.")
        return EnrollResponse(provisioning_uri=uri, secret=secret,
                              replaces_active_factor=True,
                              enrolment_token=token, expires_in=ttl)

    # P2-05. Two defects in the statement this replaces.
    #
    # It ran on a bare `acquire()`. `totp_secrets` forces RLS with policies on
    # `app.user_id` and `app.tenant_id`, so an unbound connection could not
    # satisfy the INSERT's WITH CHECK and the write was refused — and the row
    # carried neither owner column, both of which are now NOT NULL.
    #
    # And `ON CONFLICT (username)` names a constraint that no longer exists:
    # 036 dropped `PRIMARY KEY (username)` precisely so two tenants could each
    # hold an `alice`. The conflict target is the partial unique index, matched
    # with its predicate.
    #
    # P4-09: that target MOVED. `065` rescoped the index from `(user_id)` to
    # `(tenant_id, user_id)` so a user who belongs to two tenants can enrol in
    # each — and this statement still named the old one, which no longer
    # exists. PostgreSQL does not fall back: it raises "there is no unique or
    # exclusion constraint matching the ON CONFLICT specification", so
    # enrolment was broken for EVERY user the moment 065 applied.
    #
    # Nothing caught it because no test drove this route against the post-065
    # schema. The migration had its tests and the route had its own; the PAIR
    # is what broke. `tests/tenancy/test_p4_mfa_is_tenant_scoped.py` now enrols
    # through this route — twice in one tenant, and once in each of two.
    #
    # P0-01: `DO UPDATE ... WHERE totp_secrets.enabled = FALSE`. This statement
    # may replace an enrolment nobody has confirmed, and must never replace one
    # that is on — even if one was turned on between the check above and here.
    # Then nothing is returned, and the request is refused rather than
    # reporting an enrolment it did not make.
    from core.runtime.plaintext_columns import exists

    async with tenant_connection(principal) as conn:
        # `secret` is never written with a seed: it was written as NULL here
        # even before `063`. It IS cleared on a replacement while the column
        # exists, so a legacy plaintext seed does not outlive the enrolment that
        # replaced it. Named only while it exists — `063` drops it, and a
        # statement naming a dropped column raises.
        legacy = await exists(conn, "totp_secrets", "secret")
        written = await conn.fetchval(
            """INSERT INTO totp_secrets
                   (username, secret_ciphertext, secret_nonce,
                    encryption_key_version, encrypted_at, migration_status,
                    user_id, tenant_id)
               VALUES ($1, $2, $3, $4, NOW(), 'ENCRYPTED', $5, $6)
               ON CONFLICT (tenant_id, user_id)
                   WHERE user_id IS NOT NULL AND revoked_at IS NULL
               DO UPDATE SET """ + ("secret = NULL, " if legacy else "") + """
                             secret_ciphertext = EXCLUDED.secret_ciphertext,
                             secret_nonce = EXCLUDED.secret_nonce,
                             encryption_key_version = EXCLUDED.encryption_key_version,
                             encrypted_at = NOW(),
                             migration_status = 'ENCRYPTED',
                             enrolled_at = NOW()
                   WHERE totp_secrets.enabled = FALSE
               RETURNING id""",
            _user, ciphertext, nonce, key_version,
            principal.user_id, principal.tenant_id,
        )
    if written is None:
        return _refuse(409, "A second factor was turned on while this request ran. "
                            "Nothing was changed; start again.")
    return EnrollResponse(provisioning_uri=uri, secret=secret)


@router.post("/verify")
async def totp_verify(body: VerifyRequest, _user: str = Depends(require_auth)):
    """Confirm the user's authenticator app is producing valid codes.

    With an `enrolment_token` from `/enroll`, the code must come from the NEW
    authenticator, and a valid one replaces the factor the token was issued
    against. Without one, the stored factor is checked and turned on.

    A wrong code counts against the account lockout: this endpoint answers
    "is this a valid code?" for whoever holds the token, and an unlimited
    answer to that is a way to find one.
    """
    # Shared, fail-closed checks (security/totp.py) — same logic the Armory's
    # high-risk approval uses, so the two can never drift.
    from security import totp as totp_helper
    from utils.account_lockout import is_locked

    lock_key = _lock_key(_user)
    if await is_locked(lock_key):
        return _refuse(429, _LOCKED)

    if body.enrolment_token:
        return await _confirm_replacement(body, _user, lock_key)

    try:
        enrolled = await totp_helper.is_enrolled(_user)
    except totp_helper.TotpUnavailable:
        return _refuse(503, "The second factor could not be checked. Try again shortly.")
    if not enrolled:
        raise HTTPException(status_code=400, detail="TOTP not enrolled — call /auth/totp/enroll first")
    if not await totp_helper.verify(_user, body.code):
        return await _count_failure(lock_key, "Invalid or expired TOTP code")
    # Fix P29: a valid code activates the second factor — login won't enforce until this runs
    principal = await principal_from_username(_user)
    async with tenant_connection(principal) as conn:
        await conn.execute(
            "UPDATE totp_secrets SET enabled=TRUE WHERE username=$1", _user
        )
    return {"verified": True, "enabled": True}


async def _confirm_replacement(body: VerifyRequest, user: str, lock_key: str):
    """Swap in the seed `/enroll` sealed — only if a code from it is valid."""
    from core.runtime.plaintext_columns import exists
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret
    from security.totp_enrolment import (
        EnrolmentTokenInvalid, EnrolmentUnavailable, read_reenrolment_token,
    )

    principal = await principal_from_username(user)
    try:
        pending = await read_reenrolment_token(
            body.enrolment_token or "", subject=user, principal=principal)
    except EnrolmentUnavailable:
        return _refuse(503, "The replacement could not be checked right now, so "
                            "nothing was changed. Try again shortly.")
    except EnrolmentTokenInvalid:
        raise HTTPException(
            status_code=400,
            detail="This replacement has expired or was not started by this "
                   "account. Start it again from /auth/totp/enroll.")

    code = (body.code or "").strip()

    def _check(secret: str) -> bool:
        return bool(code) and bool(pyotp.TOTP(secret).verify(code, valid_window=1))

    try:
        ok = await use_totp_secret(principal, pending.ciphertext, pending.nonce,
                                   pending.key_version, _check)
    except MfaKeyUnavailable:
        return _refuse(503, "The second-factor key is unavailable, so nothing was "
                            "changed. Try again shortly.")
    if not ok:
        return await _count_failure(lock_key, "Invalid or expired TOTP code")

    async with tenant_connection(principal) as bound:
        # `tenant_connection` is typed as yielding `object`; this is an asyncpg
        # connection, and saying so keeps the type gate's count where it was.
        conn: Any = bound
        legacy = await exists(conn, "totp_secrets", "secret")
        # Compare-and-swap on the factor the token was issued against. The
        # sealed bytes are written as they came back: they were sealed for this
        # tenant, user and key version, and `use_totp_secret` just opened them
        # under exactly those.
        replaced = await conn.fetchval(
            "UPDATE totp_secrets SET "
            + ("secret = NULL, " if legacy else "") +
            "       secret_ciphertext = $1, secret_nonce = $2,"
            "       encryption_key_version = $3, encrypted_at = NOW(),"
            "       migration_status = 'ENCRYPTED', enrolled_at = NOW(),"
            "       enabled = TRUE"
            " WHERE id = $4 AND user_id = $5 AND tenant_id = $6"
            "   AND enabled = TRUE AND revoked_at IS NULL AND enrolled_at = $7"
            " RETURNING id",
            pending.ciphertext, pending.nonce, pending.key_version,
            uuid.UUID(pending.replaces_row_id), principal.user_id,
            principal.tenant_id, pending.replaces_enrolled_at,
        )
    if replaced is None:
        return _refuse(409, "Your second factor changed after this replacement "
                            "began, so it was not applied. Start again.")
    return {"verified": True, "enabled": True, "replaced": True}


@router.get("/status", response_model=TotpStatus)
async def totp_status(_user: str = Depends(require_auth)) -> TotpStatus:
    """Whether the caller has a second factor **in the tenant they are in**.

    The question "is MFA on for this user" has no answer in this system, and a
    surface that appeared to give one would be the thing D-23 warns about: a
    person reads "enabled", believes they are protected everywhere, and is not.

    `tenant_id` is echoed so a client cannot render the answer without saying
    which organisation it is about.
    """
    principal = await principal_from_username(_user)
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT enabled FROM totp_secrets "
            " WHERE user_id = $1 AND revoked_at IS NULL",
            principal.user_id)
    return TotpStatus(
        enrolled=row is not None,
        enabled=bool(row["enabled"]) if row is not None else False,
        tenant_id=str(principal.tenant_id),
    )
