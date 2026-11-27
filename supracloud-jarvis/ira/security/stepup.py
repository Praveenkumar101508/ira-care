"""security/stepup.py — refuse an action unless the caller proved who they are recently (D-095).

A bearer token proves that somebody signed in once and that nobody has signed the session out
since. It does not prove that the person is still at the keyboard: an access token copied out
of a browser works for whoever copied it, and a refresh token keeps a session alive for a week
without anybody typing a password. For most of the API that is the accepted trade. For a
deployment administrator about to open the operating system of a watch on somebody's wrist, or
wipe it (care/deviceadmin.py), it is not.

So the session token now records when its holder last proved who they are (`auth_time`, Unix
seconds) and how (`amr`: "pwd", "otp", "recovery"), set at sign-in and at `POST /auth/step-up`
and COPIED unchanged by `/auth/refresh` (api/middleware/auth.py). `principal_from_username`
carries both onto the Principal. This module decides, from those two facts and the account's
own second-factor enrolment, whether an action may go ahead:

  * no `auth_time` (a token issued before the claim existed, or a session that never passed
    through `require_auth`)                                    → refused: never recently authenticated;
  * `auth_time` older than the window (`IRA_STEP_UP_MAX_AGE_SECONDS`, 60 to 3600, default 600)
                                                                → refused: too old;
  * the account has an ENABLED second factor and neither "otp" nor "recovery" is in `amr`
                                                                → refused: the factor was not shown;
  * the enrolment state cannot be read                          → 503, never "no factor, carry on".

The refusal is 403 in the API's error envelope with `error_code: step_up_required`
(core/runtime/errors.STEP_UP_REQUIRED, "This action requires re-authentication.") and
`safe_details` carrying `reason`, `max_age_seconds`, `auth_age_seconds` when it is known, and
`how: POST /auth/step-up`, so a client can send the person to step up and retry. It is a fact about the session, not about the account's rights: a
caller who is not allowed the action at all must be refused BEFORE this check, so that a
non-administrator learns nothing about step-up (care/routes.py does the grant check first).

What this does not do: it cannot tell a password typed by the account holder from one typed by
somebody who phished it, and a step-up token stolen inside the window works as the holder's
would. It narrows the window from "a week of refreshes" to `max_age_seconds`, and with a second
factor enrolled it adds the factor; D-089's two-person rule for a factory reset stays on top.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException

from core.identity.principal import Principal
from core.runtime.errors import STEP_UP_REQUIRED, IRAError

logger = logging.getLogger("ira.security.stepup")

#: The `amr` values that count as a second factor having been shown.
SECOND_FACTOR_METHODS = frozenset({"otp", "recovery"})

#: Where a refused caller goes next (`safe_details.how`); the message itself is STEP_UP_REQUIRED's.
HOW = "POST /auth/step-up"

#: Why a check refused. Public (`detail.reason`), so each names only a fact about the SESSION.
NO_AUTH_TIME = "no_authentication_time"
TOO_OLD = "authentication_too_old"
SECOND_FACTOR_MISSING = "second_factor_not_shown"


def max_age_seconds() -> int:
    """The configured window (config.py bounds it to 60..3600 at start)."""
    from config import get_settings
    return int(get_settings().ira_step_up_max_age_seconds)


def auth_age_seconds(principal: Principal, *, now: Optional[float] = None) -> Optional[int]:
    """Seconds since the session's holder last proved who they are; None when unknown.
    A clock in the future reads as age 0, not as a negative age that would pass any window."""
    if principal.auth_time is None:
        return None
    return max(0, int((now if now is not None else time.time()) - int(principal.auth_time)))


@dataclass(frozen=True)
class Verdict:
    """What the check decided, for the `/me` route and the tests as much as for the refusal."""
    auth_age_seconds: Optional[int]
    max_age_seconds: int
    mfa_enrolled: bool
    reason: Optional[str]   # None when the session is recent enough

    @property
    def ok(self) -> bool:
        return self.reason is None


def assess(principal: Principal, *, max_age: int, mfa_enrolled: bool,
           require_mfa_if_enrolled: bool = True, now: Optional[float] = None) -> Verdict:
    """The pure decision: no database, no clock but `now`."""
    age = auth_age_seconds(principal, now=now)
    if age is None:
        reason = NO_AUTH_TIME
    elif age > max_age:
        reason = TOO_OLD
    elif require_mfa_if_enrolled and mfa_enrolled and not (SECOND_FACTOR_METHODS & set(principal.amr)):
        reason = SECOND_FACTOR_MISSING
    else:
        reason = None
    return Verdict(auth_age_seconds=age, max_age_seconds=max_age, mfa_enrolled=mfa_enrolled, reason=reason)


class EnrolmentUnknown(RuntimeError):
    """The account's second-factor enrolment could not be read. Callers answer 503."""


async def mfa_enrolled(principal: Principal) -> bool:
    """Whether the account has an ENABLED, unrevoked second factor, read on the caller's own
    bound connection (`totp_secrets` has forced row-level security on the user and tenant).
    A failure to read is raised, never read as "not enrolled"."""
    from core.identity.db_context import tenant_connection
    try:
        async with tenant_connection(principal) as conn:
            return bool(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM totp_secrets "
                "WHERE user_id = $1 AND enabled = TRUE AND revoked_at IS NULL)", principal.user_id))
    except Exception as exc:  # noqa: BLE001 — the refusal below says why; the seed is never involved
        logger.warning("second-factor enrolment unknown for user=%s: %s", principal.user_id, type(exc).__name__)
        raise EnrolmentUnknown("the second-factor enrolment could not be read") from exc


def refusal(verdict: Verdict) -> IRAError:
    """403 `step_up_required` in the API's error envelope. `safe_details` names only facts about the
    session (its age, the window, which part is missing) and where to go next."""
    details = {"reason": verdict.reason, "max_age_seconds": verdict.max_age_seconds, "how": HOW}
    if verdict.auth_age_seconds is not None:
        details["auth_age_seconds"] = verdict.auth_age_seconds
    return IRAError(STEP_UP_REQUIRED, safe_details=details,
                    log_message=f"step-up required: {verdict.reason}")


async def check(principal: Principal, *, max_age_seconds_: Optional[int] = None,
                require_mfa_if_enrolled: bool = True) -> Verdict:
    """The verdict for this principal, with the enrolment read. Raises 503 when it cannot be."""
    max_age = int(max_age_seconds_) if max_age_seconds_ is not None else max_age_seconds()
    try:
        enrolled = await mfa_enrolled(principal) if require_mfa_if_enrolled else False
    except EnrolmentUnknown:
        raise HTTPException(status_code=503, detail="Your second factor could not be checked right now.")
    return assess(principal, max_age=max_age, mfa_enrolled=enrolled, require_mfa_if_enrolled=require_mfa_if_enrolled)


async def assert_recent_authentication(principal: Principal, *, max_age_seconds_: Optional[int] = None,
                                       require_mfa_if_enrolled: bool = True) -> Verdict:
    """Refuse (403 `step_up_required`) unless the session is recent enough. For a route that has
    its own authorisation to do FIRST (care/routes.py: the deployment-administrator grant)."""
    verdict = await check(principal, max_age_seconds_=max_age_seconds_,
                          require_mfa_if_enrolled=require_mfa_if_enrolled)
    if not verdict.ok:
        logger.warning("step-up required user=%s reason=%s age=%s max=%s", principal.user_id,
                       verdict.reason, verdict.auth_age_seconds, verdict.max_age_seconds)
        raise refusal(verdict)
    return verdict


def require_recent_authentication(max_age_seconds: Optional[int] = None, require_mfa_if_enrolled: bool = True,
                                  *, principal: Callable = None) -> Callable:
    """A FastAPI dependency: the Principal, refused with 403 `step_up_required` unless its holder
    proved who they are within `max_age_seconds` (the setting when None) and, when the account
    has a second factor, showed it. `principal` is the dependency that resolves the caller
    (`require_tenant_principal` by default)."""
    if principal is None:
        from api.middleware.workspace import require_tenant_principal
        principal = require_tenant_principal

    async def dependency(p: Principal = Depends(principal)) -> Principal:
        await assert_recent_authentication(p, max_age_seconds_=max_age_seconds,
                                           require_mfa_if_enrolled=require_mfa_if_enrolled)
        return p

    dependency.__name__ = f"require_recent_authentication_{max_age_seconds or 'default'}"
    return dependency


__all__ = [
    "HOW", "NO_AUTH_TIME", "SECOND_FACTOR_METHODS", "SECOND_FACTOR_MISSING", "TOO_OLD",
    "EnrolmentUnknown", "Verdict", "assess", "assert_recent_authentication", "auth_age_seconds",
    "check", "max_age_seconds", "mfa_enrolled", "refusal", "require_recent_authentication",
]
