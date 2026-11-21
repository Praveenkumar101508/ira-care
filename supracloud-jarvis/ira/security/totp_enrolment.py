"""
security/totp_enrolment.py — a replacement second factor stays pending until it works.

── The defect this exists for (P0-01) ──────────────────────────────────────

`POST /auth/totp/enroll` used to overwrite the stored seed of an ENABLED factor
in place, keeping it enabled. Two consequences:

  * whoever held a bearer token — including one copied by somebody else — could
    swap the owner's live second factor for their own and lock the owner out;
  * even an honest re-enrolment left the account demanding codes from an
    authenticator nobody had confirmed was set up. Abandon the QR code half-way
    and the account cannot be signed into.

Re-authentication (password, then a current code — `api/routes/totp.py`) closes
the first. This module closes the second: the replacement is not written at all
until a code from the NEW authenticator proves it works. Until then the old
factor is untouched and keeps working.

── Where the pending seed lives, and why there ─────────────────────────────

Nowhere on the server. `/enroll` seals the new seed under the user's own MFA key
(`security.mfa_crypto.seal_totp_secret`, AES-GCM with the tenant, user and key
version as associated data — exactly what the `totp_secrets` row stores) and
hands the sealed form back inside a short-lived signed token. `/verify` opens it
with the same key, checks the new code, and only then writes those same sealed
bytes into the row.

The alternatives were considered and refused:

  * a pending column pair on `totp_secrets` needs a migration — a protected
    change of its own — for state that is meant to live ten minutes;
  * a Redis key puts second-factor material in a store this codebase treats as
    fail-soft and disposable, and turns a Redis outage into "cannot re-enrol";
  * resetting `enabled` until the new code is checked drops the account to a
    password-only sign-in for the whole window — strictly worse than today.

What the token carries is ciphertext the client cannot open (the key never
leaves the server) and cannot forge (it is signed, and AES-GCM authenticates the
seed against the tenant and user it was sealed for). The plaintext seed is
returned once in the same response, as it always was: the person has to scan it.

── What binds a token to one replacement ───────────────────────────────────

  * signed with a key DERIVED from `IRA_SECRET_KEY` for this purpose alone, so a
    re-enrolment token can never verify as an access token, nor an access token
    as one of these;
  * `aud` is fixed, `exp` is ten minutes;
  * subject, user id and tenant id must equal the confirming caller's;
  * `ver` is the account's token version when it was issued, so "log out
    everywhere" cancels a pending replacement along with every session — the
    same rule every IRA token follows (`tests/test_token_version_binding.py`);
  * the factor it replaces — row id and enrolment time — is recorded, and the
    write happens only while the row still holds that factor. A second
    re-enrolment confirmed first makes an older token useless rather than a way
    back to an older seed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: How long a pending replacement stays confirmable. Long enough to open an
#: authenticator app and scan a code; short enough that a forgotten one expires.
REENROLMENT_TTL_SECONDS = 600

_AUDIENCE = "ira:totp-reenrolment"
_KEY_LABEL = b"ira/totp-reenrolment/v1"
_ALGORITHM = "HS256"


class EnrolmentTokenInvalid(Exception):
    """The token is malformed, forged, expired, or belongs to someone else.

    One exception, one response: the caller is not told which, because the
    difference is only useful to someone probing tokens.
    """


class EnrolmentUnavailable(Exception):
    """The account's token version could not be read, so no token is issued or
    accepted. Never answered as "valid": callers refuse with a 503."""


async def _token_version(principal) -> int:
    from core.identity import token_versions

    try:
        return await token_versions.current(principal.user_id)
    except (token_versions.TokenVersionUnavailable, token_versions.UnknownAccount) as exc:
        raise EnrolmentUnavailable("the account's token version is unavailable") from exc


@dataclass(frozen=True)
class PendingFactor:
    """A sealed replacement seed and the factor it is allowed to replace."""

    ciphertext: bytes
    nonce: bytes
    key_version: int
    replaces_row_id: str
    replaces_enrolled_at: datetime


def _signing_key() -> bytes:
    """A key for these tokens only, derived from the deployment secret.

    HMAC with a fixed label rather than the secret itself: the access-token
    signer and this one must never accept each other's output, and deriving is
    what guarantees it without a second secret to configure and rotate.
    """
    from config import get_settings

    secret = get_settings().ira_secret_key.encode("utf-8")
    return hmac.new(secret, _KEY_LABEL, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


async def issue_reenrolment_token(
    *, subject: str, principal, replaces, ciphertext: bytes, nonce: bytes,
    key_version: int,
) -> tuple[str, int]:
    """Sign a pending replacement. Returns `(token, seconds_until_expiry)`.

    `replaces` is the `security.totp.ActiveFactor` whose current code the caller
    has just presented. Raises `EnrolmentUnavailable`.
    """
    import jwt

    now = datetime.now(timezone.utc)
    claims = {
        "aud": _AUDIENCE,
        "sub": subject,
        "ver": await _token_version(principal),
        "uid": str(principal.user_id),
        "tid": str(principal.tenant_id),
        "fid": str(replaces.row_id),
        "gen": replaces.enrolled_at.isoformat(),
        "ct": _b64(ciphertext),
        "nn": _b64(nonce),
        "kv": int(key_version),
        "iat": now,
        "exp": now + timedelta(seconds=REENROLMENT_TTL_SECONDS),
    }
    token = jwt.encode(claims, _signing_key(), algorithm=_ALGORITHM)
    return token, REENROLMENT_TTL_SECONDS


async def read_reenrolment_token(token: str, *, subject: str, principal) -> PendingFactor:
    """Verify a token for the confirming caller and return what it carries.

    Raises `EnrolmentTokenInvalid` for every failure of the token itself, and
    `EnrolmentUnavailable` when the account's token version cannot be read.
    """
    import jwt
    from jwt import PyJWTError

    if not token or not isinstance(token, str):
        raise EnrolmentTokenInvalid("no token")
    try:
        claims = jwt.decode(
            token, _signing_key(), algorithms=[_ALGORITHM], audience=_AUDIENCE,
            options={"require": ["aud", "sub", "exp", "iat", "ver"]},
        )
        if claims.get("sub") != subject:
            raise EnrolmentTokenInvalid("subject mismatch")
        if claims.get("uid") != str(principal.user_id):
            raise EnrolmentTokenInvalid("user mismatch")
        if claims.get("tid") != str(principal.tenant_id):
            raise EnrolmentTokenInvalid("tenant mismatch")
        # `>`, as `assert_token_version_current` compares: a token issued before
        # the last "log out everywhere" is dead.
        if await _token_version(principal) > int(claims["ver"]):
            raise EnrolmentTokenInvalid("issued before the last sign-out everywhere")
        return PendingFactor(
            ciphertext=_unb64(claims["ct"]),
            nonce=_unb64(claims["nn"]),
            key_version=int(claims["kv"]),
            replaces_row_id=str(claims["fid"]),
            replaces_enrolled_at=datetime.fromisoformat(claims["gen"]),
        )
    except (EnrolmentTokenInvalid, EnrolmentUnavailable):
        raise
    except (PyJWTError, KeyError, TypeError, ValueError) as exc:
        # The type only: a decode error can carry the token, and the token
        # carries sealed key material.
        raise EnrolmentTokenInvalid(type(exc).__name__) from None


__all__ = [
    "REENROLMENT_TTL_SECONDS", "EnrolmentTokenInvalid", "EnrolmentUnavailable",
    "PendingFactor",
    "issue_reenrolment_token", "read_reenrolment_token",
]
