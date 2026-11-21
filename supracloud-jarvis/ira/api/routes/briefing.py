"""Briefing endpoints — trigger and retrieve IRA's briefings.

── P12-03 finding G ────────────────────────────────────────────────────────

These three routes read `briefings` on a bare `acquire()` behind `require_auth`
alone, with no predicate — `ORDER BY created_at DESC LIMIT 1`. The table had no
tenancy columns and no row-level security, so the most recent briefing
generated ANYWHERE in the deployment was returned to any authenticated user,
`content` and all.

A briefing is not incidental data: `worker/briefing.py` assembles it from four
workspace-scoped tables through correctly bound connections, and it is a
deliberate summary of one workspace's revenue, security posture, tasks and
calendar. The gather was right and the storage and the read were wrong.

`081` gave the table `tenant_id` and `workspace_id`, both NOT NULL, with the
strict `049` predicate. These routes now take `require_workspace_principal` and
`workspace_connection`, so the boundary is enforced by the database and the
route agrees with it rather than substituting for it.

The workspace dependency is the right one here even though it makes the routes
refuse when no workspace is named: a briefing cannot be generated without a
workspace to read, so there is no such thing as a tenant-wide briefing to serve
in that case. An explicit refusal is the honest answer, and `[]` would not be —
under the new policy an unbound read returns nothing at all, which is
indistinguishable from "you have no briefings yet".
"""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, Depends, Query

from api.middleware.workspace import require_workspace_principal
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

router = APIRouter(prefix="/briefing", tags=["briefing"])

# Fix #91: keep strong references to background briefing tasks so Python's
# garbage collector cannot collect them before they finish.  The asyncio
# event loop only keeps a *weak* reference to tasks; without a live strong
# reference the task can be silently dropped mid-execution.
_background_tasks: set[asyncio.Task] = set()


@router.post("/now")
async def trigger_briefing(
    # Fix #90: Pydantic v2 renamed regex= to pattern= (regex= was deprecated in v1)
    briefing_type: str = Query("morning", pattern="^(morning|evening|security|business)$"),
    principal: Principal = Depends(require_workspace_principal),
):
    """Trigger an immediate briefing. Runs asynchronously and delivers via all channels."""
    import logging as _log
    from worker.briefing import generate_briefing

    # The CALLER's verified principal, not the scheduler's.
    #
    # This passed no principal, so `generate_briefing` fell through to
    # `default_workspace_job_principal(PRIVATE_TENANT_ID)` — the path meant for
    # unattended scheduled work, which has no request and nobody to ask. On a
    # request there is somebody to ask, and using the scheduler's identity here
    # meant a cloud user pressing "brief me now" generated a briefing of the
    # PRIVATE tenant's default workspace, from data that is not theirs, and
    # (before 081) could then read it back.
    #
    # Passing the request's own verified principal makes the generated row land
    # in the workspace the caller actually selected, which is also the only
    # workspace 081's policy will now accept the INSERT into.
    _t = asyncio.create_task(generate_briefing(briefing_type, principal=principal))
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)
    _t.add_done_callback(
        lambda t: t.exception() and _log.getLogger("ira.briefing").warning(
            f"Briefing task failed: {t.exception()}"
        )
    )
    return {"status": "generating", "message": "IRA is preparing your briefing now, Sir."}


@router.get("/latest")
async def get_latest(
    briefing_type: str = Query("morning"),
    principal: Principal = Depends(require_workspace_principal),
):
    """Retrieve the most recent briefing of a given type, in this workspace."""
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            """SELECT id, briefing_type, content, summary, created_at
               FROM briefings WHERE briefing_type=$1
               ORDER BY created_at DESC LIMIT 1""",
            briefing_type,
        )
    if not row:
        return {"message": "No briefings found yet. Trigger one with POST /briefing/now"}
    return {
        "id": str(row["id"]),
        "type": row["briefing_type"],
        "content": row["content"],
        "summary": row["summary"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/history")
async def briefing_history(
    limit: int = Query(10, le=50),
    principal: Principal = Depends(require_workspace_principal),
):
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """SELECT id, briefing_type, summary, created_at FROM briefings
               ORDER BY created_at DESC LIMIT $1""",
            limit,
        )
    return [
        {
            "id": str(r["id"]),
            "type": r["briefing_type"],
            "summary": r["summary"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
