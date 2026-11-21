"""Authentication provider abstraction.

The brief is explicit that not every provider belongs in one commit: *"an
interface and one working provider are sufficient."* So this defines the seam
and ships the provider that already exists (password, IRA Private's .env admin
account). Google, Apple and passkeys implement the same Protocol later without
touching the callers.

Two things are deliberately encoded here rather than left to implementations:

**Authentication is not authorisation.** A provider returns an
:class:`AuthenticatedSubject` — proof of who someone is. It does NOT return a
Principal, and it never decides a tenant. Tenant membership is resolved
separately from `tenant_memberships`, so a provider compromise cannot grant
access to a tenant the user was never a member of.

**Token signing is a separate concern with a known gap.** See
:class:`TokenSigningPolicy` — IRA Private's shared HS256 secret is acceptable for
one owner on one box and is NOT acceptable as the long-term SaaS design. That is
recorded in code, not only in a document, because the code is what gets read
when someone wires up the cloud edition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Protocol, runtime_checkable


class AuthMethod(str, Enum):
    PASSWORD = "password"
    GOOGLE = "google"
    APPLE = "apple"
    PASSKEY = "passkey"
    EMAIL_LINK = "email_link"


@dataclass(frozen=True, slots=True)
class AuthenticatedSubject:
    """Proof of identity. Carries no tenant and no privilege."""

    #: Stable identifier from the provider (never a display name).
    subject: str
    method: AuthMethod
    email: Optional[str] = None
    email_verified: bool = False
    display_name: str = ""
    #: Provider-specific extras. Must never contain tokens or secrets.
    claims: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("AuthenticatedSubject requires a stable subject identifier")


class AuthenticationFailed(Exception):
    """Authentication did not succeed.

    Carries no detail about WHY by design: distinguishing "no such user" from
    "wrong password" to the caller is how an enumeration oracle gets built.
    """


@runtime_checkable
class AuthProvider(Protocol):
    """One way of proving who someone is."""

    @property
    def method(self) -> AuthMethod:
        ...

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthenticatedSubject:
        """Return the subject, or raise AuthenticationFailed. Never return None
        for failure — an ignored None reads as success at the call site."""
        ...


class PasswordAuthProvider:
    """IRA Private's existing .env admin account, behind the interface.

    Delegates to `api.middleware.auth.authenticate_user` so there is exactly one
    password implementation — including its constant-time comparison and its
    lockout behaviour. A second copy would drift.
    """

    @property
    def method(self) -> AuthMethod:
        return AuthMethod.PASSWORD

    async def authenticate(self, credentials: Mapping[str, str]) -> AuthenticatedSubject:
        from api.middleware.auth import authenticate_user

        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not username or not password:
            raise AuthenticationFailed()
        if not authenticate_user(username, password):
            raise AuthenticationFailed()
        return AuthenticatedSubject(
            subject=username,
            method=AuthMethod.PASSWORD,
            display_name=username,
        )


class TokenSigningPolicy(str, Enum):
    """How session tokens are signed.

    ``SHARED_HS256`` is what IRA Private uses today: one symmetric secret
    (`IRA_SECRET_KEY`) signs and verifies. For a single owner on a single box
    that is a reasonable trade. For a multi-tenant service it is not:

      * every service that must VERIFY a token can also MINT one, so there is no
        way to give a component read-only trust;
      * rotation invalidates every session at once, which makes operators avoid
        rotating;
      * a single leaked value forges tokens for every tenant.

    The cloud edition must move to ``ASYMMETRIC_ROTATING`` (or managed OIDC)
    before it serves traffic. Recorded here so the constraint is visible at the
    point of use.
    """

    SHARED_HS256 = "shared_hs256"
    ASYMMETRIC_ROTATING = "asymmetric_rotating"
    MANAGED_OIDC = "managed_oidc"


#: What IRA Private uses. Not a recommendation for cloud.
PRIVATE_TOKEN_SIGNING = TokenSigningPolicy.SHARED_HS256

#: Policies acceptable for a multi-tenant deployment.
CLOUD_ACCEPTABLE_SIGNING = frozenset(
    {TokenSigningPolicy.ASYMMETRIC_ROTATING, TokenSigningPolicy.MANAGED_OIDC}
)


def assert_signing_policy_acceptable(edition: str, policy: TokenSigningPolicy) -> None:
    """Refuse a cloud deployment that still signs with one shared secret."""
    if edition == "cloud" and policy not in CLOUD_ACCEPTABLE_SIGNING:
        raise RuntimeError(
            f"Token signing policy {policy.value!r} is not acceptable for the cloud "
            "edition: one shared symmetric secret means every verifier can also mint "
            "tokens for every tenant. Use asymmetric signing with key rotation, or a "
            "managed OIDC provider."
        )


__all__ = [
    "AuthMethod",
    "AuthenticatedSubject",
    "AuthenticationFailed",
    "AuthProvider",
    "PasswordAuthProvider",
    "TokenSigningPolicy",
    "PRIVATE_TOKEN_SIGNING",
    "CLOUD_ACCEPTABLE_SIGNING",
    "assert_signing_policy_acceptable",
]
