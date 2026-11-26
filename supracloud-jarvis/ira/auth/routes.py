"""
Auth endpoints — /auth/token, /auth/logout, /auth/logout/all, /auth/refresh.

Registered directly on the app (not an APIRouter) exactly as they were in
`main.py`, so paths, decorators and rate limits are byte-for-byte unchanged.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Form, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
# OAuth2PasswordRequestForm is imported at MODULE scope. Under
# `from __future__ import annotations` FastAPI resolves the `form:` annotation as a
# ForwardRef against module globals; a local import leaves it unresolved -> the
# /auth/token route raises TypeError("ForwardRef(...) is not callable") at startup.
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _Creds

from api.middleware.auth import (
    authenticate_user, create_login_tokens,
    decode_token, require_auth, revoke_token, bump_token_version,
)
from utils.canary import check_canary_username

logger = logging.getLogger("ira.auth.routes")

_bearer_dep = _HTTPBearer(auto_error=False)


class StepUpRequest(BaseModel):
    """`POST /auth/step-up` (D-095): the account password, and a current code
    when the account has a second factor. Never logged."""
    password: str
    totp_code: str | None = None


def _public_base_url() -> str:
    """Where a link should point.

    Read from configuration rather than from the request, and that is a security
    decision rather than a convenience one. Building the link from `Host` or
    `X-Forwarded-Host` lets an attacker who can set those send a reset link that
    points at their own server — the user clicks it, the token goes to them, and
    the mail came from the real service. It is a well-worn hole and the fix is
    to never read the request at all.
    """
    import os

    configured = (os.getenv("IRA_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return configured or "https://localhost"


async def _send_reset_link(address: str, token: str) -> None:
    """Mail a reset link. Never raises, never logs the token."""
    from core.notify.mail import Message, send
    from urllib.parse import quote

    link = f"{_public_base_url()}/auth/reset?token={quote(token)}"
    delivery = await send(Message(
        to=address,
        subject="Reset your password",
        body=(
            "Somebody asked to reset the password for this account.\n\n"
            f"{link}\n\n"
            "The link works once and expires in an hour. If this was not you, "
            "nothing has changed and you can ignore this message — but if you "
            "did not expect it, consider that somebody knows your address.\n"
        ),
    ))
    # The reason is written by `core.notify.mail` and never carries the body.
    if not delivery.delivered:
        logger.warning("a password reset link could not be delivered: %s",
                       delivery.reason)


async def _send_verification_link(address: str, token: str) -> None:
    """Mail a verification link. Never raises, never logs the token."""
    from core.notify.mail import Message, send
    from urllib.parse import quote

    link = f"{_public_base_url()}/auth/verify?token={quote(token)}"
    delivery = await send(Message(
        to=address,
        subject="Confirm your email address",
        body=(
            "Confirm this address to finish setting up your account.\n\n"
            f"{link}\n\n"
            "If you did not create an account, you can ignore this message.\n"
        ),
    ))
    if not delivery.delivered:
        logger.warning("a verification link could not be delivered: %s",
                       delivery.reason)


def register_auth_routes(app: FastAPI, limiter) -> None:
    """Attach the auth endpoints to the app using the shared rate limiter."""

    @app.post("/auth/token", tags=["auth"], summary="Get JWT access + refresh tokens")
    @limiter.limit("5/minute")   # app-layer brute-force protection (nginx may be bypassed)
    async def login(
        request: Request,
        form: OAuth2PasswordRequestForm = Depends(OAuth2PasswordRequestForm),
        totp_code: str | None = Form(None),  # optional TOTP field
    ):
        from utils.account_lockout import is_locked, record_failure, clear_failures

        # P5.2: ghost-username tripwire — fires CRITICAL event, still returns 401
        source_ip = request.client.host if request.client else None
        if await check_canary_username(form.username, source_ip=source_ip):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"},
            )

        # One name for every security decision below. Delegated accounts are
        # matched case- and space-insensitively (`authenticate_delegated`), so
        # " Alice", "ALICE" and "alice" are one account. Keyed on the raw form,
        # each spelling had its own lockout counter, found no second-factor
        # enrolment (the factor was skipped), and was issued a token whose
        # `sub` neither revocation nor "log out everywhere" reaches. The owner
        # is matched exactly, so the owner's name is used as typed.
        _typed = form.username or ""
        _lock_key = _typed.strip().lower()

        # P2.3: check lockout BEFORE running bcrypt (avoid unnecessary CPU)
        if await is_locked(_lock_key):
            return JSONResponse(
                status_code=429,
                content={"detail": "Account temporarily locked due to too many failed attempts. Try again later."},
            )

        # Owner first; delegated accounts (PR #67) are a fallback with their own
        # bcrypt hashes. The scope middleware default-denies their tokens on
        # everything except the mapped chat/voice routes.
        _authed = authenticate_user(_typed, form.password)
        _subject = _typed
        if not _authed:
            from security.delegated import authenticate_delegated
            _authed = await authenticate_delegated(_typed, form.password)
            _subject = _lock_key
        if not _authed:
            _count, _locked = await record_failure(_lock_key)
            if _locked:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Account locked after too many failed attempts. Try again in 15 minutes."},
                )
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"},
            )

        # ── Second factor (P2-05) ───────────────────────────────────────────
        #
        # This used to read the secret store here, on an unbound pool
        # connection. That table has FORCE ROW LEVEL SECURITY with policies on
        # `app.user_id` and `app.tenant_id`, and an unbound connection leaves
        # both NULL — so the query matched ZERO rows for everybody, the result
        # was always None, and **the challenge below never ran**. Anyone who had
        # enrolled a second factor was logging in on a password alone.
        #
        # (Phrased without naming the table or the raw accessor on purpose: the
        # static guard in tests/tenancy/test_p205_totp_tenancy.py matches on
        # that pair, and a comment describing the defect should not read as the
        # defect. The guard stays strict; the prose moves.)
        #
        # Two things changed. The lookup is bound to a real principal so it can
        # see the row, and an *undeterminable* enrolment state now refuses the
        # login rather than skipping the factor. The old shape treated every
        # failure as "no second factor configured", which is the fail-open
        # direction on the one check whose entire purpose is to be an obstacle.
        from security.totp import (
            FACTOR_BAD, NOT_ENROLLED, TotpUnavailable, check_login_second_factor,
        )

        try:
            _factor = await check_login_second_factor(_subject, totp_code)
        except TotpUnavailable:
            logger.warning("refusing login for %r: TOTP enrolment state is unknown",
                           _subject)
            return JSONResponse(
                status_code=503,
                content={"detail": "Cannot verify second-factor enrolment — "
                                   "refusing to sign in without it."},
            )
        if _factor == FACTOR_BAD:
            await record_failure(_lock_key)
            # `second_factor` is the machine-readable form of the detail, so a
            # client can ask for a code without matching on prose (P0-01: the
            # web sign-in had no code step and read this as a wrong password).
            # It tells the caller nothing the distinct detail did not: both are
            # reached only behind a correct password.
            return JSONResponse(
                status_code=401,
                content={"detail": "TOTP code required or invalid",
                         "second_factor": "totp"},
            )
        # NOT_ENROLLED falls through — a user without a second factor signs in on
        # a password, which is the whole point of the factor being optional.
        assert _factor in (NOT_ENROLLED, "ok")

        # Successful login — clear the failure counter
        await clear_failures(_lock_key)
        # Record for the Trust Console (fail-soft: login never breaks on Redis)
        try:
            from datetime import datetime, timezone
            from api.routes.trust import LAST_LOGIN_KEY
            from utils.redis_client import get_redis
            await get_redis().set(LAST_LOGIN_KEY, datetime.now(timezone.utc).isoformat())
        except Exception:
            pass
        # What this login checked, stamped on the tokens (D-095): the password,
        # and the one-time code when the factor was verified.
        _amr = ("pwd", "otp") if _factor == "ok" else ("pwd",)
        return await create_login_tokens(_subject, amr=_amr)

    @app.post("/auth/logout", tags=["auth"], summary="Revoke the current access token")
    async def logout(
        request: Request,
        creds: _Creds | None = Depends(_bearer_dep),
    ):
        """Add the token's jti to the Redis revocation list so it can't be reused."""
        from datetime import datetime, timezone
        if creds is None:
            return JSONResponse(status_code=401, content={"detail": "No token provided"})
        try:
            payload = decode_token(creds.credentials)
        except Exception:
            # An invalid or expired token has nothing to revoke; same answer as
            # success, so this endpoint is not an oracle for token validity.
            return {"message": "Logged out"}
        if payload.jti:
            remaining = int((payload.exp - datetime.now(timezone.utc)).total_seconds())
            try:
                await revoke_token(payload.jti, max(1, remaining))
            except Exception:  # noqa: BLE001
                # The token decoded, so it was valid: saying "Logged out" here
                # would tell the user a token is dead while it still works. A
                # 503 reveals nothing the bearer cannot already learn by using
                # the token, and it is retryable.
                logger.exception("logout: token revocation failed")
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Sign-out could not be completed. Please try again."},
                )
        return {"message": "Logged out"}

    @app.post("/auth/logout/all", tags=["auth"], summary="Revoke ALL tokens for the current user")
    async def logout_all(
        request: Request,
        creds: _Creds | None = Depends(_bearer_dep),
    ):
        """Bump the per-account token version, invalidating every token issued so far.

        Only a **current access token** may do this. It used to decode the
        bearer and bump straight away, so a refresh token, a revoked token or
        one from before the last "log out everywhere" could sign the account
        out everywhere, again and again. The checks are `require_auth`'s, and
        success is reported only after the new version is committed to
        PostgreSQL (migration 096).
        """
        from api.middleware.auth import _is_revoked, assert_token_version_current

        if creds is None:
            return JSONResponse(status_code=401, content={"detail": "No token provided"})
        payload = decode_token(creds.credentials)
        if payload.tok != "access":
            return JSONResponse(
                status_code=401,
                content={"detail": "Use an access token to sign out everywhere"})
        if payload.jti and await _is_revoked(payload.jti):
            return JSONResponse(status_code=401, content={"detail": "Token has been revoked"})
        await assert_token_version_current(payload)
        await bump_token_version(payload.sub, payload.uid)

        # Batch 8. Bumping the version invalidates every ACCESS token; it does
        # nothing to the refresh tokens, which is what P2-04 had to fix on the
        # refresh path itself. In the cloud edition the sessions go too, so
        # "sign out everywhere" means the thing it says rather than "sign out
        # everywhere for thirty minutes".
        from core.editions import EDITION_CLOUD, current_edition

        if current_edition() == EDITION_CLOUD:
            try:
                from core.identity.resolver import resolve_principal
                from core.identity.sessions import revoke_all_sessions

                await revoke_all_sessions(
                    await resolve_principal(payload.sub, edition="cloud"),
                    reason="signed out everywhere")
            except Exception:  # noqa: BLE001
                # The version bump already happened and already invalidated the
                # access tokens, so reporting failure here would be misleading
                # in the other direction. Logged loudly: a revocation that only
                # half happened is worth knowing about.
                logger.exception("could not revoke sessions on sign-out-everywhere")

        return {"message": f"All tokens for {payload.sub!r} have been invalidated"}

    @app.post("/auth/refresh", tags=["auth"], summary="Exchange a refresh token for a new access token")
    @limiter.limit("20/minute")
    async def refresh_token_endpoint(
        request: Request,
        creds: _Creds | None = Depends(_bearer_dep),
    ):
        """Verify the refresh token and issue a fresh short-lived access token."""
        from datetime import datetime, timezone
        from api.middleware.auth import (
            _is_revoked, _get_token_version, _make_access_token,
            assert_token_version_current,
        )
        if creds is None:
            return JSONResponse(status_code=401, content={"detail": "No token provided"})
        payload = decode_token(creds.credentials)
        if payload.tok != "refresh":
            return JSONResponse(status_code=400, content={"detail": "Not a refresh token"})
        if payload.jti and await _is_revoked(payload.jti):
            return JSONResponse(status_code=401, content={"detail": "Refresh token revoked"})
        # P2-04. Without this the endpoint checked only the jti blocklist, so a
        # refresh token issued BEFORE /auth/logout/all still minted a new access
        # token — stamped with the version current at that moment, and therefore
        # indistinguishable downstream from one issued by a fresh login. The
        # revocation invalidated the 30-minute credentials and left the 7-day one
        # that replaces them.
        await assert_token_version_current(payload)

        # Batch 8. In the cloud edition the refresh token's SESSION must still
        # be live. Without this check the session table is decoration: a person
        # ends a session in the list, the row says revoked, and the refresh
        # token in somebody else's hands keeps minting access tokens for seven
        # days. Revocation has to be enforced where the token is spent.
        #
        # Cloud only, and that is not a gap. The private edition has one owner
        # and no session list to end anything from, so requiring a row there
        # would refuse every refresh on a deployment that never writes one.
        #
        # A missing session refuses. That is the whole point — "no row" is the
        # state of a token that was revoked and erased, or never recorded, and
        # both should stop working.
        from core.editions import EDITION_CLOUD, current_edition

        if current_edition() == EDITION_CLOUD and payload.jti:
            from core.identity.resolver import resolve_principal
            from core.identity.sessions import (
                SessionUnavailable, session_is_live, touch_session,
            )

            try:
                principal = await resolve_principal(payload.sub, edition="cloud")
                live = await session_is_live(principal, payload.jti)
            except SessionUnavailable:
                # "I cannot say" is not "this session was revoked". A store
                # outage must not sign everybody out, and must not mint tokens
                # either — 503 says retry, which is the truthful answer.
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Sessions are temporarily unavailable."},
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not establish the session for a refresh")
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Sessions are temporarily unavailable."},
                )

            if not live:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "This session has ended."},
                )
            await touch_session(principal, payload.jti)
        else:
            # The private edition's counterpart: a household account's refresh
            # token is spent only while the account is still active. Revoking
            # an account used to leave its refresh token minting access tokens
            # for the rest of its seven days. The owner is not a delegated
            # account and has no row to consult.
            from api.middleware.auth import is_owner
            from security.delegated import get_delegated_role

            if not is_owner(payload.sub) and await get_delegated_role(payload.sub) is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "This account is no longer active."},
                )

        ver = await _get_token_version(payload.sub, payload.uid)
        # The tenant SELECTION is carried forward. Dropping it would silently
        # return a multi-tenant session to "nothing selected", which resolves to
        # AmbiguousTenant — the user would be forced to pick their organisation
        # again every thirty minutes. It is carried as a selector, not as
        # authority: the new token's `tid` is re-verified against membership on
        # every request exactly as the old one's was, so refreshing cannot
        # preserve access that has since been revoked.
        #
        # `auth_time` and `amr` are COPIED from the refresh token, never
        # re-stamped (D-095). A refresh proves possession of a refresh token,
        # not that the person is still there; stamping the present moment would
        # make a session kept alive for a week indistinguishable from a sign-in
        # a minute ago, which is exactly what step-up has to tell apart. A
        # refresh token issued before the claim existed carries none, and the
        # new access token says so too.
        access_token, _, access_exp = _make_access_token(
            payload.sub, ver=ver, tid=payload.tid, uid=payload.uid,
            auth_time=payload.auth_time, amr=payload.amr)
        now = datetime.now(timezone.utc)
        from api.middleware.auth import TokenResponse as _TR
        return _TR(
            access_token=access_token,
            expires_in=max(0, int((access_exp - now).total_seconds())),
        )

    # ── Step-up authentication (D-095) ──────────────────────────────────────
    #
    # A session proves that somebody signed in once. For a deployment
    # administrator about to issue, approve or revoke an instruction to a watch
    # (care/deviceadmin.py), the server asks for proof that the person is still
    # there NOW: the password of the token's own subject, and a current code
    # when the account has a second factor. What comes back is a fresh ACCESS
    # token for the same session with `auth_time` set to this moment; no refresh
    # token, so the recent proof cannot be kept alive by refreshing it.

    @app.post("/auth/step-up", tags=["auth"],
              summary="Prove the account password (and second factor) again for a recent-authentication token")
    @limiter.limit("5/minute")   # the same budget as /auth/token: this checks a password
    async def step_up(
        request: Request,
        body: StepUpRequest,
        user: str = Depends(require_auth),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    ):
        from datetime import datetime, timezone

        from api.middleware.auth import (
            TokenPayload, _make_access_token, current_token_payload,
        )
        from api.middleware.workspace import principal_from_username
        from core.identity import auth_audit
        from security.reauth import ReauthUnavailable, verify_account_password
        from security.totp import (
            FACTOR_BAD, FACTOR_OK, NOT_ENROLLED, TotpUnavailable, verify_active_factor,
        )
        from utils.account_lockout import clear_failures, is_locked, record_failure

        def _refuse(status_code: int, detail: str, **extra):
            return JSONResponse(status_code=status_code, content={"detail": detail, **extra})

        current: TokenPayload | None = current_token_payload()
        if current is None:
            # No token was verified for this request (DEV_MODE answers without
            # one). A step-up token is minted FROM a session token, so there is
            # nothing to mint it from.
            return _refuse(401, "Step-up needs a session token.")

        # The account is the TOKEN'S subject. The body cannot name one, so a
        # correct password for some other account proves nothing here.
        principal = await principal_from_username(user, x_tenant_id)
        lock_key = (user or "").strip().lower()

        async def _count_failure(detail: str, **extra):
            # The same Redis-backed counter and thresholds as /auth/token and
            # /auth/totp/enroll, under the same key: one budget of guesses for
            # the account, not a fresh one per endpoint. The attempt is logged
            # without the password or the code.
            _count, locked = await record_failure(lock_key)
            logger.warning("step-up refused for user=%s reason=%s", principal.user_id, extra.get("reauth", "password"))
            await auth_audit.record(auth_audit.SIGN_IN_REFUSED, user_id=principal.user_id,
                                    tenant_id=principal.tenant_id, subject=user,
                                    ip_address=request.client.host if request.client else None,
                                    user_agent=request.headers.get("user-agent", ""),
                                    detail="step-up refused: " + extra.get("reauth", "password"))
            if locked:
                return _refuse(429, "Account locked after too many failed attempts. Try again in 15 minutes.")
            return _refuse(401, detail, **extra)

        # Before bcrypt, as sign-in does: a locked account costs no hashing.
        if await is_locked(lock_key):
            return _refuse(429, "Account temporarily locked due to too many failed attempts. Try again later.")

        try:
            password_ok = await verify_account_password(user, body.password, principal=principal)
        except ReauthUnavailable:
            return _refuse(503, "Your password could not be checked right now. Try again shortly.")
        if not password_ok:
            return await _count_failure("Password not accepted.", reauth="password")

        # A current code is REQUIRED when a factor is enabled: a password alone
        # is what the factor exists to be more than. "Cannot tell whether a
        # factor is enrolled" refuses (503), never skips the factor.
        try:
            verdict, _factor = await verify_active_factor(user, body.totp_code, principal=principal)
        except TotpUnavailable:
            return _refuse(503, "Your second factor could not be checked right now. Try again shortly.")
        if verdict == FACTOR_BAD:
            return await _count_failure("A current second-factor code is required.",
                                        reauth="totp", second_factor="totp")
        assert verdict in (NOT_ENROLLED, FACTOR_OK)
        amr = ["pwd", "otp"] if verdict == FACTOR_OK else ["pwd"]

        await clear_failures(lock_key)
        now = datetime.now(timezone.utc)
        auth_time = int(now.timestamp())
        # Same subject, tenant selection, account id and version as the token
        # presented: this is the same session, re-proven, not a new one. The
        # expiry policy is the ordinary access-token one. No refresh token.
        access_token, _, access_exp = _make_access_token(
            current.sub, ver=current.ver, tid=current.tid, uid=current.uid,
            auth_time=auth_time, amr=amr)
        await auth_audit.record(auth_audit.SIGN_IN_SUCCEEDED, user_id=principal.user_id,
                                tenant_id=principal.tenant_id, subject=user,
                                ip_address=request.client.host if request.client else None,
                                user_agent=request.headers.get("user-agent", ""),
                                detail="step-up: " + ",".join(amr))
        logger.info("step-up succeeded for user=%s amr=%s", principal.user_id, ",".join(amr))
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": max(0, int((access_exp - now).total_seconds())),
            "auth_time": auth_time,
            "amr": amr,
        }

    # ── Registration and email verification (Phase 2 batch 2) ───────────────
    #
    # Registered here rather than in a router module so that every
    # unauthenticated credential endpoint shares one file and one rate limiter.
    # Both are OFF unless `IRA_CLOUD_REGISTRATION_ENABLED` is set: building the
    # capability and opening it to the public internet are separate decisions.

    @app.post("/auth/register", tags=["auth"], summary="Create an account")
    @limiter.limit("3/minute")   # unauthenticated and it creates rows
    async def register(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
    ):
        from core.identity.registration import (
            RegistrationDisabled, RegistrationError, register_account,
        )

        try:
            outcome = await register_account(email, password)
        except RegistrationDisabled:
            return JSONResponse(
                status_code=404,
                content={"detail": "Not found"},
            )
        except RegistrationError as exc:
            # Only password/format complaints reach here; none of them depends
            # on whether the address exists.
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("registration failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Registration is temporarily unavailable."},
            )

        if outcome.created and outcome.token and outcome.email:
            # Batch 9. The one place the token is handed on, and it now goes
            # somewhere. `_send_verification_link` never raises and never logs
            # the token — a verification link in a log file is a bearer
            # credential for that address, which is what the previous version of
            # this comment was protecting when it dropped the token silently.
            #
            # It sends to `outcome.email`, the address the DATABASE holds,
            # rather than the string the caller typed.
            await _send_verification_link(outcome.email, outcome.token)

        # Identical for a new address and an existing one. `outcome.created`
        # must never reach this body — see core/identity/registration.py.
        return JSONResponse(
            status_code=202,
            content={"detail": "If that address can be registered, "
                               "a verification email has been sent."},
        )

    # ── Cloud login (Phase 2 batch 3) ───────────────────────────────────────
    #
    # Separate from `/auth/token` rather than folded into it, and the separation
    # is deliberate. `/auth/token` is the private edition's owner login: it
    # carries the canary tripwire, the .env admin comparison, the delegated-
    # account fallback and username-keyed lockout, none of which have a cloud
    # meaning. Threading a second identity model through it would put the
    # deployment owner's login path one `if` away from every cloud change.
    #
    # Two paths, one token system: both end at `create_login_tokens`, so
    # `/auth/refresh`, `/auth/logout` and `/auth/logout/all` serve cloud
    # sessions without knowing they exist. There is no second JWT stack.

    @app.post("/auth/cloud/login", tags=["auth"],
              summary="Sign in to a cloud account")
    @limiter.limit("5/minute")   # same budget as /auth/token; P2-03 key_func
    async def cloud_login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        totp_code: str | None = Form(None),
        recovery_code: str | None = Form(None),
    ):
        """Email + password → access and refresh tokens for a cloud account.

        The route deliberately accepts **no tenant, workspace, role or scope
        input**. There is no parameter to forge: authority is resolved from the
        database after the password is verified, and the token carries only a
        subject. `test_request_supplied_tenant_and_role_are_ignored` sends them
        anyway and asserts nothing changes.
        """
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.cloud_login import (
            CloudLoginRefused, CloudLoginUnavailable, CloudMfaRequired,
            authenticate_cloud_user,
        )

        # 404, not 403, and not "disabled". A private deployment has no cloud
        # accounts, so this endpoint has nothing to offer there — and an
        # authentication surface that announces itself on a deployment where it
        # cannot work is an invitation to probe it. Same reasoning as
        # /auth/register's 404, and the same reasoning as `dev_mode_active`
        # testing `!= EDITION_PRIVATE`: the edition gate is the real control.
        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        try:
            outcome = await authenticate_cloud_user(
                email, password, totp_code=totp_code,
                recovery_code=recovery_code,
            )
        except CloudMfaRequired:
            # Distinguishable, and only reachable behind a correct password —
            # see core/identity/cloud_login.py. A client cannot be told to
            # prompt for a code without being told a code is wanted.
            return JSONResponse(
                status_code=401,
                content={"detail": "A valid second-factor code is required."},
            )
        except CloudLoginRefused:
            # One response for: unknown address, unverified address, wrong
            # password, missing credential, suspended account, deleted account,
            # no access-granting membership, and several memberships.
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"},
            )
        except CloudLoginUnavailable:
            # Cannot determine identity → refuse. Never a 200, and never the
            # same answer as "wrong password", so an outage is diagnosable
            # without being an oracle: reaching this needs no valid credential,
            # so it discloses nothing about any account.
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication is temporarily unavailable."},
            )
        except Exception:  # noqa: BLE001
            # `logger.exception` records the type and traceback. The submitted
            # password is not in scope for the formatter and is never passed to
            # it; nothing here interpolates a credential.
            logger.exception("cloud login failed unexpectedly")
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication is temporarily unavailable."},
            )

        # `outcome.subject` is the database's stored normalised email, not the
        # string the caller typed. It becomes the token's `sub`, and therefore
        # the value every later request re-resolves — so what a token identifies
        # is what the identity register holds.
        tokens = await create_login_tokens(outcome.subject, amr=outcome.amr)

        # Batch 8. Record the refresh token as a session, so the person can see
        # where they are signed in and end one of those places.
        #
        # The jti comes from decoding the token that was just minted rather than
        # from a new return value: it is the token's own claim, the client
        # already holds it, and threading it out of `create_login_tokens` would
        # put an internal identifier into the response model of both editions
        # for the benefit of one.
        #
        # A failure here does NOT fail the login. The session row is a record of
        # an authentication that has already succeeded — refusing the login
        # because the record could not be written would turn a bookkeeping
        # outage into an inability to sign in. What it does mean is that the
        # refresh path will not find a session, and `/auth/refresh` refuses
        # without one: the failure closes, it just closes later.
        try:
            from api.middleware.auth import decode_token as _decode
            from core.identity.resolver import resolve_principal
            from core.identity.sessions import record_session
            from core.runtime.client_ip import client_ip

            claims = _decode(tokens.refresh_token)
            await record_session(
                await resolve_principal(outcome.subject, edition="cloud"),
                refresh_jti=claims.jti,
                expires_at=claims.exp,
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        except Exception:  # noqa: BLE001 — see above
            logger.warning("could not record a session for a successful login",
                           exc_info=True)

        return tokens

    # ── MFA recovery codes (Phase 2 batch 7) ────────────────────────────────
    #
    # The login field above is only useful if a user can obtain a set, so both
    # halves ship together. A recovery capability whose codes cannot be issued
    # is a dead parameter, and a dead parameter on an authentication path is
    # worse than none: it reads, to anybody auditing the surface, as a working
    # way back in.
    #
    # Both endpoints require an ordinary authenticated session, which is not a
    # weakness. Recovery codes are for the person who still knows their
    # password and has lost their authenticator; issuing them needs a live
    # session precisely because the alternative — issuing them to somebody who
    # cannot currently sign in — is the account-takeover path this whole
    # mechanism exists to avoid.

    @app.post("/auth/cloud/mfa/recovery-codes", tags=["auth"],
              summary="Issue a new set of MFA recovery codes")
    @limiter.limit("3/hour")
    async def issue_recovery_codes(
        request: Request,
        user: str = Depends(require_auth),
    ):
        """Issue a fresh set, invalidating every earlier one.

        The plaintext codes are returned **once** and are not stored, so they
        cannot be shown again — regenerating is the only way back to a usable
        set. The rate limit is deliberately tight: this is the one call that
        can quietly retire a user's remaining codes, so an attacker with a
        stolen session should not be able to churn through it.
        """
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal
        from security.mfa_recovery import RecoveryUnavailable, generate_recovery_codes

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        try:
            principal = await resolve_principal(user, edition="cloud")
            issued = await generate_recovery_codes(principal)
        except CloudIdentityNotAvailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Identity is temporarily unavailable."},
            )
        except RecoveryUnavailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Recovery codes could not be issued."},
            )
        except Exception:  # noqa: BLE001 — the codes are never in the message
            logger.exception("recovery code issuance failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Recovery codes could not be issued."},
            )

        # The only place these values are ever transmitted. They are not
        # logged here or in `generate_recovery_codes`, and
        # `test_codes_are_never_logged` asserts it.
        return {
            "generation": issued.generation,
            "codes": list(issued.codes),
            "detail": "Store these now. They cannot be shown again.",
        }

    @app.get("/auth/cloud/mfa/recovery-codes", tags=["auth"],
             summary="How many MFA recovery codes remain")
    @limiter.limit("30/minute")
    async def count_recovery_codes(
        request: Request,
        user: str = Depends(require_auth),
    ):
        """The count only — never the codes, not even a prefix of one."""
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal
        from security.mfa_recovery import remaining_recovery_codes

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        try:
            principal = await resolve_principal(user, edition="cloud")
            remaining = await remaining_recovery_codes(principal)
        except CloudIdentityNotAvailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Identity is temporarily unavailable."},
            )
        except Exception:  # noqa: BLE001
            logger.exception("recovery code count failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Recovery codes are temporarily unavailable."},
            )

        return {"remaining": remaining}

    # ── Sessions and devices (Phase 2 batch 8) ──────────────────────────────
    #
    # Read-and-revoke only. There is no endpoint that creates a session or a
    # device from request input: sessions are created by signing in, and a
    # device is registered by the client that owns it. An endpoint that let a
    # caller mint either would be an endpoint for writing rows into somebody
    # else's account list.

    async def _cloud_principal(user: str):
        """The caller's own principal, or a JSONResponse explaining why not."""
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        try:
            return await resolve_principal(user, edition="cloud")
        except CloudIdentityNotAvailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Identity is temporarily unavailable."})

    @app.get("/auth/cloud/sessions", tags=["auth"],
             summary="Where this account is signed in")
    @limiter.limit("30/minute")
    async def list_own_sessions(
        request: Request,
        user: str = Depends(require_auth),
    ):
        """The caller's own sessions, newest first.

        Revoked ones are included rather than hidden: a list that silently drops
        entries cannot answer "did my revocation work", which is the question
        somebody asks straight after using it.

        ── `is_current` is not marked here, deliberately ───────────────────

        A session is keyed on the REFRESH token's `jti`, and this endpoint —
        like every other authenticated endpoint — is reached with an ACCESS
        token, which carries a different one. So the request genuinely does not
        know which row it is.

        The three ways to close that all cost more than the label is worth
        today: accept the refresh token as a second credential on a read
        endpoint, put it in a query string, or add a `sid` claim to the access
        token. The last is the right answer and is a change to the token
        structure shared with the private edition, which is Batch 11's business
        rather than something to slip in here.

        What is NOT done is guessing — marking the most recently seen row, or
        the newest. The entire purpose of this list is to find the session that
        is not you, and a wrong "this is you" label points somebody away from
        exactly the row they are looking for. `list_sessions` takes
        `current_jti` and marks correctly when it is given one; the caller that
        can supply it does not exist yet.
        """
        from core.identity.sessions import list_sessions

        principal = await _cloud_principal(user)
        if isinstance(principal, JSONResponse):
            return principal

        try:
            sessions = await list_sessions(principal)
        except Exception:  # noqa: BLE001
            logger.exception("could not list sessions")
            return JSONResponse(
                status_code=503,
                content={"detail": "Sessions are temporarily unavailable."})

        return {"sessions": [
            {
                "id": str(s.session_id),
                "device_id": str(s.device_id) if s.device_id else None,
                "issued_at": s.issued_at.isoformat() if s.issued_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
                "revoked_reason": s.revoked_reason,
                "user_agent": s.user_agent,
                "is_current": s.is_current,
            }
            for s in sessions
        ]}

    @app.post("/auth/cloud/sessions/{session_id}/revoke", tags=["auth"],
              summary="End one session")
    @limiter.limit("30/minute")
    async def revoke_own_session(
        request: Request,
        session_id: str,
        user: str = Depends(require_auth),
    ):
        """End one of the caller's own sessions.

        The same answer for "not found" and "not yours", because under RLS they
        ARE the same answer — and keeping them the same is what stops this
        becoming a way to test whether a session id exists.
        """
        import uuid as _uuid

        from core.identity.sessions import revoke_session

        principal = await _cloud_principal(user)
        if isinstance(principal, JSONResponse):
            return principal

        try:
            parsed = _uuid.UUID(session_id)
        except (ValueError, AttributeError):
            return JSONResponse(status_code=404,
                                content={"detail": "No such session."})

        try:
            ended = await revoke_session(principal, parsed, reason="ended by the user")
        except Exception:  # noqa: BLE001
            logger.exception("could not revoke a session")
            return JSONResponse(
                status_code=503,
                content={"detail": "Sessions are temporarily unavailable."})

        if not ended:
            return JSONResponse(status_code=404,
                                content={"detail": "No such session."})
        return {"detail": "That session has ended."}

    @app.get("/auth/cloud/devices", tags=["auth"],
             summary="Devices this account signs in from")
    @limiter.limit("30/minute")
    async def list_own_devices(
        request: Request,
        user: str = Depends(require_auth),
    ):
        from core.identity.sessions import list_devices

        principal = await _cloud_principal(user)
        if isinstance(principal, JSONResponse):
            return principal

        try:
            devices = await list_devices(principal)
        except Exception:  # noqa: BLE001
            logger.exception("could not list devices")
            return JSONResponse(
                status_code=503,
                content={"detail": "Devices are temporarily unavailable."})

        return {"devices": [
            {
                "id": str(d.device_id),
                "label": d.label,
                "platform": d.platform,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
            }
            for d in devices
        ]}

    @app.post("/auth/cloud/devices/{device_id}/revoke", tags=["auth"],
              summary="Revoke a device and end its sessions")
    @limiter.limit("30/minute")
    async def revoke_own_device(
        request: Request,
        device_id: str,
        user: str = Depends(require_auth),
    ):
        """Revoke a device and end every live session on it.

        The sessions are the point. Marking a lost phone revoked while its
        sessions keep refreshing is a checkbox, not a revocation.
        """
        import uuid as _uuid

        from core.identity.sessions import revoke_device

        principal = await _cloud_principal(user)
        if isinstance(principal, JSONResponse):
            return principal

        try:
            parsed = _uuid.UUID(device_id)
        except (ValueError, AttributeError):
            return JSONResponse(status_code=404,
                                content={"detail": "No such device."})

        try:
            ended = await revoke_device(principal, parsed,
                                        reason="device revoked by the user")
        except Exception:  # noqa: BLE001
            logger.exception("could not revoke a device")
            return JSONResponse(
                status_code=503,
                content={"detail": "Devices are temporarily unavailable."})

        return {"detail": "That device has been revoked.", "sessions_ended": ended}

    @app.get("/auth/cloud/security-events", tags=["auth"],
             summary="This account's own authentication history")
    @limiter.limit("30/minute")
    async def list_own_auth_events(
        request: Request,
        user: str = Depends(require_auth),
    ):
        """When this account signed in, and what happened to it.

        The caller's OWN history and nobody else's — not the tenant's. "When did
        my colleague last sign in" is the movement log `057` refused to hand out
        through `/auth/cloud/sessions`, and it is no more a tenant
        administrator's to read because it lives in an audit table. `061`'s
        SELECT policy is what enforces that; this endpoint passes no predicate
        of its own, so the boundary has exactly one definition.

        ── What is NOT returned ────────────────────────────────────────────

        `subject_hash` and `ip_hash`. They are HMACs under the application
        secret and so are not an address or an IP — but they are STABLE
        identifiers, and a list of them is a list of "the same place, again",
        which is not something an API needs to hand out. `auth_audit.history`
        drops them at the source rather than here, so a second caller cannot
        forget to.
        """
        from core.identity import auth_audit

        principal = await _cloud_principal(user)
        if isinstance(principal, JSONResponse):
            return principal

        try:
            events = await auth_audit.history(principal)
        except Exception:  # noqa: BLE001
            logger.exception("could not read the authentication history")
            return JSONResponse(
                status_code=503,
                content={"detail": "Security events are temporarily unavailable."})

        return {"events": events}

    @app.post("/auth/verify-email", tags=["auth"], summary="Verify an email address")
    @limiter.limit("10/minute")
    async def verify_email_endpoint(
        request: Request,
        token: str = Form(...),
    ):
        from core.identity.registration import VerificationError, verify_email

        try:
            await verify_email(token)
        except VerificationError:
            # One answer for unknown, expired, consumed, deleted and
            # address-changed. Distinguishing them tells the holder of a stolen
            # link whether the address is real.
            return JSONResponse(
                status_code=400,
                content={"detail": "This verification link is not valid."},
            )
        except Exception:  # noqa: BLE001
            logger.exception("email verification failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Verification is temporarily unavailable."},
            )

        return {"detail": "Email address verified."}

    # ── Password reset and verification resend (Phase 2 batch 9) ────────────
    #
    # Both are unauthenticated, both take an email address, and both answer
    # IDENTICALLY whatever that address turns out to be. That is the whole
    # design: these are the endpoints an attacker uses to build a user list
    # before attacking anything, and any difference in status, body or timing
    # is the oracle they are looking for.
    #
    # "Identically" includes the mail. Whether delivery succeeded is invisible
    # to the caller, because a "we could not send that" for real addresses only
    # is the same oracle wearing a different hat.

    @app.post("/auth/password-reset/request", tags=["auth"],
              summary="Request a password reset link")
    @limiter.limit("5/minute")
    async def request_password_reset_endpoint(
        request: Request,
        email: str = Form(...),
    ):
        """Always 202. The response says nothing about the address."""
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.password_reset import (
            ResetUnavailable, request_password_reset,
        )

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        accepted = JSONResponse(
            status_code=202,
            content={"detail": "If that address has a verified account, "
                               "a reset link has been sent."},
        )

        try:
            outcome = await request_password_reset(email)
        except ResetUnavailable:
            # A store outage is the ONE case that answers differently, and it
            # discloses nothing: reaching it needs no valid address, so it says
            # "the service is unwell", not "that address exists".
            return JSONResponse(
                status_code=503,
                content={"detail": "Password reset is temporarily unavailable."},
            )
        except Exception:  # noqa: BLE001 — never carries the token
            logger.exception("password reset request failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Password reset is temporarily unavailable."},
            )

        if outcome.issued and outcome.token and outcome.email:
            await _send_reset_link(outcome.email, outcome.token)

        return accepted

    @app.post("/auth/password-reset/complete", tags=["auth"],
              summary="Set a new password using a reset link")
    @limiter.limit("5/minute")
    async def complete_password_reset_endpoint(
        request: Request,
        token: str = Form(...),
        password: str = Form(...),
    ):
        """One answer for unknown, expired and already-used.

        Distinguishing them tells the holder of a stolen link whether it was
        ever real, and when it stopped being real — which is exactly what
        somebody sweeping a mailbox archive wants to know.
        """
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.password_reset import (
            ResetError, ResetUnavailable, complete_password_reset,
        )

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        try:
            await complete_password_reset(token, password)
        except ResetUnavailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Password reset is temporarily unavailable."},
            )
        except ResetError as exc:
            # `str(exc)` is safe: every message this raises is written in
            # `password_reset.py` and none of them names the account, the
            # address or the token. Password-complexity complaints reach here
            # too, and they depend on the submitted password rather than on
            # anything about the account.
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("password reset completion failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Password reset is temporarily unavailable."},
            )

        return {"detail": "Your password has been changed and every session "
                          "has been signed out."}

    @app.post("/auth/verify-email/resend", tags=["auth"],
              summary="Send the verification link again")
    @limiter.limit("3/minute")
    async def resend_verification_endpoint(
        request: Request,
        email: str = Form(...),
    ):
        """Always 202, for the same reason as the reset request.

        Rate limited harder than the reset, because this one is a mail
        amplifier: an attacker who can call it repeatedly turns this service
        into the thing that floods somebody else's inbox.
        """
        from core.editions import EDITION_CLOUD, current_edition
        from core.identity.registration import (
            RegistrationDisabled, resend_verification,
        )

        if current_edition() != EDITION_CLOUD:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        accepted = JSONResponse(
            status_code=202,
            content={"detail": "If that address is awaiting verification, "
                               "a new link has been sent."},
        )

        try:
            outcome = await resend_verification(email)
        except RegistrationDisabled:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        except Exception:  # noqa: BLE001
            logger.exception("verification resend failed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Verification is temporarily unavailable."},
            )

        if outcome.token and outcome.email:
            await _send_verification_link(outcome.email, outcome.token)

        return accepted
