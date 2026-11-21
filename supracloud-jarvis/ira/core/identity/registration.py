"""Self-serve account creation and email verification (Phase 2 batch 2).

The account half of cloud identity. Batch 1 made a verified user with a
membership resolvable; this creates the user and proves the address. It
deliberately stops short of granting anything.

── A verified account is an identity, not an authorisation ─────────────────

Nothing here creates a tenant, a workspace or a membership. After verification
the user still resolves to nothing, because `_resolve_cloud_principal` requires
a membership that grants access and there is none. That is a representable and
tested state, not an oversight — provisioning is the next batch, and keeping
the two apart means "registered but not provisioned" is something the system
can hold rather than a gap between two half-applied writes.

── Enumeration is the design constraint ────────────────────────────────────

`POST /auth/register` is unauthenticated and public when enabled, so its
response is an oracle unless every branch looks identical. Registering an
address that already exists returns exactly what registering a new one returns,
takes a comparable amount of work, and sends no signal the caller can read. The
one asymmetry that cannot be removed — an existing user gets no new
verification email — is invisible to the requester.

`RegistrationOutcome.created` exists for the CALLER INSIDE the process (tests,
metrics, the mail dispatcher deciding whether to send). It must never reach a
response body, and `test_registration_response_is_identical...` is what keeps
that true at the route.

── The token is never stored ───────────────────────────────────────────────

Only its SHA-256. `user_sessions.token_hash` set the precedent and recorded the
reason: *"a session table that stores usable tokens turns a read of this table
into account takeover."* The same applies here — a readable verification token
is an account takeover for any address whose mail the attacker cannot read.

The token is returned once, from `register_account`, for the mail path to send.
It is never logged, never returned again, and cannot be recovered from the row.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("ira.identity.registration")

#: Minimum password length. Deliberately a floor and not a "complexity policy":
#: composition rules push users towards predictable substitutions, and length is
#: the property that actually costs an attacker. bcrypt truncates at 72 bytes,
#: which is the ceiling that matters and is enforced below.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72


class RegistrationError(RuntimeError):
    """Registration could not proceed. Safe to show to the caller."""


class RegistrationDisabled(RegistrationError):
    """Self-serve registration is not enabled on this deployment."""


class VerificationError(RuntimeError):
    """A verification token could not be honoured.

    One type for every failure — unknown, expired, already used, or issued for
    an address the account no longer has. The caller must not be able to tell
    which: "this token was already used" tells an attacker holding a stolen
    link that the address is real and the account is live.
    """


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """What registration did. `token` is present only for a new account."""

    #: True when this call created a user. INTERNAL ONLY — see the module
    #: header. Putting this in a response body makes the endpoint an
    #: enumeration oracle.
    created: bool
    #: The verification token, in the clear, exactly once. Only the mail path
    #: should ever hold it. `None` when nothing was created.
    token: Optional[str] = None
    user_id: Optional[uuid.UUID] = None
    #: The address the token was minted for, so the mail path sends to the
    #: address the DATABASE holds rather than the string the caller typed.
    #: Batch 9: `resend_verification` needs it, and taking it from the request
    #: would let a caller name where somebody else's link is delivered.
    email: Optional[str] = None


def _hash_token(token: str) -> str:
    """SHA-256 hex. Not bcrypt, and the difference is deliberate.

    A password is low-entropy and needs a slow hash to survive an offline
    attack on the stored value. A 256-bit random token is not guessable, so the
    only property needed is that the stored form cannot be presented — and a
    verification lookup happens on an unauthenticated path where a deliberately
    slow hash would be a denial-of-service lever.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise RegistrationError(
            f"a password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        # bcrypt silently truncates beyond 72 bytes, so a longer password is
        # not the password the user thinks it is. Refusing beats accepting a
        # secret and quietly storing a prefix of it.
        raise RegistrationError(
            f"a password must be at most {MAX_PASSWORD_BYTES} bytes"
        )


def _registration_enabled() -> bool:
    from config import get_settings

    return bool(getattr(get_settings(), "ira_cloud_registration_enabled", False))


def _ttl_hours() -> int:
    from config import get_settings

    return int(getattr(get_settings(), "ira_email_verification_ttl_hours", 24) or 24)



@asynccontextmanager
async def _token_connection(token_hash: str):
    """A connection that may see exactly ONE verification token: this one.

    `email_verification_tokens` is FORCE RLS with policies keyed on
    `app.verification_token_hash`, so a connection sees only the row whose hash
    it has already presented, and an unbound one sees none.

    Local to this module rather than in `db_context` on purpose. That module's
    helpers all take a Principal, and the one that does not —
    `identity_bootstrap_connection` — is pinned by an architecture guard for
    exactly that reason. Adding a second "no principal required" entry point to
    the shared surface would make the pattern look ordinary; keeping it here
    means the only thing that can open a token-scoped connection is the module
    that owns tokens.
    """
    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.verification_token_hash', $1, true)",
                token_hash,
            )
            yield conn


async def register_account(email: str, password: str) -> RegistrationOutcome:
    """Create an unverified account and mint a verification token.

    Returns the same *shape* whether or not the address was already taken. The
    caller must not branch on `created` in anything the requester can observe.
    """
    if not _registration_enabled():
        raise RegistrationDisabled(
            "self-serve registration is not enabled on this deployment"
        )

    if not email or "@" not in email:
        raise RegistrationError("a valid email address is required")
    _validate_password(password)

    from api.middleware.auth import hash_password
    from core.identity.db_context import identity_bootstrap_connection
    from utils.db import acquire

    email = email.strip()

    # `users` carries no RLS, so the existence check runs on an ordinary
    # connection. `normalise_email()` is the database's own function — the same
    # one the generated column uses — so this cannot drift from what the unique
    # index enforces.
    async with acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM users WHERE normalised_email = normalise_email($1) "
            "AND deleted_at IS NULL",
            email,
        )

    if existing is not None:
        # Hash anyway. Not for the stored value — nothing is stored — but so
        # that "address taken" and "address new" cost the same wall-clock. This
        # is the same reason `authenticate_user` hashes against a dummy on a
        # username miss (P2-01).
        hash_password(password)
        logger.info("registration for an address that already exists — no-op")
        return RegistrationOutcome(created=False)

    user_id = uuid.uuid4()
    password_hash = hash_password(password)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=_ttl_hours())

    try:
        async with acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status) "
                "VALUES ($1,$2,$3,'password','active')",
                user_id, email, email.split("@", 1)[0],
            )
    except Exception as exc:  # noqa: BLE001
        # Lost a race with a concurrent registration for the same address. The
        # requester is told what they would have been told a moment later
        # anyway — nothing — rather than seeing a database error that confirms
        # the address exists.
        logger.info("registration lost a race: %s", type(exc).__name__)
        return RegistrationOutcome(created=False)

    # The credential insert is bound to the user just created:
    # `user_credentials` is FORCE RLS keyed on `app.user_id`, so an unbound
    # INSERT is refused by the policy's WITH CHECK rather than silently
    # succeeding. This is the second call site of the bootstrap connection and
    # it is on the architecture guard's allow-list deliberately.
    #
    # Not one transaction with the user row, and that is a trade made with open
    # eyes: `identity_bootstrap_connection` opens its own transaction, so
    # sharing one would mean inlining its GUC binding here and losing the
    # single definition of what a user-bound connection is. The failure mode if
    # this half does not complete is a user row with no credential — an account
    # that cannot authenticate and cannot be registered again. `_orphaned` in
    # the tests pins that state so it is a known, findable condition rather
    # than a mystery; repairing it belongs with the password-reset flow, which
    # is the tool that fixes it.
    async with identity_bootstrap_connection(user_id) as conn:
        await conn.execute(
            "INSERT INTO user_credentials (user_id,password_hash) VALUES ($1,$2)",
            user_id, password_hash,
        )

    token_hash = _hash_token(token)
    async with _token_connection(token_hash) as conn:
        await conn.execute(
            "INSERT INTO email_verification_tokens "
            "(user_id,token_hash,email,expires_at) VALUES ($1,$2,$3,$4)",
            user_id, token_hash, email, expires_at,
        )

    # The token is NOT logged. It is a bearer credential for this address.
    logger.info("registered a new account, verification pending")
    return RegistrationOutcome(created=True, token=token, user_id=user_id,
                               email=email)


async def resend_verification(email: str) -> RegistrationOutcome:
    """Mint a fresh verification token for an account still awaiting one.

    Returns an outcome with no token for every case that produces nothing — an
    unknown address, one that is already verified, a deleted account. The CALLER
    must answer identically regardless: this endpoint takes an address and is
    unauthenticated, so any observable difference is the same enumeration oracle
    `register_account` exists to avoid.

    ── Why the old tokens are not retired ──────────────────────────────────

    `email_verification_tokens` deliberately has no one-open-per-user index, and
    resending leaves earlier tokens alive until they expire. That is the right
    trade for THIS token and not for a reset token: a verification link proves
    an address rather than granting a credential, and the common reason to
    resend is that the first mail went astray — invalidating it would break the
    case where it arrives late instead. Reset tokens are superseded precisely
    because they do grant a credential.

    ── The address comes from the database ─────────────────────────────────

    The token is minted against `users.email`, not the string the caller typed,
    and the outcome carries it so the mail path sends there. Taking the
    destination from the request would let a caller ask for somebody else's
    verification link to be delivered to an address of their choosing.
    """
    if not _registration_enabled():
        raise RegistrationDisabled(
            "self-serve registration is not enabled on this deployment"
        )

    from utils.db import acquire

    presented = (email or "").strip()
    if not presented:
        return RegistrationOutcome(created=False)

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email FROM users
             WHERE normalised_email = normalise_email($1)
               AND email_verified_at IS NULL
               AND deleted_at IS NULL
             ORDER BY created_at
             LIMIT 1
            """,
            presented,
        )

    if row is None:
        # Not logged with the address. A log of "resend requested for an
        # unknown address" is a list of addresses somebody probed.
        logger.info("verification resend for an address with nothing pending")
        return RegistrationOutcome(created=False)

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=_ttl_hours())
    token_hash = _hash_token(token)

    async with _token_connection(token_hash) as conn:
        await conn.execute(
            "INSERT INTO email_verification_tokens "
            "(user_id,token_hash,email,expires_at) VALUES ($1,$2,$3,$4)",
            row["id"], token_hash, row["email"], expires_at,
        )

    logger.info("re-issued a verification token")
    return RegistrationOutcome(created=False, token=token, user_id=row["id"],
                               email=row["email"])


async def verify_email(token: str) -> uuid.UUID:
    """Consume a verification token and mark the address verified.

    Returns the user id. Raises `VerificationError` for every failure, without
    distinguishing them — see the class docstring.

    Single-use and atomic: the UPDATE that consumes the token is conditional on
    it still being unconsumed, so two simultaneous presentations of the same
    link cannot both succeed. Checking `consumed_at` and then writing it would
    leave exactly that window.
    """
    if not token or not token.strip():
        raise VerificationError("this verification link is not valid")

    token_hash = _hash_token(token.strip())

    async with _token_connection(token_hash) as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_verification_tokens t
               SET consumed_at = NOW()
              FROM users u
             WHERE t.token_hash  = $1
               AND t.consumed_at IS NULL
               AND t.expires_at  > NOW()
               AND u.id          = t.user_id
               AND u.deleted_at  IS NULL
               -- The address the token was issued for must still be the
               -- account's address. Without this a token minted for
               -- alice@old verifies alice@new, marking an address proven
               -- that nobody proved.
               AND normalise_email(u.email) = normalise_email(t.email)
            RETURNING t.user_id
            """,
            token_hash,
        )
        if row is None:
            # Unknown, expired, already consumed, deleted account or a
            # changed address — all one answer, deliberately.
            raise VerificationError("this verification link is not valid")

        user_id = row["user_id"]
        await conn.execute(
            "UPDATE users SET email_verified_at = COALESCE(email_verified_at, NOW()) "
            "WHERE id = $1",
            user_id,
        )

    logger.info("an email address was verified")
    return user_id


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "RegistrationDisabled",
    "RegistrationError",
    "RegistrationOutcome",
    "VerificationError",
    "register_account",
    "resend_verification",
    "verify_email",
]
