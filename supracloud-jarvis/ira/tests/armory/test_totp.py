"""Fail-closed behaviour of the shared TOTP helper (security/totp.py).

With no database and no enrolled secret, an *approval* check must resolve to
"not approved" — a momentarily unavailable second factor can never be read as a
pass.

A **gate** is the other direction, and P2-05 changed it. `is_enrolled` used to
return False when it could not reach the store, and the login path read that as
"this person has no second factor" and skipped the challenge. So the failure
mode of the MFA store was to turn MFA off. It now raises `TotpUnavailable`, and
the caller refuses the login. `verify()` keeps returning False, because it
asserts something positive and an unreachable store cannot support it.
"""
import pytest

from security import totp


async def test_verify_fail_closed_without_secret():
    # No DB / no secret → not verifiable, regardless of the code.
    assert await totp.verify("owner", "123456") is False
    assert await totp.verify("owner", "") is False
    assert await totp.verify("", "123456") is False


async def test_is_enrolled_raises_rather_than_answering_no_without_a_db():
    """**Deliberate contract change (P2-05).** This asserted `False`.

    `False` from a gate means "this person has no second factor", and
    `auth/routes.py` acted on exactly that: no factor found, no challenge
    demanded. So an unreachable database — or, as it turned out, a correctly
    configured one reached on an unbound connection — silently disabled MFA for
    everybody. That is fail-OPEN wearing the name fail-closed.

    `is_enrolled` now raises `TotpUnavailable` when it cannot determine the
    answer, and the login path turns that into a 503 rather than a sign-in. An
    empty username still returns False, because "nobody" genuinely has no
    enrolment — that is an answer, not a failure to obtain one.

    `verify()` still returns False on the same condition, and the asymmetry is
    the point: verification is a positive assertion ("this code is valid") and
    an unreachable store cannot support it, whereas a gate must not read an
    unknown as an absence.
    """
    assert await totp.is_enrolled("") is False

    with pytest.raises(totp.TotpUnavailable):
        await totp.is_enrolled("owner")
