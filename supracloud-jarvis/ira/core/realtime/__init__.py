"""Real-time voice and avatar sessions — the cloud path.

Phase 6. What this package adds is the layer that lets a companion hold a live
conversation over an external transport, degrade gracefully when it cannot, and
be honest about both.

    open_session          service.py     pick the rung, get a grant, or fall to text
        ↓
    capability ladder     capability.py  full avatar → low-resource → voice → text
        ↓
    transport             transport.py   provider-neutral; DENYING by default
        ↓
    state machine         session.py     enumerated states, illegal moves raise
        ↓
    bounded reconnect     reconnect.py   finite attempts, finite total time
        ↓
    telemetry             telemetry.py   measurements only, enforced twice
        ↓
    persistence           store.py       tenant-scoped, RLS, no content

── What already existed and is untouched ────────────────────────────────────

The private browser voice path is NOT replaced by this phase. `voice/lipsync.py`
already derives viseme timings from real audio via faster-whisper forced
alignment; `VoiceConsole.tsx` already implements barge-in at `BARGE_IN_MS = 150`
through `useSpeechSyncStore.interrupt()`. Both keep working exactly as before,
with no provider configured and nothing leaving the device.

── Three claims, and their limits ───────────────────────────────────────────

**"No audio or transcripts are stored."** True of this package, and enforced by
the schema rather than by convention: `realtime_sessions.audio_stored` and
`.transcript_stored` are GENERATED ALWAYS AS (FALSE), and
`realtime_events.metrics` is CHECK-constrained to allow-listed keys with
numeric or boolean values, so no string of any name can be written.

**"Media is not end-to-end encrypted."** Stated because the opposite is the
easy thing to imply. WebRTC encrypts in transit, and the SFU terminates that
encryption — the provider can observe the media. Nothing in Phase 6 changes
this and nothing in Phase 6 claims otherwise.

**"IRA connects to LiveKit."** NOT claimed. The adapter mints a correctly
signed, narrowly scoped, short-lived join token, and the tests decode and
verify it. No live LiveKit server was reachable from this environment, so the
end-to-end join is unproven and Phase 6's report says so.
"""
from __future__ import annotations

from core.realtime.capability import (
    FLOOR,
    LADDER,
    CapabilityLevel,
    CapabilityProfile,
    best_supported,
    budget_ms,
    descend,
    is_floor,
    profile_for,
)
from core.realtime.reconnect import (
    DEFAULT_POLICY,
    NO_RETRY,
    BackoffPolicy,
    exhausted,
    schedule,
    total_ms,
)
from core.realtime.service import (
    SessionOffer,
    degrade,
    end_session,
    mark_connected,
    note_interruption,
    open_session,
    report_first_audio,
)
from core.realtime.session import (
    EndReason,
    EventKind,
    InvalidTransition,
    RealtimeSession,
    SessionEvent,
    SessionState,
    can_transition,
)
from core.realtime.telemetry import (
    ALLOWED_METRIC_KEYS,
    ContentInTelemetry,
    LatencyReport,
    assess,
    is_measurement_only,
    sanitise_metrics,
)
from core.realtime.transport import (
    DenyingTransport,
    LiveKitTransport,
    RealtimeTransport,
    TransportGrant,
    TransportUnavailable,
    build_transport,
)

__all__ = [
    "ALLOWED_METRIC_KEYS",
    "DEFAULT_POLICY",
    "FLOOR",
    "LADDER",
    "NO_RETRY",
    "BackoffPolicy",
    "CapabilityLevel",
    "CapabilityProfile",
    "ContentInTelemetry",
    "DenyingTransport",
    "EndReason",
    "EventKind",
    "InvalidTransition",
    "LatencyReport",
    "LiveKitTransport",
    "RealtimeSession",
    "RealtimeTransport",
    "SessionEvent",
    "SessionOffer",
    "SessionState",
    "TransportGrant",
    "TransportUnavailable",
    "assess",
    "best_supported",
    "budget_ms",
    "build_transport",
    "can_transition",
    "degrade",
    "descend",
    "end_session",
    "exhausted",
    "is_floor",
    "is_measurement_only",
    "mark_connected",
    "note_interruption",
    "open_session",
    "profile_for",
    "report_first_audio",
    "sanitise_metrics",
    "schedule",
    "total_ms",
]
