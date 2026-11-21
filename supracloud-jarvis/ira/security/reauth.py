"""
security/reauth.py — prove the person holding a session still knows its password.

A bearer access token proves somebody signed in within the last half hour. It
does not prove that the somebody presenting it now is the account holder: a
token copied out of a browser, a log or a proxy works just as well for whoever
copied it. For most requests that is the accepted trade. For a request that
changes how the account itself is protected — replacing its second factor — it
is not, because the change outlives the stolen token and locks the real holder
out (P0-01).

So those requests ask for the account password again and check it here, against
the same credential the sign-in path checks:

  * the private edition's owner — the configured owner password, through
    `security.delegated.verify_owner_password`, which is also what access grants
    use and which ignores DEV_MODE on purpose: a live session must never stand
    in for the password;
  * a household (delegated) account — its own bcrypt hash, through
    `security.delegated.authenticate_delegated`, the sign-in path's fallback;
  * a cloud account — its `user_credentials` hash, through
    `core.identity.cloud_login.verify_cloud_password`, which the cloud sign-in
    path's lookup also serves.

There is no second password store and no second hashing scheme: every branch
ends in the bcrypt verification the matching sign-in already performs.

What is checked is always the password of the TOKEN'S subject. The caller cannot
name a different account, so a correct password for some other account proves
nothing here.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("ira.security.reauth")


class ReauthUnavailable(Exception):
    """The password could not be checked — the credential store was unreachable.

    Distinct from a wrong password for the same reason `TotpUnavailable` is
    distinct from "not enrolled": "I could not tell" must not be answered as
    "no", and it must never be answered as "yes". Callers refuse with a 503.
    """


async def verify_account_password(username: str, password: str, *, principal) -> bool:
    """True iff `password` is the current password of the account `username`.

    `username` is the authenticated subject (the token's `sub`) and `principal`
    the identity it resolved to. Raises `ReauthUnavailable` when the answer
    cannot be determined; returns False for every other refusal.
    """
    if not username or not password:
        return False

    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() == EDITION_CLOUD:
        from core.identity.cloud_login import (
            CloudLoginUnavailable, verify_cloud_password,
        )

        try:
            return await verify_cloud_password(
                username, password, user_id=principal.user_id)
        except CloudLoginUnavailable as exc:
            raise ReauthUnavailable("the credential store could not be reached") from exc

    from api.middleware.auth import is_owner
    from security.delegated import authenticate_delegated, verify_owner_password

    # `is_owner`, the one owner check every gate uses (3C-04). It selects which
    # credential to check, never whether to check one: the owner's password is
    # still verified below, and anyone else goes to their own hash.
    if is_owner(username):
        return verify_owner_password(password)
    # `authenticate_delegated` fails closed on a database error (returns
    # False), which is the right direction for a password check: a wrong
    # answer here refuses a change, it never grants one.
    return await authenticate_delegated(username, password)


__all__ = ["ReauthUnavailable", "verify_account_password"]
