"""Tenant-scoped persistence for realtime sessions and their telemetry.

Every statement runs inside `tenant_connection`, so Row-Level Security binds
`app.tenant_id` to the authenticated Principal — the same arrangement as
`core/privacy/store.py`.

Writes here are FAIL-SOFT and that decision needs stating, because Phase 4 was
bitten by exactly this pattern: a fail-soft ledger write swallowed a bad `kind`
value for two phases and the audit trail was silently always empty.

The difference is what the write is FOR. A permission decision that is not
recorded destroys the evidence that an action was authorised. A latency
measurement that is not recorded loses a data point about a conversation that
is happening fine. Failing a live call because a telemetry INSERT timed out
would be the wrong trade — but the lesson from Phase 4 still applies, so:

  * every fail-soft path logs at WARNING with the exception, and
  * `tests/realtime/test_store_is_wired.py` asserts the writes actually reach
    a real database with the real schema, rather than trusting that a call
    which raises nothing did something.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.realtime.session import EventKind, RealtimeSession
from core.realtime.telemetry import sanitise_metrics

logger = logging.getLogger("ira.realtime.store")


async def create_session(principal: Principal, session: RealtimeSession) -> Optional[uuid.UUID]:
    """Insert the session row at REQUEST time, before any transport exists.

    Written this early so a session that never connects still has a row. "IRA
    tried to open a live session and could not" is information the user is
    entitled to, and a table that only records successes cannot express it.
    """
    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO realtime_sessions
                    (id, tenant_id, user_id, companion_id, provider,
                     provider_region, state, opened_level, current_level,
                     requested_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING id
                """,
                uuid.UUID(session.session_id), principal.tenant_id,
                principal.user_id,
                uuid.UUID(session.companion_id) if session.companion_id else None,
                session.provider, session.provider_region, session.state.value,
                session.opened_level.value, session.current_level.value,
                session.requested_at,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not create realtime session row: %s", exc)
        return None


async def update_session(principal: Principal, session: RealtimeSession) -> bool:
    """Persist the session's current state. Called on each transition.

    `audio_stored` and `transcript_stored` are absent on purpose — they are
    GENERATED ALWAYS columns and naming them here would fail. That is the point
    of making them generated.
    """
    try:
        async with tenant_connection(principal) as conn:
            result = await conn.execute(
                """
                UPDATE realtime_sessions SET
                    provider        = $3,
                    provider_region = $4,
                    state           = $5,
                    current_level   = $6,
                    connected_at    = $7,
                    ended_at        = $8,
                    first_audio_ms  = $9,
                    reconnect_count = $10,
                    downgrade_count = $11,
                    end_reason      = $12,
                    updated_at      = NOW()
                WHERE id = $1 AND tenant_id = $2
                """,
                uuid.UUID(session.session_id), principal.tenant_id,
                session.provider, session.provider_region, session.state.value,
                session.current_level.value, session.connected_at,
                session.ended_at, session.first_audio_ms,
                session.reconnect_count, session.downgrade_count,
                session.end_reason.value if session.end_reason else None,
            )
        return str(result).endswith("1")
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not update realtime session %s: %s",
                       session.session_id, exc)
        return False


async def record_events(principal: Principal, session: RealtimeSession) -> int:
    """Append the session's unflushed events.

    `sanitise_metrics` runs here as well as in the database. The redundancy is
    the point: this raise names the offending key at the boundary, and the
    CHECK constraint is what makes the guarantee unbypassable.
    """
    # The session's event list IS the unflushed buffer: a successful write
    # clears it, and a failed one leaves it for the next flush to retry. No
    # per-event "flushed" flag, because a flag on a frozen dataclass is a flag
    # nobody can set — which would silently mean "everything, every time".
    pending = list(session.events)
    if not pending:
        return 0

    rows = []
    for event in pending:
        rows.append((
            principal.tenant_id,
            uuid.UUID(session.session_id),
            event.kind.value,
            event.level.value if event.level else None,
            int(event.at_ms),
            json.dumps(sanitise_metrics(event.metrics)),
        ))

    try:
        async with tenant_connection(principal) as conn:
            await conn.executemany(
                """
                INSERT INTO realtime_events
                    (tenant_id, session_id, kind, level, at_ms, metrics)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                """,
                rows,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not record %d realtime event(s): %s", len(rows), exc)
        return 0

    # Drop exactly what was written, not the whole list: the INSERT awaited
    # above, and a live session can have appended new events in the meantime.
    # `clear()` here would discard measurements that were never stored.
    del session.events[:len(pending)]
    return len(rows)


async def recent_sessions(
    principal: Principal, *, limit: int = 20
) -> list[dict[str, Any]]:
    """The caller's recent live sessions, for the disclosure surface.

    `audio_stored` and `transcript_stored` are SELECTed rather than hardcoded
    to False in Python. The value shown to a user is then one the database
    guarantees, and if someone ever migrates those columns into something
    writable, this surface starts telling the truth about it immediately.
    """
    capped = max(1, min(int(limit), 100))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, provider_region, state, opened_level,
                   current_level, requested_at, connected_at, ended_at,
                   first_audio_ms, reconnect_count, downgrade_count,
                   end_reason, audio_stored, transcript_stored
            FROM realtime_sessions
            WHERE tenant_id = $1
            ORDER BY requested_at DESC
            LIMIT $2
            """,
            principal.tenant_id, capped,
        )
    return [
        {
            "session_id": str(r["id"]),
            "provider": r["provider"],
            "provider_region": r["provider_region"],
            "state": r["state"],
            "opened_level": r["opened_level"],
            "current_level": r["current_level"],
            "requested_at": r["requested_at"].isoformat() if r["requested_at"] else None,
            "connected_at": r["connected_at"].isoformat() if r["connected_at"] else None,
            "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            "first_audio_ms": r["first_audio_ms"],
            "reconnect_count": r["reconnect_count"],
            "downgrade_count": r["downgrade_count"],
            "end_reason": r["end_reason"],
            "audio_stored": r["audio_stored"],
            "transcript_stored": r["transcript_stored"],
        }
        for r in rows
    ]


async def session_events(
    principal: Principal, session_id: uuid.UUID, *, limit: int = 200
) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit), 1000))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT kind, level, at_ms, metrics, at FROM realtime_events "
            "WHERE tenant_id = $1 AND session_id = $2 ORDER BY at_ms LIMIT $3",
            principal.tenant_id, session_id, capped,
        )
    return [
        {
            "kind": r["kind"],
            "level": r["level"],
            "at_ms": r["at_ms"],
            "metrics": r["metrics"] if isinstance(r["metrics"], dict)
                       else json.loads(r["metrics"] or "{}"),
            "at": r["at"].isoformat() if r["at"] else None,
        }
        for r in rows
    ]


async def capability_ladder(principal: Principal) -> list[dict[str, Any]]:
    """The ladder as the database holds it. Global reference data, no RLS."""
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT level, rung, renders_avatar, produces_audio, "
            "target_first_audio_ms, min_downlink_kbps "
            "FROM realtime_capability_profiles ORDER BY rung"
        )
    return [dict(r) for r in rows]


__all__ = [
    "capability_ladder",
    "create_session",
    "record_events",
    "recent_sessions",
    "session_events",
    "update_session",
]
