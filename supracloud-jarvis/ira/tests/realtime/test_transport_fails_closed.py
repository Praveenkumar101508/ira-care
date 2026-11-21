"""The realtime transport must refuse by default, and refuse cleanly.

A live session routes a user's microphone through somebody else's
infrastructure. The failure that matters is not "the provider was down" — it is
a build that acquires a provider by accident, or one that issues join tokens
nothing will accept and calls that working.

So the properties asserted here are all about REFUSAL:

  * unconfigured refuses;
  * partially configured refuses (the typo'd-secret case);
  * a provider this build does not implement refuses;
  * refusal is a distinct exception type, because it is expected rather than
    exceptional on every private-edition install.

And, for the one implemented provider, that the credential it mints is real:
correctly signed, narrowly scoped and short-lived.

── The limit of these tests ─────────────────────────────────────────────────

No live LiveKit server is reachable from this environment and the `livekit` SDK
is not installed, so nothing here proves a browser can join a room. What is
proven is that the token IRA issues is well-formed and correctly signed against
the documented claim set. Phase 6 claims exactly that and no more.
"""
from __future__ import annotations

from datetime import timezone

import pytest

from core.realtime.transport import (
    KNOWN_REGIONS,
    TOKEN_TTL_SECONDS,
    DenyingTransport,
    LiveKitTransport,
    RealtimeTransport,
    TransportGrant,
    TransportUnavailable,
    build_transport,
    token_lifetime,
)

# Imported directly, NOT via importorskip. PyJWT is a declared dependency
# (requirements.txt, since P16-R1) and the library api/middleware/auth.py uses,
# so a missing import here is a broken build and must be loud.
#
# The first version of this file used `pytest.importorskip("jwt")` when PyJWT
# was NOT declared and merely happened to be installed in one developer
# container. In CI the whole file skipped silently while two route tests
# elsewhere failed on the same missing library. A skip that hides an entire
# suite is indistinguishable from a suite that passes — which is why this is a
# hard import now that the library is genuinely declared.
import jwt
from jwt import PyJWTError as JWTError

SECRET = "test-secret-not-a-real-credential"
CONFIGURED = {
    "IRA_REALTIME_PROVIDER": "livekit",
    # The BROWSER-facing address. `LIVEKIT_URL` is the internal one the
    # server-side voice agent uses; see build_transport for why they differ.
    "LIVEKIT_PUBLIC_URL": "wss://example.invalid",
    "LIVEKIT_API_KEY": "APIkey123",
    "LIVEKIT_API_SECRET": SECRET,
}


# ── Refusal is the default ───────────────────────────────────────────────────


def test_an_empty_environment_yields_the_denying_transport():
    """The single most important line in this file.

    Every existing private install has no realtime configuration, and must
    remain a deployment where nothing leaves the device.
    """
    transport = build_transport({})
    assert isinstance(transport, DenyingTransport)
    assert transport.name == "none"
    assert transport.is_configured() is False


@pytest.mark.parametrize("value", ["", "none", "None", "  ", "off", "disabled"])
def test_explicitly_disabled_values_all_refuse(value):
    assert isinstance(build_transport({"IRA_REALTIME_PROVIDER": value}), DenyingTransport)


def test_an_unimplemented_provider_refuses_rather_than_guessing():
    """A name this build does not know is not a pending feature. It means the
    operator believes they configured something that does not exist, and
    guessing which vendor they meant is how media crosses infrastructure
    nobody chose."""
    transport = build_transport({"IRA_REALTIME_PROVIDER": "some-other-sfu"})
    assert isinstance(transport, DenyingTransport)
    with pytest.raises(TransportUnavailable):
        transport.open(room="r", identity="i", can_publish=True)


@pytest.mark.parametrize(
    "missing", ["LIVEKIT_PUBLIC_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
)
def test_a_partially_configured_provider_refuses(missing):
    """The typo'd-secret case.

    Two credentials out of three is a misconfiguration. Half-working here means
    a transport that appears fine and fails at the first media packet — after
    the user has already started talking.
    """
    env = dict(CONFIGURED)
    env[missing] = ""
    transport = build_transport(env)
    assert isinstance(transport, DenyingTransport), (
        f"a build missing {missing} produced a live transport"
    )


def test_a_blank_credential_is_treated_as_missing_not_as_a_value():
    env = dict(CONFIGURED, LIVEKIT_API_SECRET="   ")
    assert isinstance(build_transport(env), DenyingTransport)


def test_the_denying_transport_says_why():
    transport = DenyingTransport()
    with pytest.raises(TransportUnavailable) as exc:
        transport.open(room="r", identity="i", can_publish=True)
    assert "text-only" in str(exc.value)


def test_unavailability_is_its_own_exception_type():
    """`TransportUnavailable` is EXPECTED — every private install raises it on
    every attempt. A caller must be able to distinguish it from a provider
    that broke, because one is an incident and the other is Tuesday."""
    assert issubclass(TransportUnavailable, RuntimeError)
    with pytest.raises(TransportUnavailable):
        DenyingTransport().open(room="r", identity="i", can_publish=True)


def test_both_transports_satisfy_the_protocol():
    """Structural, not nominal: a provider added later need not inherit
    anything, but must offer the same three members."""
    assert isinstance(DenyingTransport(), RealtimeTransport)
    assert isinstance(
        LiveKitTransport(url="u", api_key="k", api_secret="s"), RealtimeTransport
    )


# ── When it is configured, the credential is real ────────────────────────────


@pytest.fixture
def livekit() -> LiveKitTransport:
    transport = build_transport(CONFIGURED)
    assert isinstance(transport, LiveKitTransport)
    return transport


def test_a_configured_build_yields_the_provider(livekit):
    assert livekit.name == "livekit"
    assert livekit.is_configured() is True


def test_the_minted_token_verifies_against_the_api_secret(livekit):
    """The signature is checked, not just the shape. A token that decodes with
    `verify_signature=False` proves nothing — LiveKit will reject it."""
    grant = livekit.open(room="ira-abc", identity="user-1", can_publish=True)
    claims = jwt.decode(grant.token, SECRET, algorithms=["HS256"])
    assert claims["iss"] == "APIkey123"
    assert claims["sub"] == "user-1"
    assert claims["video"]["room"] == "ira-abc"
    assert claims["video"]["roomJoin"] is True


def test_a_token_signed_with_the_wrong_secret_does_not_verify(livekit):
    """Confirms the previous test is actually checking something."""
    grant = livekit.open(room="r", identity="i", can_publish=True)
    with pytest.raises(JWTError):
        jwt.decode(grant.token, "a-different-secret", algorithms=["HS256"])


def test_the_token_library_is_the_one_the_project_declares():
    """Guards the mistake that broke CI on this file's first run.

    `core/realtime/transport.py` must mint tokens with the library
    requirements.txt declares and api/middleware/auth.py uses. Importing an
    undeclared one produced a build that worked in a single container and
    refused every realtime session in CI.

    Which library that is changed in P16-R1 — python-jose out, PyJWT in — so
    this reads requirements.txt rather than naming a library, and cannot again
    outlive the fact it encodes.
    """
    import inspect

    from core.realtime import transport as module

    source = inspect.getsource(module)

    # INVERTED BY P16-R1, and the inversion is the point.
    #
    # This assertion used to require `from jose import jwt` and forbid PyJWT,
    # because PyJWT was not a declared dependency and importing it produced a
    # module that worked locally and refused every session in CI.
    #
    # The RULE has not changed: use the library requirements.txt declares. The
    # PREMISE changed — P16-R1 removed python-jose (the only source of `ecdsa`,
    # whose Minerva timing attack has no upstream fix, and the only thing
    # pinning `pyasn1<0.5.0`) and declared PyJWT in its place.
    #
    # So the check now points the other way, and it is checked against
    # requirements.txt rather than hard-coded, so it cannot again outlive the
    # fact it encodes.
    from pathlib import Path
    reqs = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text()
    assert "PyJWT==" in reqs, (
        "PyJWT is no longer declared in requirements.txt, so this test is "
        "asserting the same mistake it was written to prevent — an undeclared "
        "import that works locally and fails in CI."
    )
    declared = [ln for ln in reqs.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("python-jose") for ln in declared), (
        "python-jose is declared again. It pulls `ecdsa`, whose Minerva timing "
        "attack (PYSEC-2026-1325) has no upstream fix, and pins pyasn1<0.5.0."
    )
    assert "import jwt" in source, "transport.py no longer imports the declared JWT library"
    assert "from jose import" not in source, (
        "transport.py imports python-jose, which P16-R1 removed from "
        "requirements.txt. It is absent in CI and pulls `ecdsa`."
    )


def test_the_token_is_a_participant_credential_not_an_admin_one(livekit):
    """A join token that can create rooms, list them or start a recording is
    a much larger credential than the job needs — and it is handed to a
    browser."""
    grant = livekit.open(room="r", identity="i", can_publish=True)
    video = jwt.decode(grant.token, SECRET, algorithms=["HS256"])["video"]
    assert video["roomCreate"] is False
    assert video["roomAdmin"] is False
    assert video["roomList"] is False
    assert video["recorder"] is False, (
        "the token permits starting an egress — a credential that cannot "
        "record is a stronger guarantee than a UI that does not offer it"
    )


def test_a_listener_does_not_get_a_publishing_credential(livekit):
    """`can_publish` is passed through rather than defaulted to True."""
    listener = livekit.open(room="r", identity="i", can_publish=False)
    speaker = livekit.open(room="r", identity="i", can_publish=True)
    assert jwt.decode(listener.token, SECRET, algorithms=["HS256"])["video"]["canPublish"] is False
    assert jwt.decode(speaker.token, SECRET, algorithms=["HS256"])["video"]["canPublish"] is True


def test_the_token_is_short_lived(livekit):
    grant = livekit.open(room="r", identity="i", can_publish=True)
    claims = jwt.decode(grant.token, SECRET, algorithms=["HS256"])
    assert claims["exp"] - claims["nbf"] == TOKEN_TTL_SECONDS
    assert TOKEN_TTL_SECONDS <= 900, (
        "a join token's only job is to get through the door once; a long-lived "
        "one sits in a browser tab"
    )
    lifetime = token_lifetime(grant)
    assert 0 < lifetime.total_seconds() <= TOKEN_TTL_SECONDS + 1
    assert grant.expires_at.tzinfo is not None, "an aware timestamp, or comparisons lie"
    assert grant.expires_at.utcoffset() == timezone.utc.utcoffset(None)


def test_each_token_is_distinct(livekit):
    """A replayable token is a token two people can hold."""
    a = livekit.open(room="r", identity="i", can_publish=True)
    b = livekit.open(room="r", identity="i", can_publish=True)
    assert a.token != b.token
    claims_a = jwt.decode(a.token, SECRET, algorithms=["HS256"])
    claims_b = jwt.decode(b.token, SECRET, algorithms=["HS256"])
    assert claims_a["jti"] != claims_b["jti"]


# ── The credential must not leak into logs or the database ───────────────────


def test_the_grant_does_not_print_its_token(livekit):
    """`repr` ends up in tracebacks, log lines and error reports."""
    grant = livekit.open(room="r", identity="i", can_publish=True)
    assert grant.token not in repr(grant)
    assert grant.token not in str(grant)
    assert grant.url not in repr(grant)


def test_the_redacted_view_carries_no_credential(livekit):
    """`redacted()` is what goes to the database and the logs."""
    grant = livekit.open(room="r", identity="i", can_publish=True)
    redacted = grant.redacted()
    assert "token" not in redacted
    assert "url" not in redacted
    assert redacted["provider"] == "livekit"
    assert SECRET not in repr(redacted)
    assert grant.token not in repr(redacted)


# ── Region ───────────────────────────────────────────────────────────────────


def test_an_unknown_region_falls_back_to_unknown_rather_than_being_stored():
    """The schema's CHECK would reject a free-form region at INSERT time —
    which is to say, while recording a session that is already underway."""
    transport = build_transport(dict(CONFIGURED, IRA_REALTIME_REGION="eu-west-2b"))
    assert transport.region == "unknown"
    assert transport.region in KNOWN_REGIONS


def test_a_known_region_is_carried_through_to_the_grant():
    transport = build_transport(dict(CONFIGURED, IRA_REALTIME_REGION="eu"))
    grant = transport.open(room="r", identity="i", can_publish=True)
    assert grant.region == "eu"
    assert isinstance(grant, TransportGrant)


def test_every_region_the_transport_may_produce_is_one_the_schema_accepts():
    """Guards the pair that would otherwise fail at INSERT time."""
    for region in KNOWN_REGIONS:
        transport = build_transport(dict(CONFIGURED, IRA_REALTIME_REGION=region))
        assert transport.region in KNOWN_REGIONS

# ── Which URL reaches the browser ────────────────────────────────────────────


def test_the_browser_facing_url_is_preferred_over_the_internal_one():
    """The grant's URL is handed to a browser, so it must be one a browser can
    reach.

    In the compose stack these are different hosts: `LIVEKIT_URL` is
    `ws://livekit:7880`, reachable only from inside the network. Handing that to
    a browser produces a connection failure that looks like a provider outage on
    a deployment that is configured correctly.
    """
    transport = build_transport({
        "IRA_REALTIME_PROVIDER": "livekit",
        "LIVEKIT_PUBLIC_URL": "wss://public.example.invalid",
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_API_KEY": "APIkey123",
        "LIVEKIT_API_SECRET": SECRET,
    })
    grant = transport.open(room="r", identity="i", can_publish=True)
    assert grant.url == "wss://public.example.invalid"


def test_the_internal_url_is_accepted_when_it_is_the_only_one_set():
    """A single-host install often sets only `LIVEKIT_URL`, and there the two
    addresses coincide. Refusing that would fail a working deployment."""
    transport = build_transport({
        "IRA_REALTIME_PROVIDER": "livekit",
        "LIVEKIT_URL": "wss://only.example.invalid",
        "LIVEKIT_API_KEY": "APIkey123",
        "LIVEKIT_API_SECRET": SECRET,
    })
    assert isinstance(transport, LiveKitTransport)
    grant = transport.open(room="r", identity="i", can_publish=True)
    assert grant.url == "wss://only.example.invalid"


def test_a_blank_public_url_falls_through_rather_than_winning():
    """`LIVEKIT_PUBLIC_URL=` in a .env file is a blank string, not an absent
    key. Letting it win would refuse an install that set the internal URL."""
    transport = build_transport({
        "IRA_REALTIME_PROVIDER": "livekit",
        "LIVEKIT_PUBLIC_URL": "   ",
        "LIVEKIT_URL": "wss://fallback.example.invalid",
        "LIVEKIT_API_KEY": "APIkey123",
        "LIVEKIT_API_SECRET": SECRET,
    })
    assert isinstance(transport, LiveKitTransport)
    assert transport.open(room="r", identity="i", can_publish=True).url == (
        "wss://fallback.example.invalid"
    )


def test_neither_url_set_refuses():
    transport = build_transport({
        "IRA_REALTIME_PROVIDER": "livekit",
        "LIVEKIT_API_KEY": "APIkey123",
        "LIVEKIT_API_SECRET": SECRET,
    })
    assert isinstance(transport, DenyingTransport)
