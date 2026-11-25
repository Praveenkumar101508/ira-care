"""Search across the people the caller may see (D3.g, D-080).

One box on the dashboard: people by name, visits by purpose or carer, and alerts by kind.
It never searches what anybody said (check-in answers, transcripts, care-plan text): those
stay behind the per-person history search, which applies consent. People outside the
caller's circles never appear (authz.visible_people, D-076), every query runs on a
workspace-bound connection (row-level security), and the text typed is matched literally:
`%`, `_` and `\\` are escaped, and it is passed as a parameter, never into the SQL.

Each search is written to the access log, once per person it returned (or once, with no
person, when it found nothing), recording that a search happened and what kinds came
back, never the words typed.
"""
from __future__ import annotations

import uuid

from core.identity.principal import Principal

from . import audit, authz

MIN_QUERY = 2
MAX_QUERY = 80
MAX_RESULTS = 50


def like_pattern(query: str) -> str:
    """`%query%` with the LIKE wildcards in the query itself escaped, for ESCAPE '\\'."""
    q = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


async def search(conn, principal: Principal, query: str, *, limit: int = MAX_RESULTS) -> dict:
    q = " ".join(query.split())
    if len(q) < MIN_QUERY:
        return {"query": q, "results": []}
    q = q[:MAX_QUERY]
    limit = max(1, min(int(limit), MAX_RESULTS))
    pattern = like_pattern(q)
    visible = await authz.visible_people(conn, principal)
    ids = None if visible is None else list(visible)

    results: list[dict] = []
    for r in await conn.fetch(
            """
            SELECT id, coalesce(nullif(preferred_name, ''), display_name) AS name
              FROM care_people
             WHERE status = 'active'
               AND (display_name ILIKE $1 ESCAPE '\\' OR preferred_name ILIKE $1 ESCAPE '\\')
               AND ($2::uuid[] IS NULL OR id = ANY($2::uuid[]))
             ORDER BY name LIMIT $3
            """, pattern, ids, limit):
        results.append({"kind": "person", "id": str(r["id"]), "person_id": str(r["id"]), "person_name": r["name"],
                        "text": r["name"], "at": None})
    for r in await conn.fetch(
            """
            SELECT v.id, v.person_id, v.planned_start, v.purpose, v.carer_name, v.status,
                   coalesce(nullif(pp.preferred_name, ''), pp.display_name) AS name
              FROM care_visits v JOIN care_people pp ON pp.id = v.person_id
             WHERE (v.purpose ILIKE $1 ESCAPE '\\' OR v.carer_name ILIKE $1 ESCAPE '\\')
               AND ($2::uuid[] IS NULL OR v.person_id = ANY($2::uuid[]))
             ORDER BY v.planned_start DESC LIMIT $3
            """, pattern, ids, limit):
        results.append({"kind": "visit", "id": str(r["id"]), "person_id": str(r["person_id"]), "person_name": r["name"],
                        "text": f"{r['purpose'] or 'Visit'} · {r['carer_name'] or 'no carer named'} · {r['status']}",
                        "at": r["planned_start"].isoformat()})
    for r in await conn.fetch(
            """
            SELECT a.id, a.person_id, a.opened_at, a.kind, a.severity, a.status,
                   coalesce(nullif(pp.preferred_name, ''), pp.display_name) AS name
              FROM care_alerts a JOIN care_people pp ON pp.id = a.person_id
             WHERE (replace(a.kind, '_', ' ') ILIKE $1 ESCAPE '\\' OR a.severity ILIKE $1 ESCAPE '\\')
               AND ($2::uuid[] IS NULL OR a.person_id = ANY($2::uuid[]))
             ORDER BY a.opened_at DESC LIMIT $3
            """, pattern, ids, limit):
        results.append({"kind": "alert", "id": str(r["id"]), "person_id": str(r["person_id"]), "person_name": r["name"],
                        "text": f"{r['kind'].replace('_', ' ')} · {r['severity']} · {r['status']}",
                        "at": r["opened_at"].isoformat()})
    results = results[:limit]

    per_person: dict[str, set] = {}
    for r in results:
        per_person.setdefault(r["person_id"], set()).add(r["kind"])
    if per_person:
        for pid, kinds in per_person.items():
            await audit.record_access(conn, principal, person_id=uuid.UUID(pid), resource_kind="person",
                                      action="search", detail={"scope": "workspace", "kinds": sorted(kinds)})
    else:
        await audit.record_access(conn, principal, person_id=None, resource_kind="person", action="search",
                                  detail={"scope": "workspace", "results": 0})
    return {"query": q, "results": results}
