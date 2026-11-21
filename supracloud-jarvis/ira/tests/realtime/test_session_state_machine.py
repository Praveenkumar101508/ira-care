"""The session state machine, and the capability ladder it moves along.

The failure this file exists to prevent is a UI stuck on "connecting". That
happens when a state change nobody enumerated is applied anyway, leaving the
session in a state the code believes is something else — and the state is
exactly what the user is shown.

So: illegal transitions raise, terminal states absorb, and every mover does its
own bookkeeping so a caller cannot forget to record why something happened.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.realtime.capability import (
    FLOOR,
    LADDER,
    CapabilityLevel,
    best_supported,
    budget_ms,
    descend,
    is_floor,
    profile_for,
)
from core.realtime.session import (
    TERMINAL,
    EndReason,
    EventKind,
    InvalidTransition,
    RealtimeSession,
    SessionState,
    can_transition,
)

TENANT = "f0f0f0f0-0000-4000-8000-00000000000a"


def new_session(**kwargs) -> RealtimeSession:
    return RealtimeSession(session_id="s-1", tenant_id=TENANT, **kwargs)


def live_session() -> RealtimeSession:
    session = new_session()
    session.begin_connecting(
        provider="livekit", region="eu", level=CapabilityLevel.FULL_AVATAR
    )
    session.mark_connected()
    return session


# ── The ladder ───────────────────────────────────────────────────────────────


def test_descent_visits_every_rung_in_order():
    """No rung is skipped, so a session's history reads as a monotonic ladder."""
    level = CapabilityLevel.FULL_AVATAR
    visited = [level]
    for _ in range(len(LADDER) + 2):     # more steps than rungs, on purpose
        level = descend(level)
        if visited[-1] is level:
            break
        visited.append(level)
    assert visited == [p.level for p in LADDER]


def test_the_floor_absorbs():
    """Descending from the floor stays on the floor rather than raising or
    wrapping around to the top."""
    assert descend(FLOOR) is FLOOR
    assert is_floor(FLOOR)


def test_an_unknown_rung_drops_straight_to_the_floor():
    """Fail closed: if we cannot tell how capable a session is, the safe answer
    is the rung that needs nothing."""
    assert descend("holodeck") is FLOOR
    assert profile_for("holodeck") is None
    assert budget_ms("holodeck") is None


@pytest.mark.parametrize("bandwidth,expected", [
    (5000, CapabilityLevel.FULL_AVATAR),
    (1500, CapabilityLevel.FULL_AVATAR),
    (1499, CapabilityLevel.LOW_RESOURCE_AVATAR),
    (600, CapabilityLevel.LOW_RESOURCE_AVATAR),
    (599, CapabilityLevel.VOICE_ONLY),
    (100, CapabilityLevel.VOICE_ONLY),
    (99, CapabilityLevel.TEXT_ONLY),
    (0, CapabilityLevel.TEXT_ONLY),
])
def test_the_rung_is_chosen_from_what_the_client_can_carry(bandwidth, expected):
    assert best_supported(
        downlink_kbps=bandwidth, avatar_available=True, audio_available=True
    ) is expected


def test_unreported_bandwidth_is_treated_as_unknown_not_as_unlimited():
    """A client that has not said what it can carry does not get the avatar.

    Treating silence as "unlimited" is how a phone on a train gets sent a video
    stream it cannot receive, and the session dies before the first word.
    """
    assert best_supported(
        downlink_kbps=None, avatar_available=True, audio_available=True
    ) is CapabilityLevel.VOICE_ONLY


def test_no_microphone_means_the_floor_however_fast_the_link():
    assert best_supported(
        downlink_kbps=100_000, avatar_available=True, audio_available=False
    ) is FLOOR


def test_no_avatar_support_skips_the_avatar_rungs_but_keeps_the_voice():
    assert best_supported(
        downlink_kbps=100_000, avatar_available=False, audio_available=True
    ) is CapabilityLevel.VOICE_ONLY


def test_a_request_for_a_lower_rung_is_never_upgraded():
    """A user who asked for voice-only does not get an avatar because the link
    turned out to be fast."""
    assert best_supported(
        downlink_kbps=100_000, avatar_available=True, audio_available=True,
        requested=CapabilityLevel.VOICE_ONLY,
    ) is CapabilityLevel.VOICE_ONLY


def test_an_unrecognised_request_is_clamped_to_the_floor():
    assert best_supported(
        downlink_kbps=100_000, avatar_available=True, audio_available=True,
        requested="cinematic_holographic_ultra",
    ) is FLOOR


# ── Legal and illegal moves ──────────────────────────────────────────────────


def test_a_new_session_starts_at_requested():
    session = new_session()
    assert session.state is SessionState.REQUESTED
    assert session.current_level is FLOOR, (
        "a session must start at the floor and be raised deliberately — "
        "starting at full_avatar means a failure part-way through leaves the "
        "record claiming a capability that never existed"
    )


def test_the_happy_path_walks_the_enumerated_states():
    session = new_session()
    session.begin_connecting(provider="livekit", region="eu",
                             level=CapabilityLevel.FULL_AVATAR)
    assert session.state is SessionState.CONNECTING
    session.mark_connected()
    assert session.state is SessionState.LIVE
    assert session.is_live
    session.end(EndReason.USER_ENDED)
    assert session.state is SessionState.ENDED
    assert session.is_finished


def test_an_illegal_transition_raises_rather_than_being_applied():
    """The whole point. A silently-applied illegal move leaves the session in a
    state the code believes is something else."""
    session = new_session()
    with pytest.raises(InvalidTransition) as exc:
        session.transition(SessionState.LIVE)   # requested → live skips connecting
    assert "requested" in str(exc.value) and "live" in str(exc.value)
    assert session.state is SessionState.REQUESTED, "the state moved anyway"


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(SessionState))
def test_terminal_states_are_absorbing(terminal, target):
    """Nothing leaves `ended` or `failed`. A session that recovers after ending
    is a NEW session — the user closed the old one and that must mean
    something."""
    if target is terminal:
        return
    assert not can_transition(terminal, target), (
        f"{terminal.value} → {target.value} is legal; terminal states must absorb"
    )


def test_ending_twice_is_a_no_op_rather_than_an_error():
    """`end` is exactly the call that gets made twice — once by the user
    hanging up and once by the transport noticing."""
    session = live_session()
    session.end(EndReason.USER_ENDED)
    first_reason, first_at, count = session.end_reason, session.ended_at, len(session.events)
    session.end(EndReason.TRANSPORT_LOST)
    assert session.end_reason is first_reason, "the second end overwrote the first"
    assert session.ended_at == first_at
    assert len(session.events) == count, "the second end recorded another event"


def test_a_failure_reason_produces_the_failed_state_not_ended():
    """"The user hung up" and "the transport died" must be distinguishable
    afterwards; both landing in `ended` erases the difference."""
    session = live_session()
    session.end(EndReason.TRANSPORT_LOST)
    assert session.state is SessionState.FAILED


def test_a_deliberate_end_produces_ended_not_failed():
    session = live_session()
    session.end(EndReason.USER_ENDED)
    assert session.state is SessionState.ENDED


def test_a_session_can_end_before_it_ever_connects():
    """The no-provider case, which is every private-edition install."""
    session = new_session()
    session.end(EndReason.NO_PROVIDER_CONFIGURED)
    assert session.state is SessionState.ENDED
    assert session.connected_at is None


# ── Degradation ──────────────────────────────────────────────────────────────


def test_degrading_drops_exactly_one_rung_and_counts_it():
    session = live_session()
    assert session.downgrade() is CapabilityLevel.LOW_RESOURCE_AVATAR
    assert session.downgrade_count == 1
    assert session.downgrade() is CapabilityLevel.VOICE_ONLY
    assert session.downgrade_count == 2


def test_degrading_at_the_floor_records_the_attempt_without_moving():
    """"We tried to degrade and there was nowhere left to go" is the state that
    precedes the session ending. Losing it makes the failure look like it came
    from nowhere."""
    session = live_session()
    for _ in range(len(LADDER)):
        session.downgrade()
    assert session.current_level is FLOOR

    before_count = session.downgrade_count
    before_events = len(session.events)
    assert session.downgrade() is FLOOR
    assert session.downgrade_count == before_count, "a no-op counted as a downgrade"
    assert len(session.events) == before_events + 1, "the attempt was not recorded"
    assert session.events[-1].kind is EventKind.DEGRADED


# ── Measurement ──────────────────────────────────────────────────────────────


def test_the_first_audio_measurement_is_the_first_one():
    """The interesting number is how long the user waited before hearing
    ANYTHING, not how long before the most recent thing."""
    session = live_session()
    assert session.mark_first_audio(at_ms=430) == 430
    assert session.mark_first_audio(at_ms=9_999) == 430
    assert session.first_audio_ms == 430


def test_elapsed_time_is_measured_from_the_request_not_the_connection():
    """The user starts waiting when they ask, not when the transport is ready.

    Measuring from `connected_at` would exclude the connection setup — which is
    most of the wait, and the part most likely to be slow.
    """
    requested = datetime.now(timezone.utc) - timedelta(seconds=3)
    session = RealtimeSession(session_id="s", tenant_id=TENANT, requested_at=requested)
    assert session.elapsed_ms() >= 3000


def test_elapsed_time_never_goes_negative():
    """Clock skew between the browser and the server is normal; a negative
    latency is not a measurement."""
    session = new_session()
    past = session.requested_at - timedelta(seconds=5)
    assert session.elapsed_ms(past) == 0


def test_interruption_is_counted_without_changing_the_connection_state():
    """Barge-in stops the playback, not the session. Modelling it as a state
    change would make talking over IRA look like a connection fault."""
    session = live_session()
    session.mark_interrupted()
    session.mark_interrupted()
    assert session.interrupt_count == 2
    assert session.state is SessionState.LIVE
    assert session.events[-1].kind is EventKind.INTERRUPTED


# ── Events are a by-product, not something a caller must remember ────────────


def test_every_mover_records_an_event():
    session = new_session()
    session.begin_connecting(provider="livekit", region="eu",
                             level=CapabilityLevel.VOICE_ONLY)
    session.mark_connected()
    session.mark_first_audio(at_ms=300)
    session.begin_reconnect(attempt=1, backoff_ms=250)
    session.mark_reconnected()
    session.end(EndReason.USER_ENDED)

    kinds = [e.kind for e in session.events]
    for expected in (
        EventKind.CONNECTING, EventKind.CONNECTED, EventKind.FIRST_AUDIO,
        EventKind.RECONNECT_ATTEMPT, EventKind.RECONNECTED, EventKind.ENDED,
    ):
        assert expected in kinds, f"{expected.value} was not recorded"


def test_events_carry_a_monotonic_offset():
    """One axis, so a browser-reported offset and a server-observed one can sit
    in the same ordered list.

    `realtime_events` is read `ORDER BY at_ms`. Without this the browser's
    "first audio at 500ms" lands after the server's "ended at 0ms elapsed" and
    the UI renders the session's events out of order.
    """
    session = live_session()
    session.mark_first_audio(at_ms=500)
    session.end(EndReason.USER_ENDED)
    offsets = [e.at_ms for e in session.events]
    assert offsets == sorted(offsets), f"event offsets are not monotonic: {offsets}"
    assert all(o >= 0 for o in offsets)


def test_ordering_is_clamped_but_the_measurement_is_not():
    """The other half of the previous test, and the more important half.

    Keeping the timeline ordered must not be done by rewriting the number being
    measured. A client reporting first audio at 500ms while the server's clock
    says 0ms elapsed has its POSITION adjusted and its MEASUREMENT preserved —
    the opposite would quietly turn every skewed clock into a latency figure of
    zero, which is the most flattering possible lie.
    """
    session = live_session()
    assert session.mark_first_audio(at_ms=500) == 500
    assert session.first_audio_ms == 500

    first_audio = [e for e in session.events if e.kind is EventKind.FIRST_AUDIO][-1]
    assert first_audio.metrics["first_audio_ms"] == 500

    session.end(EndReason.USER_ENDED)
    assert session.events[-1].at_ms >= 500, "the end event landed before the audio"


def test_reconnects_are_counted():
    session = live_session()
    session.begin_reconnect(attempt=1, backoff_ms=250)
    session.mark_reconnected()
    session.begin_reconnect(attempt=2, backoff_ms=500)
    assert session.reconnect_count == 2
