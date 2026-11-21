"""Cloud login — email + password to an authoritative Principal (Phase 2 batch 3).

Batch 1 made a cloud principal resolvable. Batch 2 created the account and
proved the address, and its own inventory recorded the gap this module closes:

    a credential store exists and **nothing reads it**

`user_credentials` had exactly one writer (`registration.register_account`) and
no reader, so a registered cloud user held a password hash that no code path
could ever check. This is the reader.

── The chain, and who decides each link ────────────────────────────────────

    email + password
      → locate the account            (this module — a lookup, not a decision)
      → verify the password           (api.middleware.auth.verify_password)
      → resolve the Principal         (resolver._resolve_cloud_principal)
      → issue access + refresh        (api.middleware.auth.create_login_tokens)

Only the second step is an authorisation decision this module makes. Everything
about *who the caller is* — tenant, role, scopes, owner status, whether the
address is verified, whether the account is active, whether a membership grants
access — is decided by `resolve_principal`, which Batch 1 built to be the single
authority. Re-deriving any of it here would create a second answer to the same
question, and two authorities disagree eventually.

That is why the user lookup below selects only `id` and `normalised_email`. It
is deliberately not a place where eligibility is judged; it exists to find the
credential row and nothing else.

── The token carries a subject and nothing else ────────────────────────────

No tenant claim, no role claim, no scope claim. `create_login_tokens` stamps
`sub`, `jti`, `tok` and `ver`, and every subsequent request re-runs
`resolve_principal(sub)` against the database. So a membership revoked one
second after login stops granting access on the next request rather than in
thirty minutes, and a stolen token cannot be edited into a different tenant
because there is nothing tenant-shaped in it to edit.

This is the Batch 1 decision held rather than relaxed. Putting the tenant in the
token would make login simpler and would make the token a client-held authority.

── There is no tenant parameter, and that is the honest shape ──────────────

A user holding more than one access-granting membership is refused. It would be
easy to accept a tenant at login instead — but the token cannot carry the
answer, so every later request would re-resolve without one and fail. A login
that succeeds and then cannot make a single authenticated request is worse than
a refusal.

Multi-tenant login therefore waits for tenant selection, which is a batch of its
own. `test_a_user_with_two_memberships_is_refused` pins the current behaviour so
the gap is a recorded state rather than a surprise.

── Uniform refusal ─────────────────────────────────────────────────────────

`CloudLoginRefused` is raised for every one of: no such address, an address that
was never verified, a wrong password, no credential row, a suspended account, a
deleted account, no access-granting membership, and more than one. The caller
gets one answer, and `test_every_refusal_is_externally_identical` enumerates
them and asserts the responses are byte-identical.

The one branch that is *not* uniform is the second factor, and it cannot be
reached without a correct password — see `authenticate_cloud_user`.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from core.identity import auth_audit
from core.identity.principal import Principal

logger = logging.getLogger("ira.identity.cloud_login")


class CloudLoginRefused(RuntimeError):
    """These credentials do not sign in. One type for every reason.

    Deliberately carries no detail the caller can act on. The message is for
    the log; the route turns every instance of this into the same response.
    """


class CloudLoginUnavailable(RuntimeError):
    """Authentication state could not be established. Fail closed.

    Distinct from `CloudLoginRefused` because the honest answer is different —
    "no" versus "I cannot tell" — and because collapsing the two in the *other*
    direction is the P2-05 defect: an undeterminable second-factor enrolment
    read as "no second factor configured".

    Both deny. The separation exists so a caller can answer 503 rather than 401
    and a monitoring system can tell an outage from a brute-force attempt. It
    must never become a third outcome that signs anybody in.
    """


class CloudMfaRequired(RuntimeError):
    """The password was right and the second factor was missing or wrong.

    Reachable only after `verify_password` has returned True, which is what
    makes it safe to distinguish: it tells the caller nothing they could not
    already infer from holding a working password.
    """


@dataclass(frozen=True, slots=True)
class TenantChoice:
    """One organisation the authenticated user may act in."""

    tenant_id: uuid.UUID
    name: str
    role: str


class TenantSelectionRequired(RuntimeError):
    """Credentials are correct; the user belongs to several organisations.

    NOT a login failure. Batch 3 folded `AmbiguousTenant` into the uniform
    refusal because a cloud token could not carry a selection, so a user with
    two memberships genuinely could not sign in — and batch 4 refused to
    provision a second tenant precisely so that state could not be reached.
    Invitations make it reachable, so this is the answer instead of a refusal.

    ── Why listing the tenants here is not a disclosure ────────────────────

    `choices` is only ever built AFTER the password has been verified, so the
    caller has already proved they are this account. It lists the organisations
    that account is a member of and nothing else — it is not a directory, and
    it cannot be reached by anyone who does not already hold the credential.
    """

    def __init__(self, subject: str, choices: list["TenantChoice"]):
        super().__init__("select an organisation to continue")
        self.subject = subject
        self.choices = choices


@dataclass(frozen=True, slots=True)
class CloudLoginOutcome:
    """A completed cloud authentication.

    `principal` is the authoritative one — read from the database after the
    password was verified, never assembled here.
    """

    principal: Principal
    #: The value that goes in the token's `sub`, and therefore the value every
    #: later request re-resolves. The database's stored normalised form, never
    #: the string the caller typed.
    subject: str


async def _locate_credential(email: str) -> tuple[Optional[uuid.UUID], Optional[str], Optional[str]]:
    """Find (user_id, normalised_email, password_hash) for a verified account.

    Returns `(None, None, None)` when there is nothing to authenticate — no
    such address, never verified, deleted, or verified with no credential row.
    The caller must still do the same amount of work in that case; see
    `authenticate_cloud_user`.

    ── The predicate is the resolver's, on purpose ─────────────────────────

    `email_verified_at IS NOT NULL AND deleted_at IS NULL` is not an eligibility
    check bolted on here — it is what makes the lookup *deterministic*.
    `users_verified_email_key` is UNIQUE on `normalised_email` only among rows
    that satisfy exactly this predicate, and 034 records why: *"A VERIFIED email
    is globally unique. An unverified one is not constrained, so claiming
    someone else's address cannot block or seize their account."*

    Drop either clause and several rows can share an address, so `fetchrow`
    returns an arbitrary one — which would mean an attacker who registers
    (without verifying) an address someone else already owns could be handed
    that account's credential row to authenticate against.

    `normalise_email()` is the DATABASE's function, matching the resolver and
    the generated column. It lowercases only the domain, because the local-part
    is case-sensitive; a Python `.lower()` here would refuse `Solo@x.com`.
    """
    from core.identity.db_context import identity_bootstrap_connection
    from utils.db import acquire

    async with acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, normalised_email
              FROM users
             WHERE normalised_email = normalise_email($1)
               AND email_verified_at IS NOT NULL
               AND deleted_at IS NULL
            """,
            email,
        )

    if user is None:
        return None, None, None

    # `user_credentials` is FORCE RLS keyed on `app.user_id`, so this cannot be
    # read on an ordinary connection — an unbound one matches zero rows and
    # would look exactly like "this user has no credential". Binding the id we
    # just resolved means the connection can reach exactly one row: this user's.
    #
    # Third call site of the bootstrap connection, on the architecture guard's
    # allow-list deliberately. It binds no tenant, so it cannot read tenant data
    # at all — which is the correct blast radius for a pre-authentication read.
    async with identity_bootstrap_connection(user["id"]) as conn:
        cred = await conn.fetchrow(
            "SELECT password_hash FROM user_credentials WHERE user_id = $1",
            user["id"],
        )

    if cred is None:
        # A user row with no credential: registration's second write did not
        # complete, or the account authenticates some other way. Either way
        # there is no password to check.
        return None, None, None

    return user["id"], user["normalised_email"], cred["password_hash"]


async def _selectable_tenants(user_id) -> list[TenantChoice]:
    """The organisations this user may act in, read from the database.

    On `identity_bootstrap_connection`, which binds the user and NO tenant — so
    `tenant_memberships`' SELECT policy (`tenant_id = app.tenant_id OR
    user_id = app.user_id`) matches exactly this user's own rows and no other
    member of any organisation is visible. The list cannot be widened by
    changing the query.

    The name comes from `tenants.display_name`, which 055 adds precisely
    because this read happens before any tenant is bound: `organisations.name`
    is FORCE RLS tenant-scoped, so an unbound join returns NULL for every row
    and the user would be asked to choose between UUIDs.
    """
    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT m.tenant_id, m.role,
                   COALESCE(t.display_name, t.name) AS name
              FROM tenant_memberships m
              JOIN tenants t ON t.id = m.tenant_id
             WHERE m.user_id = $1
               AND m.membership_grants_access
             ORDER BY name
            """,
            user_id,
        )
    return [
        TenantChoice(tenant_id=r["tenant_id"], name=r["name"], role=r["role"])
        for r in rows
    ]


async def _audit(event: str, principal, subject: str, *, detail: str = "") -> None:
    """Record an authentication event against a resolved principal.

    A thin wrapper so the call sites stay one line each. `auth_audit.record`
    never raises, so this cannot change a login outcome — which is the whole
    contract of the audit trail: it is evidence, not a control.
    """
    await auth_audit.record(
        event,
        user_id=getattr(principal, "user_id", None),
        tenant_id=getattr(principal, "tenant_id", None),
        subject=subject,
        detail=detail,
    )


async def authenticate_cloud_user(
    email: str,
    password: str,
    *,
    totp_code: str | None = None,
    recovery_code: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> CloudLoginOutcome:
    """Authenticate a cloud account and resolve its authoritative Principal.

    Raises `CloudLoginRefused` for every credential failure, `CloudMfaRequired`
    when a correct password is missing its second factor, and
    `CloudLoginUnavailable` when the answer cannot be determined.

    ── Constant work ──────────────────────────────────────────────────────

    When there is no account, or no credential, the password is still hashed —
    against a stable dummy — before refusing. Without it the response time says
    whether the address is registered, and no amount of uniform response body
    hides a bcrypt round. This is the same mechanism `authenticate_user` uses
    for a wrong username (P2-01), reusing the same dummy rather than minting a
    second one, so both paths cost the same and cannot drift apart.

    The refusal is raised *after* the verification either way, not short-
    circuited before it. An early `return` in the missing-account branch is
    precisely the defect the constant-work design exists to prevent, and
    `test_a_missing_account_costs_the_same_as_a_wrong_password` is what notices.
    """
    from api.middleware.auth import _dummy_hash, verify_password

    if not email or not email.strip() or not password:
        raise CloudLoginRefused("no credentials presented")

    try:
        user_id, subject, password_hash = await _locate_credential(email.strip())
    except Exception as exc:  # noqa: BLE001 — an unreadable register is a refusal
        # Fail CLOSED and in a type the caller handles. A raw asyncpg error
        # escaping here would not be caught by `except CloudLoginRefused`, so a
        # route written to refuse would instead return a 500 with a traceback —
        # and a connection error's body can carry the DSN. The type only.
        logger.warning("cloud login credential lookup failed: %s", type(exc).__name__)
        raise CloudLoginUnavailable(
            "the credential store could not be reached"
        ) from exc

    # Run bcrypt in both branches. `verify_password` against the dummy cannot
    # succeed — nothing knows its plaintext — so this is cost, not a decision.
    hash_to_check = password_hash if password_hash is not None else _dummy_hash()
    try:
        password_ok = verify_password(password, hash_to_check)
    except Exception:  # noqa: BLE001 — a malformed stored hash is a refusal
        # passlib raises on a hash it cannot parse. That is a broken credential
        # row, not a valid login, and it must not propagate as a 500.
        logger.warning("cloud login could not verify a stored hash")
        password_ok = False

    if password_hash is None or not password_ok:
        # The attempted address is HMAC'd before it is stored, so this row
        # records "somebody tried this mailbox" without becoming a list of
        # addresses somebody probed. `user_id` is deliberately absent even when
        # the account exists: attributing a failed attempt to an account would
        # make the log say which addresses are registered.
        await auth_audit.record(auth_audit.SIGN_IN_REFUSED, subject=email)
        raise CloudLoginRefused("no account matches these credentials")

    assert subject is not None and user_id is not None

    # ── The authoritative decision ──────────────────────────────────────────
    #
    # Everything that follows is read, not asserted. `resolve_principal` decides
    # the tenant, the role, the scopes and `is_owner`; this module supplies only
    # the subject it has just proven possession of. A verified password with no
    # access-granting membership resolves to nothing and is refused here.
    from core.identity.resolver import (
        CloudIdentityNotAvailable, resolve_principal,
    )
    from core.editions import EDITION_CLOUD

    # `tenant_id` is a SELECTION, not an assertion. `resolve_principal` verifies
    # it against `tenant_memberships` before honouring it, so a value the caller
    # invented resolves to nothing — the refusal below is identical to the one
    # for a tenant that does not exist.
    from core.identity.resolver import AmbiguousTenant

    try:
        principal = await resolve_principal(
            subject, edition=EDITION_CLOUD, tenant_id=tenant_id,
        )
    except AmbiguousTenant as exc:
        # Several memberships and no selection. The credentials were correct,
        # so this is a question rather than a refusal — and it is asked only
        # after the password has been verified, which is what makes listing the
        # user's own organisations safe.
        logger.info("cloud login requires a tenant selection")
        raise TenantSelectionRequired(
            subject, await _selectable_tenants(user_id),
        ) from exc
    except CloudIdentityNotAvailable as exc:
        # Suspended, deleted, no membership, or a selected tenant this user is
        # not a member of. One answer, deliberately: distinguishing "you are
        # not in that organisation" from "no such organisation" would let a
        # caller with one stolen password map the tenant space.
        logger.info("cloud login resolved no principal: %s", type(exc).__name__)
        # A CORRECT password that resolves to nothing — suspended, deleted, no
        # membership. Worth distinguishing in the log from a wrong password even
        # though the CALLER is told the same thing: "somebody has this account's
        # password and it is suspended" is a different incident from "somebody
        # is guessing".
        await auth_audit.record(
            auth_audit.SIGN_IN_REFUSED, user_id=user_id, subject=subject,
            detail="password correct, no resolvable identity")
        raise CloudLoginRefused("no resolvable identity for these credentials") from exc

    # ── Second factor (P2-05) ───────────────────────────────────────────────
    #
    # Reuses the shared checker rather than reading the secret store here. That
    # store is FORCE RLS on `app.user_id` and `app.tenant_id`, and the P2-05
    # defect was a login path reading it on an unbound connection: the query
    # matched zero rows for everybody, so the challenge never ran and anyone
    # enrolled was signing in on a password alone.
    #
    # The principal is passed in, so the factor is decided for the identity that
    # was just resolved rather than for one this path resolves a second time.
    # Two resolutions is two chances to disagree about who is logging in.
    from security.totp import (
        FACTOR_BAD, NOT_ENROLLED, TotpUnavailable, check_login_second_factor,
    )

    try:
        factor = await check_login_second_factor(
            subject, totp_code, principal=principal,
        )
    except TotpUnavailable as exc:
        # "I cannot tell whether this account has a second factor" must never
        # be read as "it has none". Refusing costs a login; the other direction
        # costs the factor entirely.
        logger.warning("refusing cloud login: second-factor state is unknown")
        raise CloudLoginUnavailable(
            "the second-factor enrolment state could not be determined"
        ) from exc

    if factor == FACTOR_BAD and recovery_code:
        # A recovery code substitutes for the FACTOR and nothing else. It is
        # tried only after the enrolment check has said a factor is required and
        # the presented TOTP did not satisfy it, so it cannot become a way to
        # skip a challenge that was never issued.
        #
        # Everything the resolution already decided still stands: the principal
        # above came from `resolve_principal`, so a suspended member or one
        # whose membership was revoked is already refused and no recovery code
        # brings them back. Recovery restores a lost authenticator, not lost
        # authority.
        from security.mfa_recovery import (
            RecoveryUnavailable, consume_recovery_code,
        )

        try:
            if await consume_recovery_code(principal, recovery_code):
                logger.info("cloud login used a recovery code")
                await _audit(auth_audit.RECOVERY_CODE_USED, principal, subject)
                await _audit(auth_audit.SIGN_IN_SUCCEEDED, principal, subject,
                             detail="second factor satisfied by a recovery code")
                return CloudLoginOutcome(principal=principal, subject=subject)
        except RecoveryUnavailable as exc:
            # Fail CLOSED, and as unavailable rather than as a bad code: "I
            # could not check" must not read as "that code was wrong", which
            # would send a user to regenerate codes that are perfectly good.
            raise CloudLoginUnavailable(
                "the recovery store could not be reached"
            ) from exc

    if factor == FACTOR_BAD:
        # Recorded as MFA_FAILED rather than MFA_REQUIRED, and the distinction
        # is the point of having both. Reaching here needs a CORRECT password,
        # so this is somebody who has the password and not the factor — which
        # is either a user with a flat phone or an attacker holding stolen
        # credentials, and those look identical until you count them.
        await _audit(auth_audit.MFA_FAILED, principal, subject)
        raise CloudMfaRequired("a valid second-factor code is required")

    assert factor in (NOT_ENROLLED, "ok")

    if factor == "ok":
        await _audit(auth_audit.MFA_SUCCEEDED, principal, subject)

    logger.info("cloud login succeeded for a resolved principal")
    await _audit(auth_audit.SIGN_IN_SUCCEEDED, principal, subject)
    return CloudLoginOutcome(principal=principal, subject=subject)


async def verify_cloud_password(subject: str, password: str, *, user_id) -> bool:
    """Re-check the password of an account that is ALREADY signed in (P0-01).

    For requests that change how the account is protected, where a bearer token
    alone is not enough (`security.reauth`). The same lookup and the same bcrypt
    verification as `authenticate_cloud_user`, and nothing after them: the
    caller is already authenticated, so there is no principal to resolve and no
    second factor to ask for here.

    `user_id` is the signed-in principal's. The located credential must belong
    to it, so the check can only ever be about the caller's own account. The
    bcrypt round runs whether or not an account is found, as at sign-in.

    Raises `CloudLoginUnavailable` when the credential store cannot be read.
    """
    from api.middleware.auth import _dummy_hash, verify_password

    if not subject or not subject.strip() or not password:
        return False
    try:
        found_id, _subject, password_hash = await _locate_credential(subject.strip())
    except Exception as exc:  # noqa: BLE001 — an unreadable register is not a "no"
        logger.warning("re-authentication credential lookup failed: %s",
                       type(exc).__name__)
        raise CloudLoginUnavailable("the credential store could not be reached") from exc

    hash_to_check = password_hash if password_hash is not None else _dummy_hash()
    try:
        password_ok = verify_password(password, hash_to_check)
    except Exception:  # noqa: BLE001 — a malformed stored hash is a refusal
        logger.warning("re-authentication could not verify a stored hash")
        password_ok = False
    if password_hash is None or found_id is None:
        return False
    return bool(password_ok) and str(found_id) == str(user_id)


__all__ = [
    "CloudLoginOutcome",
    "TenantChoice",
    "TenantSelectionRequired",
    "CloudLoginRefused",
    "CloudLoginUnavailable",
    "CloudMfaRequired",
    "authenticate_cloud_user",
    "verify_cloud_password",
]
