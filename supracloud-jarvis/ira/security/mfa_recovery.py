"""MFA recovery codes (Phase 2 batch 7).

P2-05 encrypted the second factor and made an undeterminable enrolment refuse
the login. Both are right, and together they mean a user who loses their
authenticator is locked out with no way back — the stronger the factor, the
worse that gets. These are the way back.

── A recovery code IS a second factor ──────────────────────────────────────

Not a lesser one. Anything that accepts a recovery code in place of a TOTP
challenge is, from an attacker's point of view, the second factor — so it is
stored, scoped and consumed with the same care. `mfa_recovery_codes` carries the
same both-predicates RLS as `user_mfa_keys` and `totp_secrets`: a tenant
administrator who could read a member's codes could sign in as them.

── Recovery does not change who you are ────────────────────────────────────

It substitutes for the FACTOR and nothing else. `consume_recovery_code` returns
a verdict, never a principal — the caller has already resolved one, and the
account state checks that resolution performs (verified, active, membership
grants access) are untouched. A suspended member cannot recover their way back
in, because recovery was never what was refusing them.

── Why bcrypt here and SHA-256 in 054/055 ──────────────────────────────────

Those store 256-bit random tokens, where the only property needed is that the
stored form cannot be presented, and a slow hash on an unauthenticated path is a
denial-of-service lever. A recovery code is typed by a human, so it cannot be
256 bits — this uses ~50, which is ample against online guessing and not against
an offline attack on a stolen table. That is the low-entropy case, and it is
what a slow hash is for. Verification happens only after a correct password,
behind the login rate limit, so the DoS argument does not apply.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

logger = logging.getLogger("ira.security.mfa_recovery")

#: How many codes a regeneration issues. Ten is the common choice and the
#: reasoning is mundane: enough that losing a printout mid-list is survivable,
#: few enough that the bcrypt scan on redemption stays bounded.
RECOVERY_CODE_COUNT = 10

#: Crockford-style base32 without the characters people transcribe wrongly —
#: no I, L, O, U. 10 characters over a 28-symbol alphabet is ~48 bits.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LENGTH = 10


class RecoveryError(RuntimeError):
    """Recovery could not proceed. Safe to show to the caller."""


class RecoveryUnavailable(RecoveryError):
    """The store could not be reached. Fail closed — never fail open."""


@dataclass(frozen=True, slots=True)
class RecoveryCodes:
    """A freshly issued set. `codes` is plaintext, exactly once."""

    generation: int
    codes: tuple[str, ...]


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"{raw[:5]}-{raw[5:]}"


def _normalise(code: str) -> str:
    """Strip the presentation so a user may type it however they see it.

    Upper-cased and de-hyphenated. Nothing else is stripped: silently accepting
    arbitrary characters would widen what counts as a match.
    """
    return (code or "").strip().upper().replace("-", "").replace(" ", "")


async def generate_recovery_codes(principal, count: int = RECOVERY_CODE_COUNT) -> RecoveryCodes:
    """Issue a new set, invalidating every earlier one.

    Returns the plaintext codes once. They are never stored, never logged and
    cannot be recovered — regenerating is the only way to get a usable set back.

    ── The generation bump, not a delete ───────────────────────────────────

    Superseding is a monotonic counter rather than DELETE-then-INSERT. A crash
    between the two would leave an account with no recovery codes and no record
    it ever had any, and the window is exactly when somebody is mid-recovery.
    The consuming query requires the current generation, so old rows are inert
    the instant the new set commits — whether or not they are ever cleaned up.
    """
    from api.middleware.auth import hash_password
    from core.identity.db_context import tenant_connection

    wanted = max(1, min(int(count or RECOVERY_CODE_COUNT), 20))
    codes = tuple(_new_code() for _ in range(wanted))

    try:
        async with tenant_connection(principal) as conn:
            current = await conn.fetchval(
                """
                SELECT COALESCE(max(generation), 0) FROM mfa_recovery_codes
                 WHERE user_id = $1
                """,
                principal.user_id,
            )
            generation = int(current) + 1

            for code in codes:
                # `tenant_id` DEFAULTs from the bound GUC, so the INSERT names
                # only the user — the value the row carries and the value the
                # policy checks are one expression.
                await conn.execute(
                    """
                    INSERT INTO mfa_recovery_codes (user_id, code_hash, generation)
                    VALUES ($1, $2, $3)
                    """,
                    principal.user_id, hash_password(_normalise(code)), generation,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("recovery code generation failed: %s", type(exc).__name__)
        raise RecoveryUnavailable("recovery codes could not be issued") from exc

    # The codes are NOT logged. They are bearer second factors.
    logger.info("issued a new set of recovery codes")
    # The generation number, and nothing else about the codes. It says which set
    # is live, so an incident can tell "the codes they were given in March" from
    # "the codes somebody issued last night" — which is what an attacker who
    # reached the settings page does, since issuing a set invalidates the
    # owner's and hands out a fresh way past the second factor.
    from core.identity import auth_audit

    await auth_audit.record(
        auth_audit.RECOVERY_CODES_ISSUED, user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        detail=f"generation {generation}, {len(codes)} code(s)")
    return RecoveryCodes(generation=generation, codes=codes)


async def consume_recovery_code(principal, code: str) -> bool:
    """Spend one code. True if it was valid and is now spent.

    Single use, and the write is what decides: the UPDATE is conditional on the
    row still being unconsumed, so two simultaneous presentations of the same
    code cannot both succeed. Checking and then writing would leave exactly that
    window, and this is the one credential a user has left.

    Returns False rather than raising for a wrong code — the caller decides what
    a failed second factor means, and `check_login_second_factor` already has a
    verdict vocabulary for it.
    """
    from api.middleware.auth import verify_password
    from core.identity.db_context import tenant_connection

    presented = _normalise(code)
    if not presented:
        return False

    try:
        async with tenant_connection(principal) as conn:
            generation = await conn.fetchval(
                "SELECT max(generation) FROM mfa_recovery_codes WHERE user_id = $1",
                principal.user_id,
            )
            if generation is None:
                return False

            # Only the CURRENT generation, and only unconsumed rows. The scan is
            # bounded by RECOVERY_CODE_COUNT; bcrypt cannot be indexed, which is
            # the cost of the slow hash and the reason the count is small.
            rows = await conn.fetch(
                """
                SELECT id, code_hash FROM mfa_recovery_codes
                 WHERE user_id = $1 AND generation = $2 AND code_is_unused
                """,
                principal.user_id, generation,
            )

            for row in rows:
                try:
                    if not verify_password(presented, row["code_hash"]):
                        continue
                except Exception:  # noqa: BLE001 — a malformed stored hash
                    logger.warning("a stored recovery hash could not be read")
                    continue

                # Conditional on still being unused: the gate is the write.
                spent = await conn.fetchval(
                    """
                    UPDATE mfa_recovery_codes SET consumed_at = NOW()
                     WHERE id = $1 AND code_is_unused
                    RETURNING id
                    """,
                    row["id"],
                )
                if spent is not None:
                    logger.info("a recovery code was used")
                    return True
                # Lost the race to a concurrent presentation of the SAME code.
                # Keep scanning rather than returning: another row may still
                # match, and returning True here would spend nothing.
                continue
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED. "I could not check" must never read as "the factor
        # passed" — that is P2-05 restated for the substitute factor.
        logger.warning("recovery code check failed: %s", type(exc).__name__)
        raise RecoveryUnavailable("the recovery store could not be reached") from exc

    return False


async def remaining_recovery_codes(principal) -> int:
    """How many unused codes the current generation still holds."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        return int(await conn.fetchval(
            """
            SELECT count(*) FROM mfa_recovery_codes
             WHERE user_id = $1 AND code_is_unused
               AND generation = (SELECT max(generation) FROM mfa_recovery_codes
                                  WHERE user_id = $1)
            """,
            principal.user_id,
        ) or 0)


__all__ = [
    "RECOVERY_CODE_COUNT",
    "RecoveryCodes",
    "RecoveryError",
    "RecoveryUnavailable",
    "consume_recovery_code",
    "generate_recovery_codes",
    "remaining_recovery_codes",
]
