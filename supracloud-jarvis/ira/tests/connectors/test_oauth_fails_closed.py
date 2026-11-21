"""Phase 7 — every invalid OAuth callback is refused, and says which check fired.

The brief lists **invalid OAuth state** among the conditions that must fail
closed, and names *"OAuth state/PKCE weaknesses; over-broad scopes"* among the
threats. This file walks each of them.

`verify_callback` raises rather than returning a boolean, on purpose: a boolean
can be ignored by a caller who forgot to check it, and the caller here is a
public HTTP handler.

Also asserted: the reasons are DISTINGUISHABLE. An unknown state is someone
probing, an expired one is a user who left a tab open, and a consumed one is a
replay. Collapsing those into "invalid state" makes an incident un-triageable
at 3am.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from core.connectors.oauth import (
    CONNECTORS,
    GOOGLE_AUTH_URL,
    OPERATION_SCOPES,
    REFUSED_SCOPES,
    STATE_TTL_SECONDS,
    InvalidState,
    OAuthClient,
    OAuthNotConfigured,
    ScopeRefused,
    StoredState,
    assert_scopes_allowed,
    begin,
    challenge_for,
    granted_scopes_are_acceptable,
    is_available,
    revocation_request,
    scopes_for,
    token_exchange_request,
    verifier_matches,
    verify_callback,
)

TENANT = "f0f0f0f0-0000-4000-8000-00000000000a"
OTHER_TENANT = "f1f1f1f1-0000-4000-8000-00000000000b"
USER = "f0f0f0f0-0000-4000-8000-0000000000aa"

KEY = base64.b64encode(b"k" * 32).decode()
ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-id.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret-not-real",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://ira.example/api/v1/connectors/callback",
    "IRA_CONNECTOR_ENCRYPTION_KEY": KEY,
}

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def stored(**overrides) -> StoredState:
    base = dict(
        state="s" * 64,
        tenant_id=TENANT,
        user_id=USER,
        connector="gmail",
        requested_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        code_challenge="challenge",
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=None,
    )
    base.update(overrides)
    return StoredState(**base)


def verify(**overrides):
    kwargs = dict(
        presented_state="s" * 64,
        stored=stored(),
        principal_tenant_id=TENANT,
        now=NOW,
    )
    kwargs.update(overrides)
    return verify_callback(**kwargs)


# ── The happy path exists, so the refusals below mean something ──────────────


def test_a_valid_callback_verifies():
    """Without this, every refusal test could pass by refusing everything."""
    assert verify().connector == "gmail"


# ── Every refusal, named ─────────────────────────────────────────────────────


def test_an_unknown_state_is_refused():
    with pytest.raises(InvalidState) as exc:
        verify(stored=None)
    assert "not one IRA issued" in exc.value.reason


def test_a_missing_state_is_refused():
    with pytest.raises(InvalidState) as exc:
        verify(presented_state="")
    assert "no state parameter" in exc.value.reason


def test_a_replayed_state_is_refused():
    """The important one. Treating a re-presented state as a duplicate
    submission is how a stolen authorisation code gets exchanged twice."""
    with pytest.raises(InvalidState) as exc:
        verify(stored=stored(consumed_at=NOW - timedelta(minutes=1)))
    assert "replay" in exc.value.reason
    assert "already consumed" in exc.value.reason


def test_an_expired_state_is_refused():
    with pytest.raises(InvalidState) as exc:
        verify(stored=stored(expires_at=NOW - timedelta(seconds=1)))
    assert "expired" in exc.value.reason


def test_a_state_expiring_exactly_now_is_refused():
    """Boundary. `<=` rather than `<`, so a state is never usable at the instant
    it expires."""
    with pytest.raises(InvalidState):
        verify(stored=stored(expires_at=NOW))


def test_a_mismatched_state_is_refused():
    with pytest.raises(InvalidState) as exc:
        verify(presented_state="t" * 64)
    assert "does not match" in exc.value.reason


def test_a_cross_tenant_callback_is_refused():
    """A callback delivered in one tenant's session must not complete another's
    flow. RLS scopes the lookup, so this is the check that catches a store which
    forgot to set the tenant context."""
    with pytest.raises(InvalidState) as exc:
        verify(principal_tenant_id=OTHER_TENANT)
    assert "different tenant" in exc.value.reason


def test_a_provider_error_short_circuits_before_the_state_is_looked_up():
    """Believe the provider and stop. Proceeding to check a state would leak
    whether that state existed, through timing or through the error text."""
    with pytest.raises(InvalidState) as exc:
        verify(error="access_denied", stored=None)
    assert "provider returned an error" in exc.value.reason
    assert "access_denied" in exc.value.reason


def test_an_unknown_connector_in_a_stored_state_is_refused():
    with pytest.raises(InvalidState) as exc:
        verify(stored=stored(connector="dropbox"))
    assert "unknown connector" in exc.value.reason


def test_every_refusal_reason_is_distinguishable():
    """An incident needs to know WHICH check fired."""
    reasons = set()
    for kwargs in (
        {"stored": None},
        {"presented_state": ""},
        {"stored": stored(consumed_at=NOW)},
        {"stored": stored(expires_at=NOW - timedelta(seconds=1))},
        {"presented_state": "t" * 64},
        {"principal_tenant_id": OTHER_TENANT},
        {"error": "access_denied"},
        {"stored": stored(connector="dropbox")},
    ):
        with pytest.raises(InvalidState) as exc:
            verify(**kwargs)
        reasons.add(exc.value.reason)
    assert len(reasons) == 8, f"reasons collapsed into {len(reasons)} distinct messages"


def test_verification_raises_rather_than_returning_a_boolean():
    """A boolean can be ignored by a caller who forgot to check it, and the
    caller here is a public HTTP handler."""
    with pytest.raises(InvalidState):
        verify(stored=None)


# ── PKCE ─────────────────────────────────────────────────────────────────────


def test_the_challenge_is_s256_of_the_verifier():
    verifier = "abc123-a-test-verifier-long-enough-for-rfc-7636-purposes"
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge_for(verifier) == expected
    assert "=" not in challenge_for(verifier), "padding must be stripped per RFC 7636"


def test_a_verifier_matches_only_its_own_challenge():
    assert verifier_matches("verifier-one", challenge_for("verifier-one"))
    assert not verifier_matches("verifier-two", challenge_for("verifier-one"))


def test_the_flow_requests_s256_and_never_plain():
    """`plain` is permitted by RFC 7636 and offers nothing: an attacker who can
    read the verifier can read a plain challenge too. The schema's CHECK also
    refuses it, so a downgrade is not expressible."""
    pending = begin(
        connector="gmail", operations=["search"],
        tenant_id=TENANT, user_id=USER, env=ENV,
    )
    assert "code_challenge_method=S256" in pending.authorisation_url
    assert "plain" not in pending.authorisation_url


def test_each_flow_gets_a_fresh_verifier_and_state():
    """A reused state is a reused CSRF token; a reused verifier defeats PKCE."""
    a = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
              user_id=USER, env=ENV)
    b = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
              user_id=USER, env=ENV)
    assert a.state != b.state
    assert a.code_challenge != b.code_challenge
    assert a.sealed_verifier.ciphertext != b.sealed_verifier.ciphertext


def test_the_state_is_long_enough_for_the_schema():
    pending = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
                    user_id=USER, env=ENV)
    assert len(pending.state) >= 32, "the schema CHECK requires >= 32 characters"


def test_the_verifier_is_stored_encrypted_not_as_text():
    """A PKCE verifier is a credential for the life of the flow."""
    pending = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
                    user_id=USER, env=ENV)
    assert pending.sealed_verifier.key_id
    assert pending.sealed_verifier.nonce
    # The challenge is the hash and may be stored plainly; the verifier may not.
    assert pending.code_challenge.encode() not in pending.sealed_verifier.ciphertext


def test_the_state_ttl_is_short_enough_to_matter():
    assert STATE_TTL_SECONDS <= 900, (
        "a consent window longer than 15 minutes is an attacker sitting on a "
        "stolen state for longer than necessary"
    )


# ── Scope minimisation ───────────────────────────────────────────────────────


def test_reading_email_asks_only_for_read_access():
    scopes = scopes_for("gmail", ["search", "read_selected"])
    assert scopes == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert not any("send" in s or "modify" in s for s in scopes)


def test_drafting_does_not_request_send():
    """The distinction Phase 7 turns on: IRA prepares a draft, the user sends it.

    `gmail.compose` writes to the Drafts folder and cannot send.
    """
    scopes = scopes_for("gmail", ["prepare_draft"])
    assert scopes == ("https://www.googleapis.com/auth/gmail.compose",)
    assert "https://www.googleapis.com/auth/gmail.send" not in scopes


@pytest.mark.parametrize("scope", sorted(REFUSED_SCOPES))
def test_a_refused_scope_is_refused_with_its_reason(scope):
    """A build that holds a send scope it never exercises is one compromise away
    from using it."""
    with pytest.raises(ScopeRefused) as exc:
        assert_scopes_allowed([scope])
    assert REFUSED_SCOPES[scope][:30] in str(exc.value)


def test_no_operation_maps_to_a_refused_scope():
    """Guards the pair: an operation whose scope is on the refusal list would
    make `begin` raise for a feature the build claims to have."""
    for (connector, operation), scopes in OPERATION_SCOPES.items():
        for scope in scopes:
            assert scope not in REFUSED_SCOPES, (
                f"{connector}.{operation} needs {scope}, which is refused"
            )


def test_gmail_send_is_not_reachable_through_any_operation():
    """The scope Phase 7 must not hold, checked at the operation level rather
    than only in the refusal list."""
    all_scopes = {s for scopes in OPERATION_SCOPES.values() for s in scopes}
    assert "https://www.googleapis.com/auth/gmail.send" not in all_scopes
    assert "https://mail.google.com/" not in all_scopes


def test_an_unimplemented_operation_raises_rather_than_asking_for_nothing():
    """An empty scope set would produce a consent screen granting nothing, and
    the failure would surface at the first API call instead of here."""
    with pytest.raises(ScopeRefused) as exc:
        scopes_for("gmail", ["send"])
    assert "not an operation this build implements" in str(exc.value)


def test_an_unknown_connector_is_refused():
    with pytest.raises(ScopeRefused):
        scopes_for("dropbox", ["search"])


def test_every_operation_belongs_to_a_known_connector():
    for connector, _operation in OPERATION_SCOPES:
        assert connector in CONNECTORS


# ── What the provider actually granted ───────────────────────────────────────


def test_extra_scopes_from_the_provider_are_refused():
    """MORE than requested means the consent screen was not the one IRA built —
    either a redirect elsewhere or a misconfigured client. Holding an
    unrequested write scope is the over-broad-scope threat itself."""
    ok, message = granted_scopes_are_acceptable(
        ["scope.read"], ["scope.read", "https://mail.google.com/"]
    )
    assert ok is False
    assert "did not request" in message


def test_fewer_scopes_are_accepted_but_reported():
    """The user unticked something. IRA must operate with what it has and say
    which features are unavailable — continuing as though everything was granted
    produces failures later that look like bugs."""
    ok, message = granted_scopes_are_acceptable(
        ["scope.read", "scope.draft"], ["scope.read"]
    )
    assert ok is True
    assert "unavailable" in message
    assert "scope.draft" in message


def test_an_exact_match_reports_nothing():
    ok, message = granted_scopes_are_acceptable(["a", "b"], ["b", "a"])
    assert ok is True and message == ""


# ── Refusing to start a flow that cannot finish ──────────────────────────────


def test_no_client_credentials_means_no_flow():
    with pytest.raises(OAuthNotConfigured):
        begin(connector="gmail", operations=["search"], tenant_id=TENANT,
              user_id=USER, env={"IRA_CONNECTOR_ENCRYPTION_KEY": KEY})


@pytest.mark.parametrize("missing", [
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
])
def test_partial_client_configuration_refuses(missing):
    env = dict(ENV)
    env[missing] = ""
    with pytest.raises(OAuthNotConfigured):
        begin(connector="gmail", operations=["search"], tenant_id=TENANT,
              user_id=USER, env=env)


def test_availability_requires_the_encryption_key_too():
    """A flow that ends by failing to store the token wastes the user's consent
    and leaves a live grant at Google that IRA cannot see."""
    assert is_available(ENV) is True
    without_key = dict(ENV)
    without_key.pop("IRA_CONNECTOR_ENCRYPTION_KEY")
    assert is_available(without_key) is False


def test_a_missing_encryption_key_refuses_before_the_user_sees_a_consent_screen():
    from core.connectors.secrets import MissingEncryptionKey

    env = dict(ENV)
    env.pop("IRA_CONNECTOR_ENCRYPTION_KEY")
    with pytest.raises(MissingEncryptionKey):
        begin(connector="gmail", operations=["search"], tenant_id=TENANT,
              user_id=USER, env=env)


# ── The authorisation URL ────────────────────────────────────────────────────


def test_the_authorisation_url_is_googles_and_carries_the_expected_parameters():
    pending = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
                    user_id=USER, env=ENV)
    assert pending.authorisation_url.startswith(GOOGLE_AUTH_URL)
    for fragment in ("response_type=code", "access_type=offline",
                     "code_challenge_method=S256", f"state={pending.state}"):
        assert fragment in pending.authorisation_url


def test_the_client_secret_never_appears_in_the_authorisation_url():
    """It is a URL the browser will follow, and it ends up in history and logs."""
    pending = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
                    user_id=USER, env=ENV)
    assert ENV["GOOGLE_OAUTH_CLIENT_SECRET"] not in pending.authorisation_url


def test_the_client_secret_is_not_in_the_clients_repr():
    """This object appears in exception contexts, and a client secret in a
    traceback is a leaked client secret."""
    client = OAuthClient(client_id="id", client_secret="s3cret", redirect_uri="u")
    assert "s3cret" not in repr(client)


def test_the_return_target_is_enumerated_not_a_url():
    """An attacker-supplied redirect target is an open redirect, and this value
    is reachable from a callback."""
    pending = begin(connector="gmail", operations=["search"], tenant_id=TENANT,
                    user_id=USER, env=ENV, return_to="https://attacker.example")
    assert pending.return_to == "connectors"


# ── Requests handed back rather than performed ───────────────────────────────


def test_the_token_exchange_sends_the_verifier_and_not_the_challenge():
    """PKCE's whole point: the verifier is revealed only at exchange time."""
    client = OAuthClient(client_id="id", client_secret="sec", redirect_uri="uri")
    url, body = token_exchange_request(code="auth-code", verifier="v" * 50,
                                       client=client)
    assert url.startswith("https://oauth2.googleapis.com/token")
    assert body["code_verifier"] == "v" * 50
    assert body["grant_type"] == "authorization_code"
    assert "code_challenge" not in body


def test_revocation_targets_the_providers_endpoint():
    """Disconnect must revoke at the provider as well as delete locally.
    Deleting IRA's copy alone leaves the grant live at Google while telling the
    user they are disconnected."""
    url, body = revocation_request("a-refresh-token")
    assert url.endswith("/revoke")
    assert body["token"] == "a-refresh-token"
