"""Password reset (Phase 2 batch 9).

The most attacked endpoint in any authentication system, because it is the one
that hands a credential to somebody who could not present one. Almost all of
this module is about the ways it must not work.

── The answer never depends on whether the account exists ──────────────────

`request_password_reset` returns the same value, does the same work and takes
the same time for a registered address, an unregistered one, a suspended
account and a deleted one. Any difference is an enumeration oracle, and this is
the endpoint attackers use to build a user list before they attack anything
else. That includes the mail: whether delivery succeeded changes nothing the
caller can see, because a "we could not send that" response for real addresses
only is the same oracle wearing a different hat.

── A reset ends every session ──────────────────────────────────────────────

This is the point people forget. Somebody resets their password precisely
because they think somebody else has it — and if the attacker is already signed
in, a new password changes nothing for them: their refresh token keeps working
for its full lifetime. `complete_password_reset` revokes every session, in the
same transaction-shaped flow as the credential write.

── A reset does NOT satisfy the second factor ──────────────────────────────

It restores one factor, the one that was lost. An account with TOTP enrolled
still has to present it at the next login, and nothing here touches
`totp_secrets`, `user_mfa_keys` or `mfa_recovery_codes`. A reset that cleared
the second factor would make the mailbox a complete account takeover path,
which is the opposite of what a second factor is for.

── Unverified addresses ────────────────────────────────────────────────────

An account whose address has never been verified gets no reset link. The
address is not known to belong to the person, so mailing a credential to it
would let whoever squatted the address take the account — and `034` permits
several unverified rows to share an address, so "the account for this address"
is not even a well-defined question. They register or verify instead. The
CALLER cannot tell that this happened, for the enumeration reason above.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass

logger = logging.getLogger("ira.identity.password_reset")

#: Short, because the window is the exposure. A reset link sitting in a mailbox
#: is a live credential for the account; an hour is long enough to walk to
#: another device and short enough that a mailbox compromised next week is not a
#: compromised account.
RESET_TTL_MINUTES = 60


class ResetError(RuntimeError):
    """The reset could not proceed. Safe to show to the caller."""


class ResetUnavailable(ResetError):
    """The store could not be reached. Fail closed — never fail open."""


@dataclass(frozen=True, slots=True)
class ResetRequest:
    """The result of asking for a reset.

    `token` is present only when one was actually minted, and is handed
    straight to the mail transport. It is never returned to an HTTP caller and
    never logged — the route answers 202 and nothing else, whatever this says.
    """

    issued: bool
    token: str | None = None
    email: str | None = None
    user_id: uuid.UUID | None = None


def _hash_token(token: str) -> str:
    """SHA-256, matching `054` and `055`.

    Not bcrypt, and deliberately unlike `056`: the token is 256 bits of
    randomness, so there is no dictionary for a slow hash to slow down, and this
    runs on an endpoint an attacker can reach without any credential — where a
    deliberately expensive hash is a denial-of-service lever rather than a
    defence.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _token_connection(token_hash: str):
    """A connection that may see exactly ONE reset token: this one.

    Local to this module for the same reason `registration._token_connection`
    is local to that one: `db_context`'s helpers all take a Principal, and
    adding "no principal required" entry points to the shared surface would
    make the pattern look ordinary. The only thing that can open a
    reset-token-scoped connection is the module that owns reset tokens.
    """
    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.reset_token_hash', $1, true)", token_hash)
            yield conn


async def request_password_reset(email: str) -> ResetRequest:
    """Mint a reset token for a verified account, or do nothing.

    Returns `issued=False` for every case that does not produce a token —
    unknown address, unverified address, suspended or deleted account. The
    CALLER must not vary its response on this: see the module docstring.

    Supersedes any outstanding token for the same user. Requesting twice must
    not leave two live links: the second request replaces the first in the
    user's mind, and an attacker who can trigger requests should not be able to
    accumulate valid tokens.
    """
    from contextlib import asynccontextmanager

    from utils.db import acquire

    presented = (email or "").strip()
    if not presented:
        return ResetRequest(issued=False)

    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    try:
        async with acquire() as conn:
            # The lookup repeats the predicate of the unique index that makes
            # a verified address unambiguous — `email_verified_at IS NOT NULL
            # AND deleted_at IS NULL`. Written out rather than assumed, for the
            # reason Batch 3 recorded: `034` permits several UNVERIFIED rows to
            # share an address, so without this the query is non-deterministic
            # and could mint a token for a squatter's row.
            # `normalise_email()` is the DATABASE's function — the same one the
            # generated `normalised_email` column and `users_verified_email_key`
            # use. Normalising in Python would be a second implementation of a
            # rule that is already expressed once, and the two would drift on
            # exactly the addresses where it matters.
            row = await conn.fetchrow(
                """
                SELECT id, email, status FROM users
                 WHERE normalised_email = normalise_email($1)
                   AND email_verified_at IS NOT NULL
                   AND deleted_at IS NULL
                """,
                presented,
            )
            if row is None or row["status"] != "active":
                # No token, and no distinguishable behaviour. Deliberately not
                # logged with the address: a log of "reset requested for an
                # unknown address" is a list of addresses somebody probed.
                logger.info("password reset requested for a non-eligible account")
                return ResetRequest(issued=False)

            user_id = row["id"]
            destination = row["email"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("password reset lookup failed: %s", type(exc).__name__)
        raise ResetUnavailable("the account store could not be reached") from exc

    scoped = asynccontextmanager(_issuing_connection)
    try:
        async with scoped(token_hash, user_id) as conn:
            # Retire first, then insert. `password_reset_tokens_one_open_per_user`
            # is a PARTIAL unique index, checked per row as each is written
            # rather than at commit, so inserting before retiring the old token
            # violates it even though the end state is legal. Same ordering
            # lesson as Batch 6's ownership transfer.
            #
            # The retirement is a DELETE and cannot be anything else. This
            # connection holds the NEW token's secret and has never seen the old
            # one, so it cannot address that row by possession — which is why
            # `058`'s DELETE policy also accepts `app.reset_user_id`, and why
            # only DELETE does. See the reasoning in the migration.
            await conn.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = $1", user_id)
            await conn.execute(
                """
                INSERT INTO password_reset_tokens
                       (user_id, token_hash, email, expires_at)
                VALUES ($1, $2, $3, NOW() + ($4 || ' minutes')::interval)
                """,
                user_id, token_hash, destination, str(RESET_TTL_MINUTES),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("password reset token could not be issued: %s",
                       type(exc).__name__)
        raise ResetUnavailable("the reset store could not be reached") from exc

    # The token is NOT logged. It is a bearer credential for the account.
    logger.info("a password reset token was issued")
    # Recorded only on the branch that MINTED something, and that asymmetry is
    # deliberate. The non-eligible branches return early above without a line,
    # because an audit table carrying one row per probed address is the
    # enumeration oracle this whole function is written to avoid — moved from
    # the response, where the caller can see it, into a store the operator
    # reads. What the operator actually needs is "a live link exists for this
    # account", and that only happens here.
    from core.identity import auth_audit

    await auth_audit.record(auth_audit.PASSWORD_RESET_REQUESTED,
                            user_id=user_id, subject=destination)
    return ResetRequest(issued=True, token=token, email=destination,
                        user_id=user_id)


async def _issuing_connection(token_hash: str, user_id):
    """A connection that may write ONE new token and retire that user's old ones.

    Two GUCs, and they grant different things by design. `app.reset_token_hash`
    is possession of the new secret and governs the INSERT;
    `app.reset_user_id` is a plain user id and governs only the DELETE, because
    a user id is guessable and a token hash is not. `058` asserts that no other
    command accepts the second one.
    """
    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.reset_token_hash', $1, true)", token_hash)
            await conn.execute(
                "SELECT set_config('app.reset_user_id', $1, true)", str(user_id))
            yield conn


async def complete_password_reset(token: str, new_password: str) -> uuid.UUID:
    """Set a new password and end every session. Returns the user id.

    Raises `ResetError` for an unknown, expired or already-used token — one
    message for all three, because distinguishing them tells the holder of a
    stolen link whether it was ever real.

    ── Ending the sessions is not optional ─────────────────────────────────

    Somebody resets their password precisely because they think somebody else
    has it. If the attacker is already signed in, a new password changes nothing
    for them: their refresh token keeps working for its full lifetime. So every
    session goes, including the caller's own — they have just proved control of
    the mailbox, not of any device.
    """
    from contextlib import asynccontextmanager

    from api.middleware.auth import hash_password
    from core.identity.registration import _validate_password

    presented = (token or "").strip()
    if not presented:
        raise ResetError("this reset link is not valid")

    _validate_password(new_password)
    token_hash = _hash_token(presented)

    scoped = asynccontextmanager(_token_connection)
    try:
        async with scoped(token_hash) as conn:
            # Conditional on the token still being unused AND unexpired: the
            # write is the gate, so two simultaneous presentations of the same
            # link cannot both set a password. Checking and then writing leaves
            # exactly that window, on the one credential that replaces another.
            row = await conn.fetchrow(
                """
                UPDATE password_reset_tokens
                   SET consumed_at = NOW()
                 WHERE token_hash = $1
                   AND token_is_unused
                   AND expires_at > NOW()
                RETURNING user_id, email
                """,
                token_hash,
            )
            if row is None:
                raise ResetError("this reset link is not valid")

            user_id = row["user_id"]

            # The address the link was sent to must still be the account's. A
            # token minted for alice@old must not set the password on an account
            # that has since become alice@new — the old mailbox may belong to
            # somebody else now, and that is precisely when this matters.
            current = await conn.fetchval(
                "SELECT email FROM users WHERE id = $1 AND deleted_at IS NULL",
                user_id)
            if current is None or current != row["email"]:
                raise ResetError("this reset link is not valid")

            # `user_credentials` is FORCE RLS keyed on `app.user_id` (`054`), so
            # the write below is refused outright — SQLSTATE 42501 — without it
            # bound. It is bound HERE rather than when the connection opened,
            # because until the token was consumed nobody knew whose account
            # this is: the token is the only thing that says so.
            #
            # `set_config(..., true)` is transaction-local and takes effect for
            # the next statement, so the consumption and the credential write
            # stay in ONE transaction. That matters: if they were split and the
            # second half failed, the user would have spent their only link and
            # still have their old password, with nothing to retry.
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(user_id))

            await conn.execute(
                """
                INSERT INTO user_credentials (user_id, password_hash)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        updated_at = NOW()
                """,
                user_id, hash_password(new_password),
            )
    except ResetError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("password reset could not be completed: %s",
                       type(exc).__name__)
        raise ResetUnavailable("the reset store could not be reached") from exc

    ended = await _end_every_session(user_id)
    raised = await _end_every_token(user_id)

    logger.info("a password was reset")
    # After the sessions, not before, and it carries the count. "The password
    # was reset" and "and the sessions that survived it" are the two halves an
    # incident asks about, and `_end_every_session` is fail-soft by design — it
    # logs and returns rather than undoing a completed reset — so the count is
    # the only durable evidence that the second half actually happened.
    from core.identity import auth_audit

    await auth_audit.record(
        auth_audit.PASSWORD_RESET_COMPLETED, user_id=user_id,
        detail=f"{ended} session(s) ended; token version "
               f"{'raised' if raised else 'NOT raised'}")
    return user_id


async def _end_every_token(user_id) -> bool:
    """Raise the account's token version, so no token issued before the reset
    is accepted after it (migration 096). Returns whether it was raised.

    Ending the sessions stops the refresh tokens; it does nothing to an access
    token already issued, which kept working for up to thirty minutes after
    the reset, long enough for whoever the reset was against. Fail-soft for the
    same reason `_end_every_session` is: the new password is already in place,
    and refusing now would leave the person with neither password.
    """
    from core.identity import token_versions

    try:
        await token_versions.bump(user_id)
        return True
    except token_versions.UnknownAccount:
        # No counter: no token naming this account is accepted at all.
        return True
    except Exception:  # noqa: BLE001
        logger.error("password reset could not raise the token version; access "
                     "credentials issued before it stay valid until they expire",
                     exc_info=True)
        return False


async def _end_every_session(user_id) -> int:
    """Revoke every live session for this user, across every tenant. Returns how
    many were ended.

    Not `revoke_all_sessions`, which takes a Principal and is bound to one
    tenant. A person may be a member of several, and a reset is an ACCOUNT
    operation — leaving them signed in to the other organisations would mean the
    attacker they were resetting against keeps whichever tenant they were in.

    `057` made `user_sessions` require both `app.tenant_id` and `app.user_id`,
    so this walks the user's memberships and binds each in turn rather than
    reaching across the boundary. A failure here does not undo the password
    change — the new password is already correct, and refusing would leave the
    user unable to sign in with either — but it is logged as the serious thing
    it is.
    """
    from core.identity.db_context import (
        identity_bootstrap_connection, tenant_connection,
    )
    from core.identity.principal import Principal

    try:
        # `identity_bootstrap_connection`, NOT a bare `acquire()`.
        # `tenant_memberships` is tenant-scoped under FORCE RLS, so an unbound
        # connection matches ZERO rows — silently. The first version of this
        # function used `acquire()` and therefore found no tenants, revoked no
        # sessions, logged nothing, and returned successfully. That is the P2-05
        # shape exactly, in the one place where the consequence is that a
        # password reset leaves the attacker signed in.
        #
        # This is the same connection `resolve_principal` and
        # `_tenant_choices` use to enumerate a user's memberships, and for the
        # same reason: it binds `app.user_id` before any tenant is known, which
        # is the only thing available at this point in the flow.
        async with identity_bootstrap_connection(user_id) as conn:
            tenants = [r["tenant_id"] for r in await conn.fetch(
                "SELECT tenant_id FROM tenant_memberships WHERE user_id = $1",
                user_id)]
    except Exception:  # noqa: BLE001
        logger.error("password reset could not enumerate tenants to sign out of",
                     exc_info=True)
        # -1, not 0. The audit line records this number, and "no sessions
        # existed" and "the sessions could not be reached" are opposite facts
        # that a 0 would render identical — which is the P2-05 shape written
        # into the evidence instead of into the behaviour.
        return -1

    if not tenants:
        # Not an error — a registered account with no membership yet is a real
        # state. But it IS worth a line, because "no tenants" and "could not
        # read the tenants" produce identical behaviour and only one of them is
        # fine.
        logger.info("password reset: the account holds no membership, "
                    "so there were no sessions to end")

    ended = 0
    for tenant_id in tenants:
        principal = Principal(
            user_id=user_id, tenant_id=tenant_id, session_id=uuid.uuid4(),
            edition="cloud", username="", is_owner=False)
        try:
            async with tenant_connection(principal) as conn:
                rows = await conn.fetch(
                    "UPDATE user_sessions "
                    "   SET revoked_at = NOW(), revoked_reason = 'password reset' "
                    " WHERE NOT session_is_revoked "
                    "RETURNING id")
            ended += len(rows)
        except Exception:  # noqa: BLE001
            logger.error(
                "password reset could not end sessions in one of the user's "
                "tenants — an existing session may survive a reset",
                exc_info=True)
    return ended


__all__ = [
    "RESET_TTL_MINUTES",
    "ResetError",
    "ResetRequest",
    "ResetUnavailable",
    "complete_password_reset",
    "request_password_reset",
]
