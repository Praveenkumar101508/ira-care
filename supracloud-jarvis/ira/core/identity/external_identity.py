"""Federated sign-in and account linking (Phase 2 batch 10).

CLOUD-ADR-0008 settled the rule this module implements, and almost every line
here is that one rule applied to a different situation:

    An identity is `(issuer, subject)`. It is NEVER an email address.

── What this module does and does not do ───────────────────────────────────

It takes claims that have ALREADY been verified and decides who they map to.
It does not verify them: there is no discovery, no JWKS fetch, no signature
check, no `nonce` or PKCE handling here. Those belong to an OIDC client, and
conflating "who is this" with "is this real" is how a linking bug becomes
indistinguishable from a protocol bug.

`VerifiedClaims` is the boundary, and it is deliberately a distinct type rather
than a dict: constructing one is the caller asserting they have validated the
signature, the issuer, the audience and the expiry. Nothing in this module can
check that assertion, which is exactly why the type exists — a reviewer can find
every place one is built.

── Why email linking is refused, in the specific ────────────────────────────

The tempting behaviour is: a new provider identity arrives, its `email` matches
an existing account, so link them. That is the repeatedly-exploited pattern
CLOUD-ADR-0008 names, and it does not require a malicious provider:

  * an IdP that lets a user set an unverified `email` claim
  * an IdP that verifies an address the user no longer controls
  * a provider that reuses a `subject` after an account is deleted
  * a corporate IdP where an administrator can set any user's address

Any one of those turns "sign in with X" into "become whoever holds that
address". So an unrecognised identity NEVER attaches itself to an existing
account, whatever it claims. It either creates a new account or refuses, and a
person who genuinely owns both proves it by linking while signed in — which is
what `link_external_identity` is for.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from core.identity import auth_audit

logger = logging.getLogger("ira.identity.external")


class ExternalIdentityError(RuntimeError):
    """The operation was refused. Safe to show to the caller."""


class ExternalIdentityUnavailable(ExternalIdentityError):
    """The store could not be reached. Fail closed — never fail open."""


class AccountLinkRequired(ExternalIdentityError):
    """This identity is new and its address belongs to an existing account.

    Deliberately distinguishable from an ordinary refusal, because the two want
    opposite responses: this one should send the person to "sign in with your
    password, then link", and a refusal should not. It carries no detail about
    the existing account — the caller learns that linking is possible, which
    they could establish anyway by trying to register.
    """


@dataclass(frozen=True, slots=True)
class VerifiedClaims:
    """Claims from an ID token whose signature and issuer HAVE been verified.

    Constructing one is an assertion by the caller. Nothing downstream can
    re-check it, so every construction site is a place a reviewer must look.
    """

    issuer: str
    subject: str
    provider: str = ""
    email: str | None = None
    email_verified: bool = False


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    identity_id: uuid.UUID
    user_id: uuid.UUID
    issuer: str
    subject: str
    provider: str
    claimed_email: str | None
    claimed_email_verified: bool


def _clean(claims: VerifiedClaims) -> tuple[str, str]:
    issuer = (claims.issuer or "").strip()
    subject = (claims.subject or "").strip()
    if not issuer or not subject:
        # Both halves are required, and a blank one is not "no identity" — it is
        # an identity that would collide with every other blank. The database
        # refuses it too; this is the earlier, clearer refusal.
        raise ExternalIdentityError("this sign-in is not valid")
    return issuer, subject


async def _identity_connection(issuer: str, subject: str, user_id=None):
    """A connection scoped to ONE proved identity, and optionally one user.

    Local to this module for the reason `registration` and `password_reset`
    keep theirs local: `db_context`'s helpers all take a Principal, and putting
    another "no principal required" entry point on the shared surface makes the
    pattern look ordinary.
    """
    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.oidc_issuer', $1, true)", issuer)
            await conn.execute(
                "SELECT set_config('app.oidc_subject', $1, true)", subject)
            if user_id is not None:
                await conn.execute(
                    "SELECT set_config('app.user_id', $1, true)", str(user_id))
            yield conn


async def resolve_external_identity(claims: VerifiedClaims) -> uuid.UUID | None:
    """The user this identity already maps to, or None if it is unrecognised.

    Looks up `(issuer, subject)` and nothing else. There is no email fallback,
    and adding one would undo the batch.
    """
    from contextlib import asynccontextmanager

    issuer, subject = _clean(claims)
    scoped = asynccontextmanager(_identity_connection)

    try:
        async with scoped(issuer, subject) as conn:
            row = await conn.fetchrow(
                """
                SELECT e.user_id
                  FROM external_identities e
                  JOIN users u ON u.id = e.user_id
                 WHERE e.issuer = $1 AND e.subject = $2
                   AND u.deleted_at IS NULL
                   AND u.status = 'active'
                """,
                issuer, subject,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("external identity lookup failed: %s", type(exc).__name__)
        raise ExternalIdentityUnavailable(
            "the identity store could not be reached") from exc

    return row["user_id"] if row else None


async def sign_in_with_external_identity(
    claims: VerifiedClaims, *, allow_account_creation: bool = True,
) -> uuid.UUID:
    """Resolve verified claims to a user, creating an account if appropriate.

    Three outcomes, and the middle one is the batch:

      * the identity is KNOWN     → that user, always, whatever it now claims
      * the identity is NEW and its address is taken by an existing account
                                  → `AccountLinkRequired`. Never a silent link.
      * the identity is NEW and the address is free
                                  → a new account, if creation is permitted

    A suspended or deleted account does not sign in, and the identity is not
    re-pointed at a new one either — that would turn suspension into a way to
    get a fresh account with the same provider identity.
    """
    from contextlib import asynccontextmanager

    from utils.db import acquire

    issuer, subject = _clean(claims)

    existing = await resolve_external_identity(claims)
    if existing is not None:
        await _touch(issuer, subject, claims)
        logger.info("federated sign-in resolved a known identity")
        await auth_audit.record(auth_audit.PROVIDER_SIGN_IN, user_id=existing,
                                subject=claims.email, detail=issuer[:200])
        return existing

    # An unrecognised identity. Everything below is about NOT attaching it to
    # somebody else's account.
    address = (claims.email or "").strip()
    if address:
        try:
            async with acquire() as conn:
                # Only VERIFIED, live accounts count as a conflict. An
                # unverified row is not known to belong to anybody — `034`
                # permits several to share an address — so treating it as a
                # conflict would let a squatter block a real sign-up, and
                # treating it as a match would be the takeover.
                clash = await conn.fetchval(
                    """
                    SELECT id FROM users
                     WHERE normalised_email = normalise_email($1)
                       AND email_verified_at IS NOT NULL
                       AND deleted_at IS NULL
                    """,
                    address,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("external sign-in conflict check failed: %s",
                           type(exc).__name__)
            raise ExternalIdentityUnavailable(
                "the identity store could not be reached") from exc

        if clash is not None:
            # THE refusal. Not a link, however confident the provider is that it
            # verified this address — the provider is asserting something about
            # ITS user, not about ours.
            logger.info("federated sign-in needs an explicit link: the claimed "
                        "address already belongs to a verified account")
            # Attributed to the EXISTING account, because that is whose account
            # was aimed at. This is the takeover attempt CLOUD-ADR-0008 exists
            # to refuse, and the person who needs to see it in their history is
            # the one who already holds the address — a provider identity
            # claiming to be them is a thing they should be able to find out
            # about, and it is invisible to them anywhere else.
            await auth_audit.record(
                auth_audit.ACCOUNT_LINK_REQUIRED, user_id=clash,
                subject=address, detail=f"unlinked identity at {issuer}"[:200])
            raise AccountLinkRequired(
                "That address already has an account. Sign in with it and link "
                "this provider from your account settings.")

    if not allow_account_creation:
        raise ExternalIdentityError("this sign-in is not valid")

    user_id = uuid.uuid4()
    scoped = asynccontextmanager(_identity_connection)
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (id, email, display_name, auth_provider,
                                   external_subject, status)
                VALUES ($1, $2, $3, $4, $5, 'active')
                """,
                user_id, address or None,
                (address.split("@", 1)[0] if address else "New user"),
                # The ISSUER, not the friendly provider name. `users` carries a
                # UNIQUE (auth_provider, external_subject), and two deployments
                # of the same product are both "okta" while being different
                # issuers — so the friendly name would collide two unrelated
                # people's subjects. This keeps that index consistent with
                # `external_identities_issuer_subject_key`, which is the
                # authority.
                issuer[:100], subject,
            )
            # The address is marked verified only when the PROVIDER verified it
            # AND nothing else holds it — the conflict check above already
            # established the second half. Marking an unverified claim as
            # verified would let a provider hand out addresses it never checked,
            # and `users_verified_email_key` would then bind that address to
            # this account for good.
            if address and claims.email_verified:
                await conn.execute(
                    "UPDATE users SET email_verified_at = NOW() WHERE id = $1",
                    user_id)

        async with scoped(issuer, subject, user_id) as conn:
            await conn.execute(
                """
                INSERT INTO external_identities
                       (user_id, issuer, subject, provider, claimed_email,
                        claimed_email_verified, last_authenticated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                """,
                user_id, issuer, subject, claims.provider or "",
                address or None, bool(claims.email_verified),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("federated account creation failed: %s", type(exc).__name__)
        raise ExternalIdentityUnavailable(
            "the identity store could not be reached") from exc

    logger.info("federated sign-in created a new account")
    await auth_audit.record(auth_audit.PROVIDER_SIGN_IN, user_id=user_id,
                            subject=address or None,
                            detail=f"new account at {issuer}"[:200])
    return user_id


async def link_external_identity(user_id, claims: VerifiedClaims) -> ExternalIdentity:
    """Attach a provider identity to the SIGNED-IN account.

    This is the explicit process CLOUD-ADR-0008 requires in place of silent
    merging, and it is safe for a reason worth stating: BOTH sides are proved.
    The caller proved the account by holding a session, and proved the identity
    at the provider. Neither alone is enough, and `059`'s INSERT policy requires
    both GUCs so that stays true even if this function is wrong.

    Refuses an identity already linked elsewhere. Stealing it would silently
    remove somebody else's way in.

    Takes a `user_id` rather than a Principal, and that is not a shortcut.
    `Principal` refuses to exist without a tenant — correctly, since a blank
    tenant is the precondition for a cross-tenant read — but linking a provider
    is an ACCOUNT operation, and a federated-only account may hold no membership
    at all. Requiring a Principal here would make these functions unusable for
    exactly the accounts they exist to serve. Nothing tenant-scoped happens
    below, so nothing needs a tenant bound.
    """
    from contextlib import asynccontextmanager

    issuer, subject = _clean(claims)

    owner = await resolve_external_identity(claims)
    if owner is not None and owner != user_id:
        raise ExternalIdentityError(
            "That provider account is already linked to a different account.")
    if owner == user_id:
        raise ExternalIdentityError(
            "That provider account is already linked to this account.")

    # One identity per issuer per user. `059` has a unique index enforcing it,
    # and relying on that alone was a gap the tests found: the refusal arrived
    # as a database error rather than a decision, so the caller got "the store
    # could not be reached" for something that is simply not allowed — and a
    # reviewer reading this function would not know the rule existed.
    #
    # Two Google accounts against one IRA account makes "which one signed in"
    # unanswerable and leaves a second link nobody expected after unlinking the
    # first.
    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(user_id) as conn:
        already = await conn.fetchval(
            "SELECT 1 FROM external_identities WHERE user_id = $1 AND issuer = $2",
            user_id, issuer)
    if already:
        raise ExternalIdentityError(
            "This account is already linked to that provider. Unlink the "
            "existing one first.")

    scoped = asynccontextmanager(_identity_connection)
    try:
        async with scoped(issuer, subject, user_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO external_identities
                       (user_id, issuer, subject, provider, claimed_email,
                        claimed_email_verified, last_authenticated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                RETURNING id, user_id, issuer, subject, provider,
                          claimed_email, claimed_email_verified
                """,
                user_id, issuer, subject, claims.provider or "",
                (claims.email or "").strip() or None,
                bool(claims.email_verified),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("linking an external identity failed: %s",
                       type(exc).__name__)
        raise ExternalIdentityUnavailable(
            "the identity store could not be reached") from exc

    logger.info("linked an external identity to a signed-in account")
    # A new way into the account. That is the same class of change as a password
    # reset, and it is the one an account takeover makes permanent — so it has
    # to be visible in the owner's own history rather than only in a settings
    # page they would have to think to check.
    await auth_audit.record(auth_audit.PROVIDER_LINKED, user_id=user_id,
                            subject=claims.email, detail=issuer[:200])
    return _identity(row)


async def list_external_identities(user_id) -> list[ExternalIdentity]:
    """The signed-in account's own linked providers.

    `user_id`, not a Principal — see `link_external_identity`.
    """
    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, issuer, subject, provider, claimed_email,
                   claimed_email_verified
              FROM external_identities
             WHERE user_id = $1
             ORDER BY created_at
            """,
            user_id,
        )
    return [_identity(r) for r in rows]


async def unlink_external_identity(user_id, issuer: str, subject: str) -> bool:
    """Remove a link, unless it is the account's last way in.

    An account with no password and one provider is an account whose only door
    this is. Removing it is a self-inflicted permanent lockout that no
    self-service flow can repair — password reset needs a verified address AND a
    credential row to replace, and such an account has neither. The same
    reasoning `059`'s rollback guard uses, at the level of one account.
    """
    from core.identity.db_context import identity_bootstrap_connection

    issuer = (issuer or "").strip()
    subject = (subject or "").strip()
    if not issuer or not subject:
        return False

    async with identity_bootstrap_connection(user_id) as conn:
        has_password = await conn.fetchval(
            "SELECT 1 FROM user_credentials WHERE user_id = $1", user_id)
        links = await conn.fetchval(
            "SELECT count(*) FROM external_identities WHERE user_id = $1",
            user_id)

        if not has_password and links <= 1:
            raise ExternalIdentityError(
                "This is the only way to sign in to this account. Set a "
                "password first, or link another provider.")

        removed = await conn.fetchval(
            """
            DELETE FROM external_identities
             WHERE user_id = $1 AND issuer = $2 AND subject = $3
            RETURNING id
            """,
            user_id, issuer, subject,
        )

    if removed is not None:
        logger.info("unlinked an external identity")
        await auth_audit.record(auth_audit.PROVIDER_UNLINKED, user_id=user_id,
                                detail=issuer[:200])
    return removed is not None


async def _touch(issuer: str, subject: str, claims: VerifiedClaims) -> None:
    """Record the sign-in and refresh the recorded claims. Best effort.

    The identity columns are immutable — `059` has a trigger — so this can only
    move `last_authenticated_at` and the display claims. A failure must not
    refuse a sign-in that is otherwise valid.
    """
    from contextlib import asynccontextmanager

    scoped = asynccontextmanager(_identity_connection)
    try:
        async with scoped(issuer, subject) as conn:
            await conn.execute(
                """
                UPDATE external_identities
                   SET last_authenticated_at = NOW(),
                       claimed_email = $3,
                       claimed_email_verified = $4,
                       provider = COALESCE(NULLIF($5, ''), provider)
                 WHERE issuer = $1 AND subject = $2
                """,
                issuer, subject, (claims.email or "").strip() or None,
                bool(claims.email_verified), claims.provider or "",
            )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("could not record a federated sign-in", exc_info=True)


def _identity(row) -> ExternalIdentity:
    return ExternalIdentity(
        identity_id=row["id"],
        user_id=row["user_id"],
        issuer=row["issuer"],
        subject=row["subject"],
        provider=row["provider"] or "",
        claimed_email=row["claimed_email"],
        claimed_email_verified=bool(row["claimed_email_verified"]),
    )


__all__ = [
    "AccountLinkRequired",
    "ExternalIdentity",
    "ExternalIdentityError",
    "ExternalIdentityUnavailable",
    "VerifiedClaims",
    "link_external_identity",
    "list_external_identities",
    "resolve_external_identity",
    "sign_in_with_external_identity",
    "unlink_external_identity",
]
