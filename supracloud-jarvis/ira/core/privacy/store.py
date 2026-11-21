"""Tenant-scoped persistence for the Privacy Centre.

Statements run inside `workspace_connection` where the table is
workspace-scoped and `tenant_connection` where it is not, so Row-Level Security binds
`app.tenant_id` to the authenticated Principal. A Privacy Centre that could
read across tenants would be the worst possible place for that bug: it is the
screen that lists, for each user, the things they were most anxious about.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from core.identity.db_context import tenant_connection, workspace_connection
from core.identity.principal import Principal
from core.privacy.memory import MemoryRecord, RetentionSettings

logger = logging.getLogger("ira.privacy.store")

_MEMORY_COLUMNS = (
    "id, content, source_type, source_reference, storage_reason, "
    "sensitivity_category, confidence, created_at, expires_at, last_used_at, allowed_connectors, "
    "external_provider_policy, review_state, deleted_at"
)


def _row_to_memory(row: Any) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["id"],
        content=row["content"],
        source_type=row["source_type"] or "conversation",
        source_reference=row["source_reference"] or "",
        storage_reason=row["storage_reason"] or "",
        sensitivity=row["sensitivity_category"],
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        allowed_connectors=tuple(row["allowed_connectors"] or ()),
        external_provider_policy=row["external_provider_policy"],
        review_state=row["review_state"],
        deleted_at=row["deleted_at"],
    )


# ── Retention settings ───────────────────────────────────────────────────────


async def load_settings(principal: Principal) -> RetentionSettings:
    """The caller's retention settings, or the cautious defaults.

    A user with no row gets `RetentionSettings()` — which asks before storing
    and discards voice audio. Defaulting to anything more permissive would mean
    a user who has never opened this page is treated as having consented to
    something.
    """
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT memory_enabled, memory_mode, conversation_retention_days, "
            "memory_retention_days, voice_retention_hours, disabled_categories "
            "FROM retention_settings WHERE user_id = $1",
            principal.user_id,
        )
    if row is None:
        return RetentionSettings()
    return RetentionSettings(
        memory_enabled=row["memory_enabled"],
        memory_mode=row["memory_mode"],
        conversation_retention_days=row["conversation_retention_days"],
        memory_retention_days=row["memory_retention_days"],
        voice_retention_hours=row["voice_retention_hours"],
        disabled_categories=frozenset(row["disabled_categories"] or ()),
    )


async def save_settings(principal: Principal, settings: RetentionSettings) -> None:
    async with tenant_connection(principal) as conn:
        await conn.execute(
            """
            INSERT INTO retention_settings
                (tenant_id, user_id, memory_enabled, memory_mode,
                 conversation_retention_days, memory_retention_days,
                 voice_retention_hours, disabled_categories)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (tenant_id, user_id) DO UPDATE SET
                memory_enabled = EXCLUDED.memory_enabled,
                memory_mode = EXCLUDED.memory_mode,
                conversation_retention_days = EXCLUDED.conversation_retention_days,
                memory_retention_days = EXCLUDED.memory_retention_days,
                voice_retention_hours = EXCLUDED.voice_retention_hours,
                disabled_categories = EXCLUDED.disabled_categories,
                updated_at = NOW()
            """,
            principal.tenant_id, principal.user_id, settings.memory_enabled,
            settings.memory_mode, settings.conversation_retention_days,
            settings.memory_retention_days, settings.voice_retention_hours,
            list(settings.disabled_categories),
        )


# ── Memories ─────────────────────────────────────────────────────────────────


async def _personal(conn, principal: Principal) -> list[str]:
    """The namespaces this person's memory actions may reach.

    Every function below narrows by these as well as by tenant: the workspace
    policy on `memory_embeddings` has no user clause, so without them one
    person listed, edited, approved and deleted everyone's memories in the
    workspace (`core.privacy.retention.personal_memory_namespaces`).
    """
    from core.privacy.retention import personal_memory_namespaces

    return await personal_memory_namespaces(conn, principal)


async def list_memories(
    principal: Principal, *, include_deleted: bool = False, limit: int = 200
) -> list[MemoryRecord]:
    """The caller's memories, and nobody else's.

    Deleted rows are excluded by default but can be included, because during the
    grace period a user is entitled to see what was removed and when — "it's
    gone" is less reassuring than being shown that it is.
    """
    capped = max(1, min(int(limit), 500))
    clause = "" if include_deleted else "AND deleted_at IS NULL"
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT {_MEMORY_COLUMNS} FROM memory_embeddings "
            f"WHERE tenant_id = $1 AND user_id = ANY($3::text[]) {clause} "
            f"ORDER BY created_at DESC LIMIT $2",
            principal.tenant_id, capped, await _personal(conn, principal),
        )
    return [_row_to_memory(r) for r in rows]


async def pending_memories(principal: Principal) -> list[MemoryRecord]:
    """Memories awaiting the user's approval under memory_mode='ask'."""
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT {_MEMORY_COLUMNS} FROM memory_embeddings "
            f"WHERE tenant_id = $1 AND user_id = ANY($2::text[]) "
            f"AND review_state = 'pending' "
            f"AND deleted_at IS NULL ORDER BY created_at DESC",
            principal.tenant_id, await _personal(conn, principal),
        )
    return [_row_to_memory(r) for r in rows]


async def set_review_state(
    principal: Principal, memory_id: uuid.UUID, state: str
) -> bool:
    """Approve or reject one memory. Rejection also marks it deleted."""
    deleted = "NOW()" if state == "rejected" else "deleted_at"
    async with workspace_connection(principal) as conn:
        result = await conn.execute(
            f"UPDATE memory_embeddings SET review_state = $3, deleted_at = {deleted} "
            f"WHERE id = $1 AND tenant_id = $2 AND user_id = ANY($4::text[])",
            memory_id, principal.tenant_id, state, await _personal(conn, principal),
        )
    return str(result).endswith("1")


async def update_memory(
    principal: Principal,
    memory_id: uuid.UUID,
    *,
    content: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    clear_expiry: bool = False,
    sensitivity: Optional[str] = None,
    allowed_connectors: Optional[list[str]] = None,
    external_provider_policy: Optional[str] = None,
) -> bool:
    """Edit one memory. Only the fields supplied are changed.

    `clear_expiry` exists because `expires_at=None` is ambiguous over the wire:
    it could mean "leave it alone" or "remove the expiry", and guessing wrong
    silently changes how long IRA keeps something.
    """
    sets: list[str] = []
    args: list[Any] = [memory_id, principal.tenant_id]

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        sets.append(f"{fragment} = ${len(args)}")

    if content is not None:
        add("content", content)
    if clear_expiry:
        sets.append("expires_at = NULL")
    elif expires_at is not None:
        add("expires_at", expires_at)
    if sensitivity is not None:
        add("sensitivity_category", sensitivity)
    if allowed_connectors is not None:
        add("allowed_connectors", allowed_connectors)
    if external_provider_policy is not None:
        add("external_provider_policy", external_provider_policy)

    if not sets:
        return False

    async with workspace_connection(principal) as conn:
        args.append(await _personal(conn, principal))
        result = await conn.execute(
            f"UPDATE memory_embeddings SET {', '.join(sets)} "
            f"WHERE id = $1 AND tenant_id = $2 AND user_id = ANY(${len(args)}::text[])",
            *args,
        )
    return str(result).endswith("1")


async def delete_memory(principal: Principal, memory_id: uuid.UUID) -> bool:
    """Soft-delete one memory. The hard delete happens during erasure."""
    async with workspace_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE memory_embeddings SET deleted_at = NOW() "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = ANY($3::text[]) "
            "AND deleted_at IS NULL",
            memory_id, principal.tenant_id, await _personal(conn, principal),
        )
    return str(result).endswith("1")


async def expire_due_memories(principal: Principal) -> int:
    """Soft-delete memories whose expiry has passed.

    An expiry the user set is a promise. A memory that is merely *filtered* on
    read but still sitting in the table is one an unrelated query will find.
    """
    async with workspace_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE memory_embeddings SET deleted_at = NOW() "
            "WHERE tenant_id = $1 AND deleted_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at <= NOW()",
            principal.tenant_id,
        )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover
        return 0


# ── Requests ─────────────────────────────────────────────────────────────────


async def record_deletion(principal: Principal, outcome) -> Optional[uuid.UUID]:
    """Persist an erasure outcome, including what it could NOT do."""
    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO deletion_requests
                    (tenant_id, user_id, scope, status, completed_at,
                     store_results, unredactable_records, backup_residue_until)
                VALUES ($1,$2,$3,$4,NOW(),$5::jsonb,$6,$7)
                RETURNING id
                """,
                principal.tenant_id, principal.user_id, outcome.scope,
                outcome.status,
                json.dumps([r.public_dict() for r in outcome.results]),
                outcome.unerasable_records, outcome.backup_residue_until,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, reported
        logger.warning("could not record deletion request: %s", exc)
        return None


async def recent_deletions(principal: Principal, *, limit: int = 20) -> list[dict]:
    capped = max(1, min(int(limit), 100))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT scope, status, requested_at, completed_at, store_results, "
            "unredactable_records, backup_residue_until FROM deletion_requests "
            "WHERE tenant_id = $1 ORDER BY requested_at DESC LIMIT $2",
            principal.tenant_id, capped,
        )
    return [
        {
            "scope": r["scope"],
            "status": r["status"],
            "requested_at": r["requested_at"].isoformat() if r["requested_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "stores": r["store_results"] if isinstance(r["store_results"], list)
                      else json.loads(r["store_results"] or "[]"),
            "unredactable_records": r["unredactable_records"],
            "backup_residue_until": (
                r["backup_residue_until"].isoformat() if r["backup_residue_until"] else None
            ),
        }
        for r in rows
    ]


async def record_privacy_event(
    principal: Principal, kind: str, detail: dict[str, Any]
) -> None:
    """Append to `privacy_events` — the timeline the Privacy Centre shows.

    Fail-soft: a missing timeline entry must not turn a successful privacy
    action into an error the user sees.
    """
    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                "INSERT INTO privacy_events (tenant_id, user_id, kind, detail) "
                "VALUES ($1,$2,$3,$4::jsonb)",
                principal.tenant_id, principal.user_id, kind, json.dumps(detail),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record privacy event %s: %s", kind, exc)


async def recent_privacy_events(principal: Principal, *, limit: int = 50) -> list[dict]:
    capped = max(1, min(int(limit), 200))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT kind, detail, at FROM privacy_events WHERE tenant_id = $1 "
            "ORDER BY at DESC LIMIT $2",
            principal.tenant_id, capped,
        )
    return [
        {
            "kind": r["kind"],
            "detail": r["detail"] if isinstance(r["detail"], dict)
                      else json.loads(r["detail"] or "{}"),
            "at": r["at"].isoformat() if r["at"] else None,
        }
        for r in rows
    ]


__all__ = [
    "delete_memory",
    "expire_due_memories",
    "list_memories",
    "load_settings",
    "pending_memories",
    "recent_deletions",
    "recent_privacy_events",
    "record_deletion",
    "record_privacy_event",
    "save_settings",
    "set_review_state",
    "update_memory",
]
