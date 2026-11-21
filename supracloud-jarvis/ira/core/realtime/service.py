"""Opening, degrading and ending a live session — the orchestration layer.

This is where the ladder, the transport, the state machine and the store meet.
It is deliberately thin: each of those modules is independently testable, and
the reason they are separate is that "which rung are we on" and "which vendor
carries the media" are different questions that should not be settled in the
same `if`.

── The one rule this module exists to enforce ───────────────────────────────

**A session never fails to a dead end.** Every path that cannot produce a live
avatar produces something the user can still talk to. There is no branch that
returns an error and leaves the user with nothing, because the floor of the
ladder — text — needs no provider, no microphone and no bandwidth, and has
worked since long before this phase.

That is the fail-closed rule applied to a live conversation: denying the rich
capability, never denying the conversation.

── What is verified, and what is not ────────────────────────────────────────

`open_session` is exercised end to end in tests against a real PostgreSQL
schema, with `DenyingTransport` and with a configured `LiveKitTransport` whose
minted token is decoded and checked. It has NOT been run against a live LiveKit
server — the SDK is not installed and no server is reachable from CI — so no
claim is made that a browser successfully joins a room. Phase 6's report says
so plainly.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from core.identity.principal import Principal
from core.realtime import store
from core.realtime.capability import (
    CapabilityLevel,
    FLOOR,
    best_supported,
    is_floor,
)
from core.realtime.session import (
    EndReason,
    EventKind,
    RealtimeSession,
    SessionState,
)
from core.realtime.telemetry import LatencyReport, assess
from core.realtime.transport import (
    RealtimeTransport,
    TransportGrant,
    TransportUnavailable,
    build_transport,
)

logger = logging.getLogger("ira.realtime.service")


@dataclass(frozen=True, slots=True)
class SessionOffer:
    """What the caller gets back. Always usable, even when nothing connected.

    `grant` is None whenever the session is on the text floor — which includes
    every private-edition install, every misconfigured provider, and every
    client that could not sustain audio. `reason` says which, in words meant to
    be shown to a person.
    """

    session: RealtimeSession
    grant: Optional[TransportGrant]
    reason: str

    @property
    def level(self) -> CapabilityLevel:
        return self.session.current_level

    @property
    def is_realtime(self) -> bool:
        """True only when an external transport is actually carrying media."""
        return self.grant is not None and not is_floor(self.session.current_level)

    def disclosure(self) -> dict[str, object]:
        """What the UI must show about where this conversation is going.

        Every field here is something a user would want to know before
        speaking. `audio_stored` and `transcript_stored` are hardcoded False in
        this pre-persistence view and re-read FROM THE DATABASE by
        `store.recent_sessions` — the schema is what guarantees them (they are
        GENERATED ALWAYS AS FALSE), not this dictionary.
        """
        return {
            "session_id": self.session.session_id,
            "capability_level": self.session.current_level.value,
            "provider": self.session.provider,
            "provider_region": self.session.provider_region,
            "media_leaves_device": self.is_realtime,
            "processing_location": "external_provider" if self.is_realtime else "local",
            "end_to_end_encrypted": False,
            "audio_stored": False,
            "transcript_stored": False,
            "reason": self.reason,
        }


async def open_session(
    principal: Principal,
    *,
    requested_level: CapabilityLevel | str = CapabilityLevel.FULL_AVATAR,
    downlink_kbps: Optional[int] = None,
    avatar_available: bool = True,
    audio_available: bool = True,
    companion_id: Optional[str] = None,
    transport: Optional[RealtimeTransport] = None,
    persist: bool = True,
) -> SessionOffer:
    """Open the richest live session this caller can actually sustain.

    The order matters. The rung is chosen from what the CLIENT can carry before
    the transport is consulted, so a client that asked for voice-only never
    causes an avatar-capable room to be negotiated — and a deployment with no
    provider never even reaches the transport, because the floor does not need
    one.
    """
    session = RealtimeSession(
        session_id=str(uuid.uuid4()),
        tenant_id=str(principal.tenant_id),
        user_id=str(principal.user_id) if principal.user_id else None,
        companion_id=companion_id,
    )
    session.record(EventKind.REQUESTED)

    target = best_supported(
        downlink_kbps=downlink_kbps,
        avatar_available=avatar_available,
        audio_available=audio_available,
        requested=requested_level,
    )

    if persist:
        await store.create_session(principal, session)

    # ── The floor needs nothing, so it is settled without a transport ────────
    if is_floor(target):
        session.opened_level = FLOOR
        session.current_level = FLOOR
        reason = (
            "This conversation is text only — no audio or video leaves your "
            "device."
        )
        await _finalise(principal, session, persist)
        return SessionOffer(session=session, grant=None, reason=reason)

    active = transport or build_transport()
    session.begin_connecting(
        provider=active.name, region=active.region, level=target
    )

    try:
        grant = active.open(
            room=f"ira-{session.session_id}",
            identity=session.user_id or session.session_id,
            can_publish=audio_available,
        )
    except TransportUnavailable as exc:
        # Expected on any install with no provider. Not an incident: fall to
        # the floor, say so, and keep the conversation.
        logger.info("realtime unavailable, falling to text: %s", exc)
        session.current_level = FLOOR
        session.provider = "none"
        session.provider_region = "unknown"
        session.record(EventKind.DOWNGRADED, level=FLOOR)
        session.end(EndReason.NO_PROVIDER_CONFIGURED)
        await _finalise(principal, session, persist)
        return SessionOffer(
            session=session, grant=None,
            reason=(
                "A live voice session is not available on this deployment, so "
                "the conversation is text only. Nothing leaves your device."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any provider failure
        logger.warning("realtime transport failed to open: %s", exc)
        session.current_level = FLOOR
        session.end(EndReason.PROVIDER_UNAVAILABLE)
        await _finalise(principal, session, persist)
        return SessionOffer(
            session=session, grant=None,
            reason=(
                "The live voice provider could not be reached, so the "
                "conversation has fallen back to text."
            ),
        )

    session.provider_region = grant.region
    await _finalise(principal, session, persist)
    return SessionOffer(
        session=session, grant=grant,
        reason=(
            f"Live session on {grant.provider}. Your microphone audio is sent "
            f"to that provider to be carried; it is not recorded by IRA."
        ),
    )


async def mark_connected(
    principal: Principal, session: RealtimeSession, *, persist: bool = True
) -> None:
    session.mark_connected()
    await _finalise(principal, session, persist)


async def report_first_audio(
    principal: Principal,
    session: RealtimeSession,
    *,
    measured_ms: Optional[int] = None,
    persist: bool = True,
) -> LatencyReport:
    """Record time-to-first-audio and judge it against the rung's budget."""
    measured = session.mark_first_audio(at_ms=measured_ms)
    report = assess(session.current_level, measured)
    if report.budget_ms is not None:
        session.record(
            EventKind.FIRST_AUDIO, at_ms=measured, **report.as_metrics()
        )
    await _finalise(principal, session, persist)
    return report


async def degrade(
    principal: Principal,
    session: RealtimeSession,
    *,
    downlink_kbps: Optional[int] = None,
    persist: bool = True,
) -> CapabilityLevel:
    """Drop one rung. At the floor, end the session rather than pretend.

    A session that has run out of ladder is not a live session any more. Ending
    it with `CAPABILITY_FLOOR_REACHED` is what lets the UI switch cleanly to
    text instead of showing a live indicator over a connection carrying
    nothing.
    """
    before = session.current_level
    metrics = {"downlink_kbps": downlink_kbps} if downlink_kbps is not None else None
    after = session.downgrade(reason_metrics=metrics)

    if after is before and is_floor(after):
        session.end(EndReason.CAPABILITY_FLOOR_REACHED)
    elif session.state is SessionState.LIVE:
        session.transition(SessionState.DEGRADED)

    await _finalise(principal, session, persist)
    return after


async def note_interruption(
    principal: Principal, session: RealtimeSession, *, persist: bool = True
) -> int:
    """Barge-in. The user talked over IRA; the connection is fine."""
    session.mark_interrupted()
    await _finalise(principal, session, persist)
    return session.interrupt_count


async def end_session(
    principal: Principal,
    session: RealtimeSession,
    reason: EndReason = EndReason.USER_ENDED,
    *,
    persist: bool = True,
) -> None:
    session.end(reason)
    await _finalise(principal, session, persist)


async def _finalise(
    principal: Principal, session: RealtimeSession, persist: bool
) -> None:
    """Flush state and events. Fail-soft — see the note in store.py."""
    if not persist:
        return
    await store.update_session(principal, session)
    await store.record_events(principal, session)


__all__ = [
    "SessionOffer",
    "degrade",
    "end_session",
    "mark_connected",
    "note_interruption",
    "open_session",
    "report_first_audio",
]
