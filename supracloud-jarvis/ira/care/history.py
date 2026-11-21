"""Searchable history: one query across a person's check-in summaries, alerts
and (only under consent) transcripts. Every search is written to the access
log with the words searched for, because a search is a read of health data.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from core.identity.principal import Principal

from . import audit, consent, repository as repo


async def search(conn, principal: Principal, person_id: uuid.UUID, query: str, *,
                 since: Optional[dt.datetime], until: Optional[dt.datetime], limit: int) -> dict:
    q = query.strip()
    if not q:
        return {"query": q, "results": [], "transcripts_included": False}
    pattern = f"%{q}%"
    results: list[dict] = []
    for row in await conn.fetch(
            """
            SELECT id, scheduled_at AS at, question_kind, status, answer_summary FROM care_checkins
             WHERE person_id = $1 AND (answer_summary ILIKE $2 OR question_kind ILIKE $2 OR status ILIKE $2)
               AND ($3::timestamptz IS NULL OR scheduled_at >= $3) AND ($4::timestamptz IS NULL OR scheduled_at <= $4)
             ORDER BY scheduled_at DESC LIMIT $5
            """, person_id, pattern, since, until, limit):
        results.append({"kind": "checkin", "id": row["id"], "at": row["at"],
                        "text": f"{row['question_kind']} · {row['status']} · {row['answer_summary']}"})
    for row in await conn.fetch(
            """
            SELECT id, opened_at AS at, kind, severity, status, details FROM care_alerts
             WHERE person_id = $1 AND (kind ILIKE $2 OR severity ILIKE $2 OR status ILIKE $2 OR details::text ILIKE $2)
               AND ($3::timestamptz IS NULL OR opened_at >= $3) AND ($4::timestamptz IS NULL OR opened_at <= $4)
             ORDER BY opened_at DESC LIMIT $5
            """, person_id, pattern, since, until, limit):
        results.append({"kind": "alert", "id": row["id"], "at": row["at"],
                        "text": f"{row['kind'].replace('_', ' ')} · {row['severity']} · {row['status']}"})
    role = await repo.circle_role_of(conn, person_id, principal.user_id)
    if role is None and principal.is_owner:
        role = "carer"
    consents = await repo.fetch_consents(conn, person_id)
    with_transcripts = consent.may_read_transcripts(role, consents)
    if with_transcripts:
        for row in await conn.fetch(
                """
                SELECT id, created_at AS at, speaker, text FROM care_transcripts
                 WHERE person_id = $1 AND text ILIKE $2
                   AND ($3::timestamptz IS NULL OR created_at >= $3) AND ($4::timestamptz IS NULL OR created_at <= $4)
                 ORDER BY created_at DESC LIMIT $5
                """, person_id, pattern, since, until, limit):
            results.append({"kind": "transcript", "id": row["id"], "at": row["at"],
                            "text": f"{row['speaker']}: {row['text']}"})
    results.sort(key=lambda r: r["at"], reverse=True)
    await audit.record_access(conn, principal, person_id=person_id, resource_kind="report", action="search",
                              detail={"query": q[:200], "results": len(results), "transcripts_included": with_transcripts})
    return {"query": q, "results": results[:limit], "transcripts_included": with_transcripts}
