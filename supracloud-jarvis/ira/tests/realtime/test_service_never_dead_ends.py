"""However a live session fails, the user is left with something to talk to.

This is the fail-closed rule applied to a conversation. "Fail closed" for a
tool call means refusing to act; for a live session it means denying the RICH
CAPABILITY, never denying the conversation — because the floor of the ladder is
text, which needs no provider, no microphone and no bandwidth, and has worked
since long before this phase.

The test that matters is `test_no_configuration_produces_a_dead_end`: it walks
every failure the transport can produce and asserts each one still yields a
usable offer with a truthful explanation.

Persistence is switched off here (`persist=False`) so these run without a
database. The store is exercised against a real schema in
tests/tenancy/test_realtime_isolation_rls.py.
"""
from __future__ import annotations

import uuid

import pytest

from core.realtime import service
from core.realtime.capability import FLOOR, CapabilityLevel
from core.realtime.session import EndReason, EventKind, SessionState
from core.realtime.transport import (
    DenyingTransport,
    LiveKitTransport,
    TransportGrant,
    TransportUnavailable,
)

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID("f0f0f0f0-0000-4000-8000-00000000000a")
USER = uuid.UUID("f0f0f0f0-0000-4000-8000-0000000000aa")


class FakePrincipal:
    """Enough of a Principal for the service. The real one is exercised in the
    RLS suite, where its tenant binding is the thing under test."""

    tenant_id = TENANT
    user_id = USER


class ExplodingTransport:
    """A provider that fails in an unanticipated way — not the polite
    `TransportUnavailable`, but whatever a vendor SDK throws at 3am."""

    name = "livekit"
    region = "eu"

    def is_configured(self) -> bool:
        return True

    def open(self, *, room, identity, can_publish):
        raise RuntimeError("the SDK panicked")


class WorkingTransport:
    name = "livekit"
    region = "eu"

    def is_configured(self) -> bool:
        return True

    def open(self, *, room, identity, can_publish) -> TransportGrant:
        return TransportGrant(
            provider=self.name, region=self.region, room=room,
            identity=identity, url="wss://example.invalid", token="t0ken",
        )


@pytest.fixture
def principal() -> FakePrincipal:
    return FakePrincipal()


async def offer(principal, **kwargs):
    kwargs.setdefault("persist", False)
    return await service.open_session(principal, **kwargs)


# ── The property this file exists for ────────────────────────────────────────


@pytest.mark.parametrize("transport,label", [
    (DenyingTransport(), "no provider configured"),
    (ExplodingTransport(), "provider raised an unexpected error"),
    (LiveKitTransport(url="", api_key="", api_secret=""), "provider misconfigured"),
])
async def test_no_configuration_produces_a_dead_end(principal, transport, label):
    """Every failure path still yields a usable session and an explanation."""
    result = await offer(principal, transport=transport, downlink_kbps=5000)

    assert result.session is not None, f"{label}: no session at all"
    assert result.level is FLOOR, f"{label}: not on the text floor"
    assert result.grant is None, f"{label}: a grant was issued anyway"
    assert result.is_realtime is False
    assert result.reason, f"{label}: no explanation for the user"
    assert result.session.is_finished, (
        f"{label}: the session was left open with nothing carrying it"
    )


async def test_the_default_build_falls_to_text_without_being_asked(principal):
    """No transport argument at all — the path a real private install takes.

    `build_transport()` reads the process environment; with nothing configured
    it returns the refusal.
    """
    result = await offer(principal, downlink_kbps=5000)
    assert result.level is FLOOR
    assert result.session.provider == "none"
    assert result.is_realtime is False


async def test_the_no_provider_case_is_recorded_as_such_and_not_as_an_error(principal):
    """It is Tuesday on every private install, not an incident. Recording it as
    a generic error would bury real provider failures in noise."""
    result = await offer(principal, transport=DenyingTransport(), downlink_kbps=5000)
    assert result.session.end_reason is EndReason.NO_PROVIDER_CONFIGURED
    assert result.session.state is SessionState.ENDED, "a normal end, not a failure"


async def test_an_unexpected_provider_error_is_recorded_as_a_failure(principal):
    result = await offer(principal, transport=ExplodingTransport(), downlink_kbps=5000)
    assert result.session.end_reason is EndReason.PROVIDER_UNAVAILABLE
    assert result.session.state is SessionState.FAILED


async def test_a_client_that_cannot_do_audio_never_reaches_the_transport(principal):
    """The floor needs no provider, so a text session must not negotiate one.

    Consulting the transport anyway would mean a text conversation triggers an
    external connection — which is precisely the thing the user chose against.
    """
    class Tripwire(DenyingTransport):
        def open(self, **kwargs):  # pragma: no cover - the assertion is that this never runs
            raise AssertionError("the transport was consulted for a text session")

    result = await offer(
        principal, transport=Tripwire(), audio_available=False, downlink_kbps=5000
    )
    assert result.level is FLOOR
    assert result.session.provider == "none"


# ── When it does work ────────────────────────────────────────────────────────


async def test_a_working_transport_yields_a_grant_at_the_chosen_rung(principal):
    result = await offer(
        principal, transport=WorkingTransport(), downlink_kbps=5000
    )
    assert result.grant is not None
    assert result.is_realtime is True
    assert result.level is CapabilityLevel.FULL_AVATAR
    assert result.session.provider == "livekit"
    assert result.session.provider_region == "eu"


async def test_the_rung_is_chosen_before_the_transport_is_consulted(principal):
    """A client that asked for voice-only must not cause an avatar-capable room
    to be negotiated."""
    result = await offer(
        principal, transport=WorkingTransport(), downlink_kbps=5000,
        requested_level=CapabilityLevel.VOICE_ONLY,
    )
    assert result.level is CapabilityLevel.VOICE_ONLY
    assert result.session.opened_level is CapabilityLevel.VOICE_ONLY


async def test_a_slow_link_opens_lower_and_says_so(principal):
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=700)
    assert result.level is CapabilityLevel.LOW_RESOURCE_AVATAR
    assert result.session.opened_level is CapabilityLevel.LOW_RESOURCE_AVATAR


# ── The disclosure is truthful in both directions ────────────────────────────


async def test_the_text_disclosure_says_nothing_leaves_the_device(principal):
    result = await offer(principal, transport=DenyingTransport(), downlink_kbps=5000)
    disclosure = result.disclosure()
    assert disclosure["media_leaves_device"] is False
    assert disclosure["processing_location"] == "local"
    assert disclosure["provider"] == "none"


async def test_the_live_disclosure_admits_the_media_leaves_the_device(principal):
    """The direction that is tempting to soften. A live session routes the
    user's microphone through someone else's infrastructure and the disclosure
    has to say so."""
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    disclosure = result.disclosure()
    assert disclosure["media_leaves_device"] is True
    assert disclosure["processing_location"] == "external_provider"
    assert disclosure["provider"] == "livekit"
    assert "not recorded by IRA" in result.reason


async def test_the_disclosure_never_claims_end_to_end_encryption(principal):
    """WebRTC encrypts in transit and the SFU terminates that encryption — the
    provider can observe the media. Claiming otherwise would be the single most
    misleading thing this surface could say."""
    for transport in (DenyingTransport(), WorkingTransport()):
        result = await offer(principal, transport=transport, downlink_kbps=5000)
        assert result.disclosure()["end_to_end_encrypted"] is False


async def test_the_disclosure_never_claims_stored_audio_or_transcripts(principal):
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    disclosure = result.disclosure()
    assert disclosure["audio_stored"] is False
    assert disclosure["transcript_stored"] is False


async def test_the_disclosure_carries_no_credential(principal):
    """It is rendered in a UI and may be logged."""
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    rendered = repr(result.disclosure())
    assert "t0ken" not in rendered
    assert "wss://" not in rendered


# ── Degradation and the end of the ladder ────────────────────────────────────


async def test_degrading_walks_down_and_then_ends_the_session(principal):
    """A session that has run out of ladder is not a live session any more.

    Ending it lets the UI switch cleanly to text instead of showing a live
    indicator over a connection carrying nothing.
    """
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    session = result.session
    await service.mark_connected(principal, session, persist=False)

    assert await service.degrade(principal, session, persist=False) \
        is CapabilityLevel.LOW_RESOURCE_AVATAR
    assert await service.degrade(principal, session, persist=False) \
        is CapabilityLevel.VOICE_ONLY
    assert await service.degrade(principal, session, persist=False) is FLOOR
    assert not session.is_finished, "ended while still on a usable rung"

    assert await service.degrade(principal, session, persist=False) is FLOOR
    assert session.is_finished
    assert session.end_reason is EndReason.CAPABILITY_FLOOR_REACHED


async def test_a_degraded_session_is_still_live(principal):
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    session = result.session
    await service.mark_connected(principal, session, persist=False)
    await service.degrade(principal, session, persist=False)
    assert session.state is SessionState.DEGRADED
    assert session.is_live, "a degraded session still carries the conversation"


# ── Latency ──────────────────────────────────────────────────────────────────


async def test_first_audio_is_measured_against_the_rungs_budget(principal):
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    session = result.session
    await service.mark_connected(principal, session, persist=False)

    report = await service.report_first_audio(
        principal, session, measured_ms=400, persist=False
    )
    assert report.measured_ms == 400
    assert report.budget_ms == 1200
    assert report.within_budget is True


async def test_an_overrun_is_reported_rather_than_smoothed(principal):
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    session = result.session
    await service.mark_connected(principal, session, persist=False)

    report = await service.report_first_audio(
        principal, session, measured_ms=5_000, persist=False
    )
    assert report.within_budget is False
    assert report.overrun_ms == 3_800


async def test_interruption_is_recorded_without_ending_the_session(principal):
    """Barge-in. The existing private voice path already implements this
    (VoiceConsole.tsx, BARGE_IN_MS = 150); recording it here lets the telemetry
    show how often IRA is talked over without inventing a connection state."""
    result = await offer(principal, transport=WorkingTransport(), downlink_kbps=5000)
    session = result.session
    await service.mark_connected(principal, session, persist=False)

    assert await service.note_interruption(principal, session, persist=False) == 1
    assert await service.note_interruption(principal, session, persist=False) == 2
    assert session.state is SessionState.LIVE
    assert session.is_live


# ── Every session leaves a record, including the ones that never connect ─────


async def test_even_a_failed_session_has_an_event_trail(principal):
    """"IRA tried to open a live session and could not" is information the user
    is entitled to. A record that only exists on success cannot express it."""
    result = await offer(principal, transport=DenyingTransport(), downlink_kbps=5000)
    kinds = [e.kind for e in result.session.events]
    assert EventKind.REQUESTED in kinds
    assert EventKind.ENDED in kinds


async def test_a_session_gets_its_own_identifier(principal):
    a = await offer(principal, downlink_kbps=5000)
    b = await offer(principal, downlink_kbps=5000)
    assert a.session.session_id != b.session.session_id
    uuid.UUID(a.session.session_id)   # raises if it is not a real UUID


async def test_the_session_is_bound_to_the_callers_tenant(principal):
    result = await offer(principal, downlink_kbps=5000)
    assert result.session.tenant_id == str(TENANT)
    assert result.session.user_id == str(USER)
