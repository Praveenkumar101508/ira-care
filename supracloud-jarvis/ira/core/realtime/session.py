"""The realtime session state machine.

A live conversation is not request/response. It is a thing that exists over
time, is interrupted, degrades, drops and comes back — and every one of those
is a state a user can be looking at. Writing that as ad-hoc booleans
(`connected`, `reconnecting`, `failed`) is how a UI ends up showing "connecting"
forever after a failure nobody handled.

So the states are enumerated, the legal transitions are enumerated, and
anything else raises. A transition nobody thought about is a bug, and it
surfaces as an exception at the moment it happens rather than as a stuck
spinner ten minutes later.

    requested ─→ connecting ─→ live ⇄ degraded
                     │           │      │
                     │           └──┬───┘
                     │              ↓
                     │         reconnecting ─→ live | degraded
                     ↓              │
                  failed ←──────────┘
                     ▲
    (any) ───────────┴──→ ended

`ended` and `failed` are terminal and absorbing: nothing leaves them. A session
that "recovers" after ending is a new session, with its own row, because the
user closed the old one and is entitled to have that mean something.

── Interruption is not a state ──────────────────────────────────────────────

Barge-in — the user talking over IRA — does not change the session state. It is
an EVENT within a live session, because the connection is fine; what stopped is
the playback. The existing private voice path already implements this
(`useSpeechSyncStore.interrupt()`, `BARGE_IN_MS = 150` in VoiceConsole.tsx) and
Phase 6 does not replace it. Recording it as an event lets the telemetry show
how often IRA is being talked over without inventing a connection state that
does not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final, Optional

from core.realtime.capability import CapabilityLevel, FLOOR, descend, is_floor


class SessionState(str, Enum):
    REQUESTED = "requested"
    CONNECTING = "connecting"
    LIVE = "live"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    ENDED = "ended"
    FAILED = "failed"


#: Why a session stopped. Enumerated to match the schema's CHECK — an
#: unenumerated reason would be rejected by the database at the worst possible
#: moment, which is while trying to record that something already went wrong.
class EndReason(str, Enum):
    USER_ENDED = "user_ended"
    USER_INTERRUPTED = "user_interrupted"
    IDLE_TIMEOUT = "idle_timeout"
    TRANSPORT_LOST = "transport_lost"
    RECONNECT_EXHAUSTED = "reconnect_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NO_PROVIDER_CONFIGURED = "no_provider_configured"
    SERVER_SHUTDOWN = "server_shutdown"
    CAPABILITY_FLOOR_REACHED = "capability_floor_reached"
    ERROR = "error"
    #: P14-01b, finding X. The session stopped and the reason given was not one
    #: of the above — a mistyped code, or a newer client's vocabulary. It is
    #: deliberately NOT `error`: "this failed" and "we do not know how this
    #: ended" are different facts, and collapsing the second into the first
    #: turns a future client's benign new code into a false failed session.
    #: Added to the schema's CHECK by migration 084.
    UNKNOWN = "unknown"


#: The reasons that mean the session did not finish cleanly, i.e. the ones
#: `Session.end` turns into `FAILED` rather than `ENDED`.
#:
#: Defined here rather than inline in `end()` because P14-01b adds a member to
#: it, and a membership rule spelled out in one place can be asserted; the same
#: rule written as a tuple literal inside a method can only be re-read.
ABNORMAL_END_REASONS: Final[frozenset[EndReason]] = frozenset({
    EndReason.TRANSPORT_LOST,
    EndReason.RECONNECT_EXHAUSTED,
    EndReason.PROVIDER_UNAVAILABLE,
    EndReason.ERROR,
    # P14-01b, finding X, and the owner's direction in one line: an unknown
    # termination is NOT a clean hang-up.
    EndReason.UNKNOWN,
})

#: The subset of the above that is a KNOWN failure. `UNKNOWN` is deliberately
#: outside it: P13-01 recorded the objection to folding an unrecognised reason
#: into `error` — it turns a future client's benign new code into a false failed
#: session — and that objection is answered by keeping the two separable, not by
#: calling the unknown one clean.
KNOWN_FAILURE_END_REASONS: Final[frozenset[EndReason]] = (
    ABNORMAL_END_REASONS - {EndReason.UNKNOWN}
)


class EventKind(str, Enum):
    REQUESTED = "requested"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FIRST_AUDIO = "first_audio"
    DEGRADED = "degraded"
    DOWNGRADED = "downgraded"
    UPGRADED = "upgraded"
    RECONNECT_ATTEMPT = "reconnect_attempt"
    RECONNECTED = "reconnected"
    INTERRUPTED = "interrupted"
    MICROPHONE_OPENED = "microphone_opened"
    MICROPHONE_CLOSED = "microphone_closed"
    ENDED = "ended"
    FAILED = "failed"


TERMINAL: Final[frozenset[SessionState]] = frozenset(
    {SessionState.ENDED, SessionState.FAILED}
)

#: Every legal move. Absent means illegal — there is no wildcard.
_TRANSITIONS: Final[dict[SessionState, frozenset[SessionState]]] = {
    SessionState.REQUESTED: frozenset({
        SessionState.CONNECTING, SessionState.FAILED, SessionState.ENDED,
    }),
    SessionState.CONNECTING: frozenset({
        SessionState.LIVE, SessionState.DEGRADED,
        SessionState.FAILED, SessionState.ENDED,
    }),
    SessionState.LIVE: frozenset({
        SessionState.DEGRADED, SessionState.RECONNECTING,
        SessionState.ENDED, SessionState.FAILED,
    }),
    SessionState.DEGRADED: frozenset({
        SessionState.LIVE, SessionState.RECONNECTING,
        SessionState.ENDED, SessionState.FAILED,
    }),
    SessionState.RECONNECTING: frozenset({
        SessionState.LIVE, SessionState.DEGRADED,
        SessionState.FAILED, SessionState.ENDED,
    }),
    SessionState.ENDED: frozenset(),
    SessionState.FAILED: frozenset(),
}


class InvalidTransition(RuntimeError):
    """An illegal state change. Raised, never swallowed.

    Swallowing it would leave the session in a state the code believes is
    something else — and the state is what the user is shown.
    """


def can_transition(current: SessionState, target: SessionState) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One thing that happened, at a known offset, with numeric metrics only.

    `metrics` is validated by `telemetry.sanitise_metrics` before it gets here
    or before it reaches the database — the dataclass does not enforce it, the
    boundary does, and the database enforces it again.
    """

    kind: EventKind
    at_ms: int
    level: Optional[CapabilityLevel] = None
    metrics: dict[str, float | int | bool] = field(default_factory=dict)


@dataclass
class RealtimeSession:
    """One attempt at a live conversation, and everything measured about it.

    Mutable by design: it is a state machine, and the whole point is that it
    moves. Every mover records an event, so the history is a by-product of the
    transitions rather than something a caller has to remember to log.
    """

    session_id: str
    tenant_id: str
    user_id: Optional[str] = None
    companion_id: Optional[str] = None

    provider: str = "none"
    provider_region: str = "unknown"

    state: SessionState = SessionState.REQUESTED
    opened_level: CapabilityLevel = FLOOR
    current_level: CapabilityLevel = FLOOR

    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    connected_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    first_audio_ms: Optional[int] = None
    reconnect_count: int = 0
    downgrade_count: int = 0
    interrupt_count: int = 0
    end_reason: Optional[EndReason] = None

    events: list[SessionEvent] = field(default_factory=list)

    # ── Clock ────────────────────────────────────────────────────────────────
    # One monotonic axis, measured from `requested_at`, so events from the
    # server and events reported by the browser are comparable without trusting
    # that two clocks agree.

    def elapsed_ms(self, at: Optional[datetime] = None) -> int:
        now = at or datetime.now(timezone.utc)
        return max(0, int((now - self.requested_at).total_seconds() * 1000))

    # ── Movement ─────────────────────────────────────────────────────────────

    def record(
        self,
        kind: EventKind,
        *,
        at_ms: Optional[int] = None,
        level: Optional[CapabilityLevel] = None,
        **metrics: float | int | bool,
    ) -> SessionEvent:
        """Append an event at a non-decreasing offset.

        Two clocks feed this timeline: the server's, and the browser's via an
        explicit `at_ms`. They disagree — that is what clock skew is — and a
        naive append produces a list that jumps backwards, which the UI renders
        as events out of order (`realtime_events` is read `ORDER BY at_ms`).

        So the ORDERING is clamped to be non-decreasing while the MEASUREMENT
        is not touched: `first_audio_ms` on the session, and the metrics on the
        event, keep the number the client actually reported. Clamping the
        measurement itself would destroy the thing being measured; clamping
        only its position on the timeline costs nothing real, because a session
        genuinely cannot have ended before it produced its first audio.
        """
        measured = self.elapsed_ms() if at_ms is None else max(0, int(at_ms))
        ordered = max(measured, self.events[-1].at_ms) if self.events else measured
        event = SessionEvent(
            kind=kind,
            at_ms=ordered,
            level=level or self.current_level,
            metrics=dict(metrics),
        )
        self.events.append(event)
        return event

    def transition(self, target: SessionState) -> None:
        if self.state is target:
            return
        if not can_transition(self.state, target):
            raise InvalidTransition(
                f"realtime session {self.session_id}: {self.state.value} → "
                f"{target.value} is not a legal transition"
                + (" (terminal states are absorbing)" if self.state in TERMINAL else "")
            )
        self.state = target

    # ── The moves, each of which owns its bookkeeping ────────────────────────

    def begin_connecting(self, *, provider: str, region: str,
                         level: CapabilityLevel) -> None:
        self.transition(SessionState.CONNECTING)
        self.provider = provider
        self.provider_region = region
        self.opened_level = level
        self.current_level = level
        self.record(EventKind.CONNECTING, level=level)

    def mark_connected(self, *, at: Optional[datetime] = None) -> None:
        self.transition(SessionState.LIVE)
        self.connected_at = at or datetime.now(timezone.utc)
        self.record(EventKind.CONNECTED, connect_ms=self.elapsed_ms(self.connected_at))

    def mark_first_audio(self, *, at_ms: Optional[int] = None) -> int:
        """Record time-to-first-audio. The first call wins.

        A second call would overwrite the measurement with a later one — the
        interesting number is how long the user waited before hearing anything,
        not how long before the most recent thing.
        """
        measured = self.elapsed_ms() if at_ms is None else max(0, int(at_ms))
        if self.first_audio_ms is None:
            self.first_audio_ms = measured
        self.record(EventKind.FIRST_AUDIO, at_ms=measured,
                    first_audio_ms=self.first_audio_ms)
        return self.first_audio_ms

    def downgrade(self, *, reason_metrics: Optional[dict] = None) -> CapabilityLevel:
        """Drop one rung. At the floor this is a no-op that still records why.

        Recording the attempt even when nothing moves matters: "we tried to
        degrade and there was nowhere left to go" is the state that precedes
        `CAPABILITY_FLOOR_REACHED`, and losing it makes the eventual failure
        look like it came from nowhere.
        """
        before = self.current_level
        after = descend(before)
        metrics = dict(reason_metrics or {})
        if after is before or is_floor(before):
            self.record(EventKind.DEGRADED, level=before, **metrics)
            return before
        self.current_level = after
        self.downgrade_count += 1
        self.record(EventKind.DOWNGRADED, level=after, **metrics)
        return after

    def begin_reconnect(self, *, attempt: int, backoff_ms: int) -> None:
        self.transition(SessionState.RECONNECTING)
        self.reconnect_count += 1
        self.record(EventKind.RECONNECT_ATTEMPT, attempt=attempt, backoff_ms=backoff_ms)

    def mark_reconnected(self) -> None:
        self.transition(SessionState.LIVE)
        self.record(EventKind.RECONNECTED)

    def mark_interrupted(self) -> None:
        """Barge-in. Not a state change — the connection is fine."""
        self.interrupt_count += 1
        self.record(EventKind.INTERRUPTED)

    def end(self, reason: EndReason, *, at: Optional[datetime] = None) -> None:
        """Finish the session. Idempotent, and a reason is mandatory.

        Idempotent because `end` is exactly the call that gets made twice — once
        by the user hanging up and once by the transport noticing. The schema
        requires a reason for every terminal state, so there is no way to end a
        session without saying why.
        """
        if self.state in TERMINAL:
            return
        failed = reason in ABNORMAL_END_REASONS
        self.transition(SessionState.FAILED if failed else SessionState.ENDED)
        self.ended_at = at or datetime.now(timezone.utc)
        self.end_reason = reason
        self.record(
            EventKind.FAILED if failed else EventKind.ENDED,
            at_ms=self.elapsed_ms(self.ended_at),
        )

    # ── Read-only views ──────────────────────────────────────────────────────

    @property
    def is_live(self) -> bool:
        return self.state in (SessionState.LIVE, SessionState.DEGRADED)

    @property
    def is_finished(self) -> bool:
        return self.state in TERMINAL

    @property
    def ended_cleanly(self) -> bool:
        """A finished session that finished the way it was supposed to.

        P14-01b, finding X. Exists so that "did this end cleanly" and "is this
        finished" cannot be confused by a caller, which is precisely what the
        `user_ended` fallback used to make impossible to tell apart.
        """
        return (self.state is SessionState.ENDED
                and self.end_reason is not None
                and self.end_reason not in ABNORMAL_END_REASONS)

    @property
    def ended_unknown(self) -> bool:
        """Finished, not cleanly, and nobody could say why.

        Separate from a known failure on purpose: an operator counting outages
        should not be handed the sessions whose reason code was merely
        unrecognised, and an operator hunting client bugs wants exactly those.
        """
        return self.end_reason is EndReason.UNKNOWN


__all__ = [
    "ABNORMAL_END_REASONS",
    "KNOWN_FAILURE_END_REASONS",
    "TERMINAL",
    "EndReason",
    "EventKind",
    "InvalidTransition",
    "RealtimeSession",
    "SessionEvent",
    "SessionState",
    "can_transition",
]
