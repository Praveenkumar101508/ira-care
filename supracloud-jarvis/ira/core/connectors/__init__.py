"""Connectors — Gmail, Calendar and Drive, behind one governed gateway.

Phase 7. The order below is the order a call travels, and the seam in
`core/interfaces/connector_gateway.py` specifies it:

    ConnectorCall
        ↓
    gateway.py      account + scope + rung, then the Phase 4 broker
        ↓
    taint.py        content read from outside cannot authorise an action
        ↓
    google.py       the connector holds the credential; the caller never does
        ↓
    secrets.py      the ONLY module that decrypts a token
        ↓
    store.py        tenant-scoped, RLS, receipts append-only
        ↓
    ActionReceipt   produced by the gateway, not by the agent that asked

── The three claims, and their limits ──────────────────────────────────────

**"Injected text in an email cannot make IRA act."** True, and it does not
depend on detecting the injection. Every side-effecting connector action has
`max_effect = REQUIRE_CONFIRMATION` (Phase 4), and `ticket.issue` refuses to
mint for a non-permissive effect — so no ticket can exist for `email.send`.
`tests/connectors/test_injection_cannot_act.py` locks that, separately from the
taint clamp, so a regression in one cannot hide behind the other.

**"The model never reaches a credential."** Enforced by an AST test over every
module (`tests/architecture/test_credential_boundary.py`), not by convention.
There is no function anywhere that returns a token.

**"IRA reads your mail; it does not send."** A property of the CREDENTIAL:
`oauth.OPERATION_SCOPES` never requests a send scope and `REFUSED_SCOPES`
refuses `gmail.send` with a reason. Drafts go to the Drafts folder, which
`gmail.compose` permits and which cannot send.

What is NOT claimed: no live Google endpoint was reachable from this
environment, so the token exchange and the API round trips are unverified
against the real service. Request shapes and response parsing are tested against
a fake transport. The encryption key comes from the process environment, not a
KMS — the database alone is not enough to read a token, but the database plus
the environment is. Both limits are stated in CONNECTOR_MODEL.md.
"""
from __future__ import annotations

from core.connectors.gateway import (
    OPERATION_ACCESS,
    OPERATION_POLICY,
    ConnectedAccount,
    ConnectorRefused,
    IRAConnectorGateway,
    build_gateway,
)
from core.connectors.google import (
    GmailConnector,
    GoogleCalendarConnector,
    GoogleDriveConnector,
    HttpxTransport,
    Transport,
    all_connectors,
)
from core.connectors.oauth import (
    CONNECTORS,
    OPERATION_SCOPES,
    REFUSED_SCOPES,
    InvalidState,
    OAuthError,
    OAuthNotConfigured,
    PendingAuthorisation,
    ScopeRefused,
    StoredState,
    begin,
    is_available,
    scopes_for,
    verify_callback,
)
from core.connectors.secrets import (
    CredentialTampered,
    MissingEncryptionKey,
    SealedSecret,
    seal,
    use_secret,
    use_secret_async,
)
from core.connectors.taint import (
    EMPTY_INFLUENCES,
    Influences,
    Provenance,
    TaintedText,
    clamp_for_taint,
    influences_of,
    tainted,
)

__all__ = [
    "CONNECTORS",
    "EMPTY_INFLUENCES",
    "OPERATION_ACCESS",
    "OPERATION_POLICY",
    "OPERATION_SCOPES",
    "REFUSED_SCOPES",
    "ConnectedAccount",
    "ConnectorRefused",
    "CredentialTampered",
    "GmailConnector",
    "GoogleCalendarConnector",
    "GoogleDriveConnector",
    "HttpxTransport",
    "IRAConnectorGateway",
    "Influences",
    "InvalidState",
    "MissingEncryptionKey",
    "OAuthError",
    "OAuthNotConfigured",
    "PendingAuthorisation",
    "Provenance",
    "ScopeRefused",
    "SealedSecret",
    "StoredState",
    "TaintedText",
    "Transport",
    "all_connectors",
    "begin",
    "build_gateway",
    "clamp_for_taint",
    "influences_of",
    "is_available",
    "scopes_for",
    "seal",
    "tainted",
    "use_secret",
    "use_secret_async",
    "verify_callback",
]
