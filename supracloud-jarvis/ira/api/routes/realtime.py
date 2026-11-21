"""Real-time session endpoints — and the disclosure that goes with them.

GET    /api/v1/realtime/capabilities        -> the ladder, and whether realtime is available at all
POST   /api/v1/realtime/sessions            -> open the richest session this client can sustain
POST   /api/v1/realtime/sessions/{id}/connected -> the client is in the room
POST   /api/v1/realtime/sessions/{id}/first-audio -> measured time-to-first-audio
POST   /api/v1/realtime/sessions/{id}/degrade   -> drop one rung
POST   /api/v1/realtime/sessions/{id}/interrupt -> barge-in
POST   /api/v1/realtime/sessions/{id}/end       -> finish
GET    /api/v1/realtime/sessions            -> recent sessions, with what was stored
GET    /api/v1/realtime/sessions/{id}/events -> one session's telemetry

Three properties worth stating explicitly.

**The join token is returned exactly once, and never persisted.** It is minted
per request, expires in five minutes, and appears only in the response body of
`POST /sessions`. `GET /sessions` reads from the database, which never held it.

**Every response carries the disclosure.** Not a separate endpoint a client can
neglect to call: `media_leaves_device`, `provider`, `end_to_end_encrypted`,
`audio_stored` and `transcript_stored` ride along with the session itself, so a
UI cannot render a live indicator without also having been handed the facts
about where the audio is going.

**There is no endpoint that enables recording.** Not one that is permission-
gated — one that does not exist. The schema cannot store audio or transcripts
(see 027_realtime.sql), so an endpoint offering it would have nowhere to write.

── What this does NOT do ───────────────────────────────────────────────────

It does not replace the existing private browser voice path. `/api/v1/voice/*`
is untouched: it still synthesises locally, still derives viseme timings from
real audio, and still needs no provider. A deployment with no realtime provider
configured gets `available: false` here and keeps working exactly as before.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from core.realtime import service as _service
from core.realtime import store as _store
from core.realtime.capability import LADDER, CapabilityLevel
from core.realtime.reconnect import DEFAULT_POLICY, schedule
from core.realtime.session import EndReason
from core.realtime.transport import DenyingTransport, build_transport

router = APIRouter(prefix="/realtime", tags=["realtime"])

logger = logging.getLogger("ira.realtime")

#: Sessions live in memory only for the lifetime of the request that created
#: them; every subsequent call rehydrates from the database. Keeping a
#: process-local session map would break the moment there are two workers, and
#: would break silently — the second worker would report "unknown session" for
#: a session that is live.
_SESSION_NOT_FOUND = "no such realtime session for this account"


class OpenSessionBody(BaseModel):
    requested_level: str = Field("full_avatar", max_length=40)
    #: What the client measured it can receive. Omitted means "unknown", which
    #: is treated as unknown and not as unlimited — see capability.py.
    downlink_kbps: Optional[int] = Field(None, ge=0, le=10_000_000)
    avatar_available: bool = True
    audio_available: bool = True
    companion_id: Optional[str] = None


class FirstAudioBody(BaseModel):
    #: Measured by the client, from when it asked to when it heard something.
    measured_ms: int = Field(..., ge=0, le=600_000)


class DegradeBody(BaseModel):
    downlink_kbps: Optional[int] = Field(None, ge=0, le=10_000_000)


class EndBody(BaseModel):
    reason: str = Field("user_ended", max_length=40)


def _session_uuid(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(session_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_SESSION_NOT_FOUND
        ) from exc


async def _load(principal, session_id: str) -> dict[str, Any]:
    """Fetch one of the caller's sessions, or 404.

    RLS scopes the read to the caller's tenant, so "belongs to someone else"
    and "does not exist" are the same answer here — which is the answer that
    leaks nothing.
    """
    wanted = _session_uuid(session_id)
    for row in await _store.recent_sessions(principal, limit=100):
        if row["session_id"] == str(wanted):
            return row
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=_SESSION_NOT_FOUND
    )


# ── Capabilities ─────────────────────────────────────────────────────────────


@router.get("/capabilities")
async def get_capabilities(_principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """The ladder, and an honest answer about whether realtime works here.

    `available: false` is the correct, expected answer on every deployment with
    no provider configured — which is every private-edition install. It is not
    an error, and the UI must present it as "voice runs on your device" rather
    than as a fault.
    """
    transport = build_transport()
    configured = transport.is_configured()
    return {
        "available": configured,
        "provider": transport.name if configured else "none",
        "provider_region": transport.region if configured else "unknown",
        "reason": (
            "Live sessions are carried by an external provider."
            if configured
            else "No realtime provider is configured. Voice runs on this "
                 "device and nothing is sent anywhere."
        ),
        "levels": [
            {
                "level": profile.level.value,
                "rung": profile.rung,
                "renders_avatar": profile.renders_avatar,
                "produces_audio": profile.produces_audio,
                "target_first_audio_ms": profile.target_first_audio_ms,
                "min_downlink_kbps": profile.min_downlink_kbps,
                # The floor is always reachable; everything above it needs the
                # provider that `available` describes.
                "reachable": profile.rung == len(LADDER) or configured,
            }
            for profile in LADDER
        ],
        "reconnect": {
            "max_attempts": DEFAULT_POLICY.max_attempts,
            "max_total_ms": DEFAULT_POLICY.max_total_ms,
            "planned_delays_ms": schedule(DEFAULT_POLICY),
        },
        # Stated here as well as per-session so a client can show the truth
        # before anybody has pressed anything.
        "disclosure": {
            "end_to_end_encrypted": False,
            "audio_stored": False,
            "transcript_stored": False,
            "note": (
                "During a live session your microphone audio is carried by the "
                "provider named above, which can observe it. IRA does not "
                "record the audio or store a transcript of it."
            ),
        },
    }


# ── Sessions ─────────────────────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def open_session(
    body: OpenSessionBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Open the richest session this client can sustain, or fall to text.

    This endpoint does not fail. A missing provider, a broken provider and a
    client that cannot do audio all return 201 with a text-only session and an
    explanation, because the alternative is denying the conversation rather
    than the capability.
    """
    offer = await _service.open_session(
        principal,
        requested_level=body.requested_level,
        downlink_kbps=body.downlink_kbps,
        avatar_available=body.avatar_available,
        audio_available=body.audio_available,
        companion_id=body.companion_id,
    )

    response: dict[str, Any] = {
        "session_id": offer.session.session_id,
        "state": offer.session.state.value,
        "capability_level": offer.level.value,
        "opened_level": offer.session.opened_level.value,
        "is_realtime": offer.is_realtime,
        "reason": offer.reason,
        "disclosure": offer.disclosure(),
    }
    if offer.grant is not None:
        # The one place the credential appears. Minted per request, expiring in
        # minutes, never written to the database.
        response["join"] = {
            "url": offer.grant.url,
            "token": offer.grant.token,
            "room": offer.grant.room,
            "expires_at": offer.grant.expires_at.isoformat(),
        }
    return response


@router.get("/sessions")
async def list_sessions(
    limit: int = 20, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Recent sessions — the answer to "who else has heard me?".

    `audio_stored` and `transcript_stored` come back from the database, where
    they are GENERATED ALWAYS AS (FALSE). They are not hardcoded here, so if
    that ever changes this surface starts telling the truth about it
    immediately rather than continuing to say what it used to.
    """
    return {"sessions": await _store.recent_sessions(principal, limit=limit)}


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: str, limit: int = 200, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """One session's telemetry. Measurements only — the schema holds nothing else."""
    await _load(principal, session_id)
    return {
        "session_id": session_id,
        "events": await _store.session_events(
            principal, _session_uuid(session_id), limit=limit
        ),
    }


# ── Transitions ──────────────────────────────────────────────────────────────
#
# Each rehydrates the session from the database rather than from process
# memory. Two workers behind a load balancer would otherwise disagree about
# which sessions exist, and would do it silently.


async def _rehydrate(principal, session_id: str):
    from core.realtime.session import RealtimeSession, SessionState

    row = await _load(principal, session_id)
    session = RealtimeSession(
        session_id=row["session_id"],
        tenant_id=str(principal.tenant_id),
        user_id=str(principal.user_id) if principal.user_id else None,
        provider=row["provider"],
        provider_region=row["provider_region"],
        state=SessionState(row["state"]),
        opened_level=CapabilityLevel(row["opened_level"]),
        current_level=CapabilityLevel(row["current_level"]),
        first_audio_ms=row["first_audio_ms"],
        reconnect_count=row["reconnect_count"],
        downgrade_count=row["downgrade_count"],
    )
    if row["requested_at"]:
        from datetime import datetime
        session.requested_at = datetime.fromisoformat(row["requested_at"])
    return session


def _state(session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "state": session.state.value,
        "capability_level": session.current_level.value,
        "reconnect_count": session.reconnect_count,
        "downgrade_count": session.downgrade_count,
    }


@router.post("/sessions/{session_id}/connected")
async def mark_connected(
    session_id: str, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    session = await _rehydrate(principal, session_id)
    try:
        await _service.mark_connected(principal, session)
    except Exception as exc:  # noqa: BLE001 - an illegal transition is a 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _state(session)


@router.post("/sessions/{session_id}/first-audio")
async def report_first_audio(
    session_id: str, body: FirstAudioBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Record the client's measured time-to-first-audio, and judge it.

    The verdict can be — and often is — "over budget". That is the measurement
    doing its job; a surface that only reported successes would make the budget
    meaningless.
    """
    session = await _rehydrate(principal, session_id)
    report = await _service.report_first_audio(
        principal, session, measured_ms=body.measured_ms
    )
    return {
        **_state(session),
        "latency": {
            "measured_ms": report.measured_ms,
            "budget_ms": report.budget_ms,
            "within_budget": report.within_budget,
            "overrun_ms": report.overrun_ms,
            "description": report.describe(),
        },
    }


@router.post("/sessions/{session_id}/degrade")
async def degrade(
    session_id: str, body: DegradeBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    session = await _rehydrate(principal, session_id)
    await _service.degrade(principal, session, downlink_kbps=body.downlink_kbps)
    return {
        **_state(session),
        "ended": session.is_finished,
        "end_reason": session.end_reason.value if session.end_reason else None,
    }


@router.post("/sessions/{session_id}/interrupt")
async def interrupt(
    session_id: str, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Barge-in. Records that IRA was talked over; changes no connection state.

    The interruption itself is handled client-side by the existing speech-sync
    store, which stops playback in ~150ms. This endpoint exists so the
    telemetry can show how often it happens — not to perform the interruption,
    which must not wait for a network round trip.
    """
    session = await _rehydrate(principal, session_id)
    count = await _service.note_interruption(principal, session)
    return {**_state(session), "interrupt_count": count}


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str, body: EndBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Finish a session. An unrecognised reason is recorded as `unknown` rather
    than rejected — refusing to close a session because its reason code was
    unfamiliar would leave it open forever.

    P13-01 finding X found the defect: the fallback used to be `user_ended`, and
    that crosses the success/failure line. `Session.end` derives the STATE from
    the reason, so a client sending a mistyped or newer reason code had its
    session recorded as a clean user hang-up, durably, and the failure never
    appeared in any count of failed sessions.

    P13-01 made the substitution traceable and left the direction to the owner,
    because falling back to `error` would turn a future client's benign new code
    into a false failed session. **P14-01b takes the third answer the finding
    named**, under the owner's Phase 14 instruction:

        Do not record an unknown/failed termination as a clean hang-up. Use an
        explicit abnormal/unknown outcome.

    `EndReason.UNKNOWN` claims neither. It is abnormal — so the session is
    `FAILED`, never a clean `ENDED` — and it is distinguishable from a known
    failure, so a count of real outages can still exclude it. It needed
    migration `084` because `027_realtime.sql` constrains `end_reason` with a
    CHECK, which is why P13-01 could not simply do it.
    """
    session = await _rehydrate(principal, session_id)
    try:
        reason = EndReason(body.reason)
    except ValueError:
        logger.error(
            "realtime session %s ended with an UNRECOGNISED reason %r — "
            "recording it as %s, which is an ABNORMAL end, NOT a clean "
            "hang-up. It is also not counted as a known failure: if this is a "
            "newer client's vocabulary, add it to EndReason; if it is a "
            "mistyped code, this is the client bug.",
            session_id, body.reason, EndReason.UNKNOWN.value)
        reason = EndReason.UNKNOWN
    await _service.end_session(principal, session, reason)
    return {**_state(session), "end_reason": session.end_reason.value}
