"""Every look at health data or a transcript is written down.

`care_access_log` is append-only (migration 099). Rows name the actor, the
person, what kind of thing was read and how, and never the content itself.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from core.identity.principal import Principal

RESOURCE_KINDS = frozenset({
    "vitals", "transcript", "checkins", "alerts", "medication", "report", "person", "consent",
})


async def record_access(
    conn,
    principal: Principal,
    *,
    person_id: Optional[uuid.UUID],
    resource_kind: str,
    resource_id: Optional[uuid.UUID] = None,
    action: str = "view",
    detail: Optional[dict] = None,
) -> uuid.UUID:
    if resource_kind not in RESOURCE_KINDS:
        raise ValueError(f"unknown resource kind {resource_kind!r}")
    label = principal.workspace_role or ("owner" if principal.is_owner else "")
    return await conn.fetchval(
        """
        INSERT INTO care_access_log
            (actor_user_id, actor_label, person_id, resource_kind, resource_id, action, detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        RETURNING id
        """,
        principal.user_id, label[:80], person_id, resource_kind, resource_id, action,
        json.dumps(detail or {}),
    )
