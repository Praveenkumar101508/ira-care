"""
security/totp.py — shared TOTP second-factor checks (fail-closed).

The TOTP secret store (``totp_secrets``) and the pyotp verification were inline in
``api/routes/totp.py``. The Armory's high-risk approval needs the SAME check —
"is this a valid current code for this owner?" — so the logic lives here once and
both callers use it. No second implementation, no drift.

Every function fails **closed**: a missing enrollment, a DB error, or a bad code
returns False / "not enrolled" rather than raising into the caller, so a
momentarily unavailable factor can never be read as "approved".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ira.security.totp")


class TotpUnavailable(Exception):
    """The enrolment state could not be determined.

    Distinct from "not enrolled" on purpose, and the distinction is the whole
    point (P2-05). A caller deciding whether to demand a second factor must not
    read "I could not find out" as "there is no second factor" — that is the
    fail-OPEN direction, and it is exactly how the login path came to skip the
    challenge entirely.
    """


async def _row_for(principal, username: str, *, enabled_only: bool):
    """The enrolment row for `username`, under `principal`'s bound connection."""
    from core.identity.db_context import tenant_connection

    from core.runtime.plaintext_columns import exists

    async with tenant_connection(principal) as conn:
        # `secret` is named only while it exists. `063` drops it, and a
        # statement naming a dropped column raises rather than returning NULL —
        # which on this path is a 500 on a working second factor.
        legacy = await exists(conn, "totp_secrets", "secret")
        columns = ("secret, " if legacy else "NULL::text AS secret, ")
        # `id` and `enrolled_at` identify WHICH factor this is, not just whether
        # one exists. Re-enrolment binds its pending replacement to them, so a
        # replacement can only land on the factor whose code it was approved
        # against (see `verify_active_factor`).
        sql = ("SELECT " + columns +
               "secret_ciphertext, secret_nonce, encryption_key_version, "
               "id, enrolled_at "
               "FROM totp_secrets WHERE username=$1")
        if enabled_only:
            sql += " AND enabled=TRUE"
        return await conn.fetchrow(sql, username)


async def _use_row_secret(principal, row, fn):
    """Hand `fn` the plaintext seed for `row`, however it is stored.

    Encrypted rows go through `use_totp_secret`, so the plaintext exists only
    inside the callback. Legacy plaintext rows (not yet migrated) are passed
    through the same shape, so callers cannot tell the two apart and there is
    exactly one place that ever holds a seed.
    """
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret

    if row["secret_ciphertext"] is not None:
        if row["secret_nonce"] is None:
            # AES-GCM needs a nonce and it is not derivable, so this row cannot
            # be opened by anybody. `totp_secrets_encrypted_is_complete` makes
            # the state impossible on an ENCRYPTED row; it is still
            # representable mid-migration (ENCRYPTING, ciphertext written,
            # interrupted), and the important thing is which way we fail. Saying
            # so explicitly beats letting a None reach the cipher and reading
            # back a TypeError — same refusal, but one of them is diagnosable.
            raise TotpUnavailable("the enrolment has ciphertext but no nonce")
        try:
            return await use_totp_secret(
                principal, row["secret_ciphertext"], row["secret_nonce"],
                int(row["encryption_key_version"]), fn)
        except MfaKeyUnavailable as exc:
            raise TotpUnavailable("the MFA key is unavailable") from exc
    if row["secret"]:
        return fn(row["secret"])
    raise TotpUnavailable("the enrolment holds neither ciphertext nor plaintext")


async def _principal_for(username: str):
    from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal

    try:
        return await resolve_principal(username)
    except CloudIdentityNotAvailable as exc:
        raise TotpUnavailable("cloud identity is not available") from exc
    except Exception as exc:  # noqa: BLE001
        raise TotpUnavailable("could not resolve the principal") from exc


async def _fetch_secret(username: str) -> Optional[str]:
    """Deprecated shape kept for `is_enrolled`: whether an enrolment exists.

    Returns a NON-SECRET sentinel rather than the seed. Nothing outside
    `_use_row_secret` returns a plaintext TOTP secret any more (P2-05) — a
    function that hands one back is a function whose result can end up in a log
    line or a response body.
    """
    if not username:
        return None
    principal = await _principal_for(username)
    try:
        row = await _row_for(principal, username, enabled_only=False)
    except Exception as exc:  # noqa: BLE001 — never log the secret, only the failure
        logger.warning("totp secret lookup failed for %s: %s", username, type(exc).__name__)
        raise TotpUnavailable("the TOTP store could not be reached") from exc
    if row is None:
        return None
    if row["secret_ciphertext"] is None and not row["secret"]:
        return None
    return "<enrolled>"


#: Verdicts from `check_login_second_factor`. A tri-state, because "no factor
#: enrolled" and "wrong code" must lead to different responses and neither may
#: be confused with "I could not tell" (which raises instead).
NOT_ENROLLED = "not_enrolled"
FACTOR_OK = "ok"
FACTOR_BAD = "bad_code"


@dataclass(frozen=True)
class ActiveFactor:
    """Which enabled enrolment a verdict was reached against. Never the seed.

    `enrolled_at` changes every time the seed is replaced, so the pair names
    one generation of one row. Re-enrolment records it when it checks the
    current code and the replacement is written only if the row still holds
    that generation — otherwise a replacement approved against one factor
    could land on a different one.
    """

    row_id: str
    enrolled_at: datetime


async def verify_active_factor(
    username: str, code: Optional[str], *, principal=None,
) -> tuple[str, Optional[ActiveFactor]]:
    """`(verdict, factor)` for the caller's ENABLED enrolment.

    The verdict is the same tri-state `check_login_second_factor` returns, and
    it raises `TotpUnavailable` in the same cases. `factor` is None only when
    the verdict is `NOT_ENROLLED`.
    """
    if not username:
        return NOT_ENROLLED, None
    if principal is None:
        principal = await _principal_for(username)
    try:
        row = await _row_for(principal, username, enabled_only=True)
    except Exception as exc:  # noqa: BLE001 — never log the secret itself
        logger.warning("totp enrolment lookup failed for %s: %s",
                       username, type(exc).__name__)
        raise TotpUnavailable("the TOTP store could not be reached") from exc

    if row is None:
        return NOT_ENROLLED, None
    factor = ActiveFactor(row_id=str(row["id"]), enrolled_at=row["enrolled_at"])
    if not code or not code.strip():
        return FACTOR_BAD, factor

    def _verify(secret: str) -> bool:
        import pyotp
        return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=1))

    ok = await _use_row_secret(principal, row, _verify)
    return (FACTOR_OK if ok else FACTOR_BAD), factor


async def check_login_second_factor(
    username: str, code: Optional[str], *, principal=None,
) -> str:
    """Decide the second factor for a login, without ever returning the seed.

    Replaces the earlier `enabled_secret_for`, which handed `auth/routes.py` a
    plaintext TOTP seed to verify itself. That was fine while the column was
    plaintext and is exactly wrong now: the seed would be decrypted, returned
    across a module boundary, and held in a local variable in a request handler
    — three more places it can reach a traceback or a log. The verification now
    happens inside the same callback the plaintext lives in, and only a verdict
    crosses the boundary.

    Raises `TotpUnavailable` when the enrolment state cannot be determined. The
    caller must refuse the login rather than proceed without the factor.

    `enabled` matters: `/auth/totp/enroll` stores a secret and only a successful
    `/auth/totp/verify` sets it, so demanding a code mid-enrolment would lock
    somebody out of their own account.

    `principal` lets a caller that has ALREADY resolved the identity pass it in
    rather than have this function resolve it again. Cloud login does: it
    verifies a password, resolves the principal, and then asks about the second
    factor for *that* principal. Resolving a second time here would be a second
    answer to "who is signing in", and in cloud it can genuinely differ — the
    resolution takes no tenant, so a user who has since gained a second
    membership resolves to `AmbiguousTenant` and the factor check would raise
    `TotpUnavailable` for an account that is perfectly enrolled.

    It is not a way to supply an identity the caller has not proven. The
    argument only selects the connection the enrolment row is read under, and
    that connection is bound by RLS to that principal's own rows: passing
    somebody else's principal reads somebody else's enrolment and then demands
    *their* code, which fails. It cannot be used to skip a factor.
    """
    verdict, _factor = await verify_active_factor(username, code, principal=principal)
    return verdict


async def is_enrolled(username: str) -> bool:
    """True if the user has a stored TOTP secret.

    Propagates `TotpUnavailable` rather than swallowing it. A caller gating
    access on a second factor has to be able to tell "no factor is enrolled"
    from "I could not find out"; collapsing the two here is what let the login
    path skip the challenge (P2-05).
    """
    return await _fetch_secret(username) is not None


async def verify(username: str, code: str, *, valid_window: int = 1) -> bool:
    """True iff ``code`` is a valid current TOTP for ``username``.

    ``valid_window=1`` accepts the adjacent 30s steps for clock skew. Fail-closed:
    not enrolled, an empty/garbage code, or any error → False. The seed is
    verified inside the decryption callback and never returned.
    """
    if not username or not code or not code.strip():
        return False
    try:
        principal = await _principal_for(username)
        row = await _row_for(principal, username, enabled_only=False)
    except TotpUnavailable:
        # Verification asserts something positive; an unreachable store cannot
        # support it. (`is_enrolled` propagates instead, because a *gate* must
        # not read an unknown as an absent factor.)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("totp verify lookup failed for %s: %s", username, type(exc).__name__)
        return False
    if row is None:
        return False

    def _check(secret: str) -> bool:
        import pyotp
        return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=valid_window))

    try:
        return bool(await _use_row_secret(principal, row, _check))
    except TotpUnavailable:
        return False
    except Exception as exc:  # noqa: BLE001 — a broken verifier is a failed verification
        logger.warning("totp verify failed for %s: %s", username, type(exc).__name__)
        return False


__all__ = ["is_enrolled", "verify", "check_login_second_factor",
           "verify_active_factor", "ActiveFactor",
           "NOT_ENROLLED", "FACTOR_OK", "FACTOR_BAD", "TotpUnavailable"]
