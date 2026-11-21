"""
IRA Task Manager — full CRUD for tasks, reminders, and to-do management.

IRA can create, update, and complete tasks on behalf of the user.
All dangerous actions (delete, bulk-cancel) require explicit confirmation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from worker.reminders import create_reminder

Priority = Literal["low", "medium", "high", "urgent"]
Status = Literal["pending", "in_progress", "done", "cancelled"]


async def create_task(
    principal,
    title: str,
    *,
    description: str | None = None,
    priority: Priority = "medium",
    due_at: datetime | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
    remind_at: datetime | None = None,
) -> dict:
    """Create a task and optionally set a reminder. Returns the task dict."""
    task_id = str(uuid.uuid4())
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        await conn.execute(
            """INSERT INTO tasks (id, title, description, priority, due_at, tags,
                                  source, tenant_id, workspace_id, owner_user_id)
               VALUES ($1, $2, $3, $4, $5, $6::text[], $7,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid,
                       (SELECT id FROM users WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid))""",
            uuid.UUID(task_id), title, description, priority, due_at,
            tags or [], source,
        )

    if remind_at:
        await create_reminder(
            principal,
            title=f"Task due: {title}",
            remind_at=remind_at,
            task_id=task_id,
        )

    return await get_task(principal, task_id)


async def get_task(principal, task_id: str) -> dict | None:
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tasks WHERE id=$1", uuid.UUID(task_id)
        )
    if not row:
        return None
    return _row_to_dict(row)


async def list_tasks(
    principal,
    status: Status | None = None,
    priority: Priority | None = None,
    limit: int = 50,
) -> list[dict]:
    conditions = []
    params = []
    i = 1

    if status:
        conditions.append(f"status = ${i}")
        params.append(status)
        i += 1
    if priority:
        conditions.append(f"priority = ${i}")
        params.append(priority)
        i += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    from core.identity.db_context import workspace_connection


    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT * FROM tasks {where} ORDER BY due_at ASC NULLS LAST, priority DESC LIMIT ${i}",
            *params,
        )
    return [_row_to_dict(r) for r in rows]


async def update_task(principal, task_id: str, **kwargs) -> dict | None:
    """Update allowed task fields."""
    allowed = {"title", "description", "priority", "status", "due_at", "tags"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return await get_task(principal, task_id)

    # $1 is reserved for the WHERE id clause — SET params start at $2
    set_clause = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
    values = list(updates.values())
    if "status" in updates and updates["status"] == "done":
        set_clause += f", completed_at=NOW()"

    from core.identity.db_context import workspace_connection


    async with workspace_connection(principal) as conn:
        await conn.execute(
            f"UPDATE tasks SET {set_clause}, updated_at=NOW() WHERE id=$1",
            uuid.UUID(task_id), *values,
        )
    return await get_task(principal, task_id)


async def complete_task(principal, task_id: str) -> dict | None:
    return await update_task(principal, task_id, status="done")


async def get_overdue_tasks(principal) -> list[dict]:
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """SELECT * FROM tasks WHERE status IN ('pending','in_progress')
               AND due_at < NOW() ORDER BY due_at ASC"""
        )
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
