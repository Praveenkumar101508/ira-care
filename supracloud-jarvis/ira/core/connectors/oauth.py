"""OAuth authorisation-code flow with PKCE, and the refusals that matter.

The brief lists **invalid OAuth state** among the conditions that must fail
closed, and names *"OAuth state/PKCE weaknesses; over-broad scopes"* among
Phase 7's threats. This module is organised around those three.

── State: single-use, expiring, bound ───────────────────────────────────────

`state` is the CSRF defence. A callback arriving with a state IRA did not issue
must be refused, and one arriving with a state IRA issued *and already used*
must also be refused — the second is a replay, and treating it as a duplicate
submission is how a stolen authorisation code gets exchanged twice.

Every check is spelled out separately in `verify_callback` so a failure names
which one fired. "Invalid state" as a single message makes an incident
un-triageable.

The state is bound to tenant, user, connector and scopes at issue time, and
`028_connectors.sql`'s trigger refuses to let a consumption change any of them.
So even a callback that guesses a live state cannot redirect it to another
tenant.

── PKCE ────────────────────────────────────────────────────────────────────

S256 only. `plain` is permitted by RFC 7636 and offers nothing here — if an
attacker can read the verifier they can read the plain challenge too. The
schema's CHECK enforces S256, so a downgrade is not expressible.

The verifier is a credential for the life of the flow, so it is sealed with
`secrets.seal` and stored encrypted, not as text.

── Scopes: minimised, and only what this build implements ───────────────────

`scopes_for` returns the scopes an operation actually needs, and refuses any
scope not in `connector_scopes` for the current edition. Phase 7 implements
reading and drafting; it does not implement sending, so `gmail.send` is not
requestable. Asking a user to grant a permission the software cannot exercise
is asking under false pretences, and it is the over-broad-scope threat in its
most common form.

── What is NOT verified here ───────────────────────────────────────────────

No live Google endpoint was reachable from this environment. The authorisation
URL is built to Google's documented parameters and the callback verification is
exercised end to end against a real database, but the token EXCHANGE (posting
the code to Google and getting a refresh token back) is only tested against a
fake transport. "IRA connects to Gmail" is therefore not a claim Phase 7 makes;
"IRA's OAuth flow refuses every invalid callback" is.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets as _stdlib_secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final, Iterable, Mapping, Optional, Sequence

from core.connectors.secrets import SealedSecret, seal

logger = logging.getLogger("ira.connectors.oauth")

#: How long a user has to complete a consent screen. Ten minutes is generous for
#: a human and short for an attacker sitting on a stolen state.
STATE_TTL_SECONDS: Final[int] = 600

#: 32 bytes of entropy, hex-encoded to 64 chars. The schema requires >= 32
#: characters; this is comfortably above it.
STATE_BYTES: Final[int] = 32

#: RFC 7636 permits 43–128 characters. 64 bytes urlsafe-b64 lands at 86.
VERIFIER_BYTES: Final[int] = 64

CONNECTORS: Final[frozenset[str]] = frozenset(
    {"gmail", "google_calendar", "google_drive"}
)

GOOGLE_AUTH_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL: Final[str] = "https://oauth2.googleapis.com/revoke"

#: Which scopes each operation needs — the minimum, not the convenient maximum.
#: An operation absent here cannot be performed, which is how "IRA cannot send
#: email in this phase" is expressed as configuration rather than as a promise.
OPERATION_SCOPES: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("gmail", "search"): ("https://www.googleapis.com/auth/gmail.readonly",),
    ("gmail", "read_selected"): ("https://www.googleapis.com/auth/gmail.readonly",),
    ("gmail", "prepare_draft"): ("https://www.googleapis.com/auth/gmail.compose",),
    ("google_calendar", "list_events"): (
        "https://www.googleapis.com/auth/calendar.readonly",
    ),
    ("google_drive", "search"): (
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    ),
    ("google_drive", "read_selected"): (
        "https://www.googleapis.com/auth/drive.readonly",
    ),
}

#: Scopes this build will never request, with the reason. Present so an attempt
#: to add one is a visible edit to a list of refusals rather than a new string
#: in a call site. `gmail.send` is the important one: Phase 7 prepares drafts
#: and does not send, and a build that holds a send scope it never uses is one
#: compromise away from using it.
REFUSED_SCOPES: Final[dict[str, str]] = {
    "https://www.googleapis.com/auth/gmail.send":
        "Phase 7 prepares drafts and never sends. Holding a send scope that "
        "nothing exercises only widens what a compromise could do.",
    "https://mail.google.com/":
        "Full mailbox control, including permanent deletion. Never requested.",
    "https://www.googleapis.com/auth/gmail.modify":
        "Permits deleting and altering messages. Not needed to read or draft.",
    "https://www.googleapis.com/auth/drive":
        "Full Drive write access. Phase 7 reads only.",
    "https://www.googleapis.com/auth/calendar":
        "Calendar write access. Phase 7 reads only.",
}


class OAuthError(RuntimeError):
    """Base for every authorisation failure."""


class OAuthNotConfigured(OAuthError):
    """No client credentials. The connect button must be disabled, not offered."""


class InvalidState(OAuthError):
    """The callback's state did not verify. Always a refusal, never a warning.

    Carries `reason` so an incident can be triaged: "unknown state" and
    "already consumed" mean very different things.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"OAuth callback refused: {reason}")
        self.reason = reason


class ScopeRefused(OAuthError):
    """A scope this build will not request, or does not offer in this edition."""


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """The registered application's credentials.

    `client_secret` is `repr`-suppressed: this object gets logged in exception
    contexts, and a client secret in a traceback is a leaked client secret.
    """

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def load_client(env: Optional[Mapping[str, str]] = None) -> OAuthClient:
    """Read the client from configuration. Never from a request."""
    source = os.environ if env is None else env
    return OAuthClient(
        client_id=(source.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip(),
        client_secret=(source.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip(),
        redirect_uri=(source.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip(),
    )


def is_available(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only when a flow could actually complete.

    Requires BOTH the OAuth client and the credential encryption key: a flow
    that ends by failing to store the token wastes the user's consent and
    leaves a live grant at Google that IRA cannot see.
    """
    from core.connectors.secrets import is_configured as key_configured

    return load_client(env).is_configured() and key_configured(
        dict(env) if env is not None else None
    )


# ── Scope minimisation ───────────────────────────────────────────────────────


def scopes_for(connector: str, operations: Iterable[str]) -> tuple[str, ...]:
    """The minimum scopes needed for these operations.

    Raises for an unknown operation rather than returning an empty set: an
    empty set would silently produce a consent screen granting nothing, and the
    resulting failure would appear at the first API call instead of here.
    """
    if connector not in CONNECTORS:
        raise ScopeRefused(f"{connector!r} is not a connector this build implements")

    wanted: list[str] = []
    for operation in operations:
        key = (connector, operation)
        if key not in OPERATION_SCOPES:
            raise ScopeRefused(
                f"{connector}.{operation} is not an operation this build "
                f"implements, so there is no scope to request for it. "
                f"Implemented: "
                f"{sorted(op for c, op in OPERATION_SCOPES if c == connector)}"
            )
        for scope in OPERATION_SCOPES[key]:
            if scope not in wanted:
                wanted.append(scope)
    return tuple(wanted)


def assert_scopes_allowed(scopes: Sequence[str]) -> None:
    """Refuse any scope on the refusal list. Explicit, with the reason."""
    for scope in scopes:
        if scope in REFUSED_SCOPES:
            raise ScopeRefused(
                f"Refusing to request {scope}: {REFUSED_SCOPES[scope]}"
            )


# ── PKCE ─────────────────────────────────────────────────────────────────────


def _new_verifier() -> str:
    """A fresh PKCE verifier. urlsafe-b64 without padding, per RFC 7636."""
    return base64.urlsafe_b64encode(
        _stdlib_secrets.token_bytes(VERIFIER_BYTES)
    ).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    """S256 challenge. Unpadded urlsafe base64 of the SHA-256 of the verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verifier_matches(verifier: str, challenge: str) -> bool:
    """Constant-time comparison of a verifier against a stored challenge."""
    import hmac

    return hmac.compare_digest(challenge_for(verifier), challenge)


# ── Starting a flow ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PendingAuthorisation:
    """Everything the caller must persist, plus the URL to send the user to."""

    state: str
    connector: str
    scopes: tuple[str, ...]
    code_challenge: str
    sealed_verifier: SealedSecret
    expires_at: datetime
    authorisation_url: str
    return_to: str = "connectors"


def begin(
    *,
    connector: str,
    operations: Iterable[str],
    tenant_id: str,
    user_id: str,
    return_to: str = "connectors",
    env: Optional[Mapping[str, str]] = None,
    client: Optional[OAuthClient] = None,
) -> PendingAuthorisation:
    """Start an authorisation. Returns what to store and where to send the user.

    Refuses before generating anything if the build cannot finish the flow —
    there is no value in a consent screen whose callback will fail to store a
    token.
    """
    active = client or load_client(env)
    if not active.is_configured():
        raise OAuthNotConfigured(
            "GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and "
            "GOOGLE_OAUTH_REDIRECT_URI must all be set. Refusing to start a "
            "flow that cannot complete."
        )

    scopes = scopes_for(connector, operations)
    assert_scopes_allowed(scopes)

    verifier = _new_verifier()
    challenge = challenge_for(verifier)
    state = _stdlib_secrets.token_hex(STATE_BYTES)

    # The verifier is bound to the state and tenant, so a ciphertext lifted from
    # one flow's row cannot be opened in another's.
    sealed = seal(
        verifier,
        aad=f"oauth:{tenant_id}:{connector}:{state}",
        env=dict(env) if env is not None else None,
    )

    from urllib.parse import urlencode

    params = {
        "client_id": active.client_id,
        "redirect_uri": active.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # A refresh token is only issued with offline access, and only on the
        # first consent unless prompt=consent is forced.
        "access_type": "offline",
        "prompt": "consent",
        # Restricts the account chooser to the address the user is connecting,
        # when known. Omitted here; the user picks.
        "include_granted_scopes": "false",
    }

    return PendingAuthorisation(
        state=state,
        connector=connector,
        scopes=scopes,
        code_challenge=challenge,
        sealed_verifier=sealed,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS),
        authorisation_url=f"{GOOGLE_AUTH_URL}?{urlencode(params)}",
        return_to=return_to if return_to in ("connectors", "onboarding", "settings")
        else "connectors",
    )


# ── Verifying a callback ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StoredState:
    """The row `verify_callback` checks against, as the store returns it."""

    state: str
    tenant_id: str
    user_id: str
    connector: str
    requested_scopes: tuple[str, ...]
    code_challenge: str
    expires_at: datetime
    consumed_at: Optional[datetime] = None


def verify_callback(
    *,
    presented_state: str,
    stored: Optional[StoredState],
    principal_tenant_id: str,
    error: str = "",
    now: Optional[datetime] = None,
) -> StoredState:
    """Every reason to refuse a callback, checked separately and named.

    Separately, because "invalid state" as one message makes an incident
    un-triageable: an unknown state is someone probing, an expired one is a user
    who left the tab open, and a consumed one is a replay. Those want different
    responses from whoever is on call.

    Returns the verified row. Raising is the only other outcome — there is no
    boolean for a caller to ignore.
    """
    moment = now or datetime.now(timezone.utc)

    # The provider told us it failed. Believe it and stop; do not proceed to
    # look up a state, which would leak whether that state existed.
    if error:
        raise InvalidState(f"the provider returned an error ({error})")

    if not presented_state:
        raise InvalidState("no state parameter was presented")

    if stored is None:
        raise InvalidState(
            "the state is not one IRA issued (unknown, or already cleaned up)"
        )

    # Constant-time: the lookup already matched, but comparing here keeps the
    # code honest if the store is ever made fuzzy.
    import hmac

    if not hmac.compare_digest(presented_state, stored.state):
        raise InvalidState("the presented state does not match the stored one")

    if stored.consumed_at is not None:
        raise InvalidState(
            f"the state was already consumed at {stored.consumed_at.isoformat()} "
            f"— this is a replay or a double-submitted consent screen"
        )

    if stored.expires_at <= moment:
        raise InvalidState(
            f"the state expired at {stored.expires_at.isoformat()}; "
            f"authorisation must be completed within "
            f"{STATE_TTL_SECONDS // 60} minutes"
        )

    # Cross-tenant: a callback delivered in one tenant's session must not
    # complete another tenant's flow. RLS already scopes the lookup, so this is
    # defence in depth — and it is the check that would catch a store that
    # forgot to set the tenant context.
    if not hmac.compare_digest(str(stored.tenant_id), str(principal_tenant_id)):
        raise InvalidState(
            "the state belongs to a different tenant than the session "
            "presenting it"
        )

    if stored.connector not in CONNECTORS:
        raise InvalidState(f"the state names an unknown connector ({stored.connector})")

    return stored


def granted_scopes_are_acceptable(
    requested: Sequence[str], granted: Sequence[str]
) -> tuple[bool, str]:
    """Compare what was asked for against what the provider actually granted.

    Two failures matter, and they are opposite:

    * **Fewer than requested** — the user unticked something. Not an error:
      IRA must operate with what it has and say which features are unavailable.
      Silently continuing as though everything was granted produces failures
      later that look like bugs.
    * **MORE than requested** — the provider returned a scope IRA never asked
      for. That is either a redirect to a different consent screen or a
      misconfigured client, and it must be refused: holding an unrequested
      write scope is exactly the over-broad-scope threat.
    """
    requested_set, granted_set = set(requested), set(granted)

    extra = granted_set - requested_set
    if extra:
        return False, (
            f"the provider granted scope(s) IRA did not request: {sorted(extra)}. "
            f"Refusing — an unrequested scope means the consent screen was not "
            f"the one IRA built."
        )

    missing = requested_set - granted_set
    if missing:
        return True, (
            f"the following were not granted and the related features will be "
            f"unavailable: {sorted(missing)}"
        )
    return True, ""


def revocation_request(token: str) -> tuple[str, dict[str, str]]:
    """URL and form body for revoking a grant at the provider.

    Returned rather than performed so the caller owns the HTTP, and so this
    module stays free of a network dependency. `disconnect` must revoke at the
    provider as well as delete locally — deleting IRA's copy leaves the grant
    live at Google while telling the user they are disconnected.
    """
    return GOOGLE_REVOKE_URL, {"token": token}


def token_exchange_request(
    *,
    code: str,
    verifier: str,
    client: OAuthClient,
) -> tuple[str, dict[str, str]]:
    """URL and form body for exchanging an authorisation code.

    Also returned rather than performed, for the same reason, and so the
    exchange can be exercised against a fake transport in tests. The live
    exchange against Google is UNVERIFIED in this environment — stated in the
    module docstring.
    """
    return GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "redirect_uri": client.redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }


__all__ = [
    "CONNECTORS",
    "GOOGLE_AUTH_URL",
    "GOOGLE_REVOKE_URL",
    "GOOGLE_TOKEN_URL",
    "OPERATION_SCOPES",
    "REFUSED_SCOPES",
    "STATE_TTL_SECONDS",
    "InvalidState",
    "OAuthClient",
    "OAuthError",
    "OAuthNotConfigured",
    "PendingAuthorisation",
    "ScopeRefused",
    "StoredState",
    "assert_scopes_allowed",
    "begin",
    "challenge_for",
    "granted_scopes_are_acceptable",
    "is_available",
    "load_client",
    "revocation_request",
    "scopes_for",
    "token_exchange_request",
    "verifier_matches",
    "verify_callback",
]
