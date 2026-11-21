"""
memory/temporal.py — temporal + provenance-aware memory operations (PR 71).

Extends the pgvector memory (memory_embeddings + migration 017) with:
  * typed memories (13 types, brief §6) carrying source, confidence,
    sensitivity, validity windows and owner-confirmed flags;
  * supersession: an old memory is never destroyed — it is linked to its
    replacement (superseded_by) and excluded from active retrieval;
  * soft deletion (deleted_at) — the confirmation-gated Memory Vault flow
    stays the only way anything gets there;
  * contradiction detection: deterministic first pass (similarity +
    normalized-content difference + conflicting numbers/dates). IRA asks
    the owner instead of silently overwriting (constitution: "Do not
    silently overwrite contradictory memories").

Design constraints (house style, mirrors memory/life_graph.py):
  * same DB, `utils.db.acquire`, parameterized SQL only (D2);
  * embeddings via the ONE existing BGE function, lazy-imported;
  * fail-soft reads (log + empty), fail-LOUD writes (callers must know a
    save failed — memory loss must never be silent);
  * every user-scoped statement filters by user_id — cross-user isolation
    is enforced in SQL, not in the caller.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


logger = logging.getLogger("ira.memory.temporal")

MEMORY_TYPES = frozenset({
    "fact", "preference", "temporary_state", "goal", "instruction",
    "hypothesis", "historical_fact", "superseded_fact", "commitment",
    "decision", "relationship", "resource", "event",
})
SENSITIVITIES = frozenset({"public", "shared", "private", "secret"})

#: SQL fragment selecting only live memories. Kept as ONE constant so every
#: retrieval path shares the same definition of "active".
ACTIVE_FILTER_SQL = (
    "deleted_at IS NULL AND superseded_by IS NULL "
    "AND (valid_until IS NULL OR valid_until > NOW())"
)

EMBEDDING_VERSION = "bge-large-en-v1.5"


async def _embed(text: str) -> list[float]:
    from memory.embeddings import embed_one  # lazy: loads in no-numpy test env
    return await embed_one(text)


# ── Writes ────────────────────────────────────────────────────────────────────

async def save_memory(
    principal,
    content: str,
    *,
    user_id: str,
    memory_type: str = "fact",
    source: str = "owner_chat",
    sensitivity: str = "private",
    confidence: float = 1.0,
    owner_confirmed: bool = False,
    observed_at: Optional[Any] = None,
    valid_from: Optional[Any] = None,
    valid_until: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Insert a typed memory with provenance; returns the new memory id.
    Raises on invalid input or DB failure — a lost write must be loud."""
    if not content.strip():
        raise ValueError("memory content must not be empty")
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"unknown memory_type: {memory_type!r}")
    if sensitivity not in SENSITIVITIES:
        raise ValueError(f"unknown sensitivity: {sensitivity!r}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    if not user_id:
        raise ValueError("user_id is required")

    emb = await _embed(content)
    mem_id = str(uuid.uuid4())
    import json as _json
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        await conn.execute(
            """
            INSERT INTO memory_embeddings
                (id, source_type, content, embedding, user_id, metadata,
                 memory_type, source, sensitivity, confidence, owner_confirmed,
                 observed_at, valid_from, valid_until, embedding_version,
                 tenant_id, workspace_id)
            VALUES ($1, 'fact', $2, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11, $12, $13, $14,
                    NULLIF(current_setting('app.tenant_id', true), '')::uuid, NULLIF(current_setting('app.workspace_id', true), '')::uuid)
            """,
            uuid.UUID(mem_id), content, str(emb), user_id,
            _json.dumps(metadata or {}),
            memory_type, source, sensitivity, confidence, owner_confirmed,
            observed_at, valid_from, valid_until, EMBEDDING_VERSION,
        )
    logger.info("temporal memory saved: %s type=%s user=%s", mem_id, memory_type, user_id)
    return mem_id


async def supersede(principal, old_id: str, new_id: str, *, user_id: str,
                    reason: str = "") -> bool:
    """Mark `old_id` as superseded by `new_id`. The old row is preserved with
    full provenance (never deleted). Both rows must belong to `user_id`.
    Returns False when the old row wasn't found for this user (fail-closed:
    you cannot supersede another user's memory)."""
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        # confirm the replacement belongs to the same user first
        owner = await conn.fetchrow(
            "SELECT user_id FROM memory_embeddings WHERE id = $1",
            uuid.UUID(new_id))
        if owner is None or owner["user_id"] != user_id:
            logger.warning("supersede refused: new id %s not owned by %s", new_id, user_id)
            return False
        result = await conn.execute(
            """
            UPDATE memory_embeddings
               SET superseded_by = $1,
                   memory_type = CASE WHEN memory_type = 'fact'
                                      THEN 'superseded_fact' ELSE memory_type END,
                   metadata = metadata || jsonb_build_object('superseded_reason', $4::text)
             WHERE id = $2 AND user_id = $3 AND superseded_by IS NULL
            """,
            uuid.UUID(new_id), uuid.UUID(old_id), user_id, reason,
        )
    updated = result.endswith("1")
    if not updated:
        logger.warning("supersede matched no active row: old=%s user=%s", old_id, user_id)
    return updated


async def soft_forget(principal, memory_id: str, *, user_id: str) -> bool:
    """Soft-delete (sets deleted_at). Confirmation gating stays with the
    caller (Memory Vault's token flow). Scoped to user_id in SQL."""
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE memory_embeddings SET deleted_at = NOW() "
            "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            uuid.UUID(memory_id), user_id,
        )
    return result.endswith("1")


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get_memory(principal, memory_id: str, *, user_id: str) -> Optional[dict[str, Any]]:
    """Fetch one memory WITH provenance; user-scoped. Fail-soft: None."""
    try:
        from core.identity.db_context import workspace_connection

        async with workspace_connection(principal) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, content, memory_type, source, sensitivity, confidence,
                       owner_confirmed, observed_at, valid_from, valid_until,
                       superseded_by, deleted_at, created_at, embedding_version, metadata
                  FROM memory_embeddings
                 WHERE id = $1 AND user_id = $2
                """,
                uuid.UUID(memory_id), user_id)
        return dict(row) if row else None
    except Exception as exc:
        logger.error("get_memory failed: %s", exc)
        return None


async def retrieve_active(principal, query: str, *, user_id: str, top_k: int = 5,
                          include_sensitivities: tuple[str, ...] = ("public", "shared", "private"),
                          raise_errors: bool = False,
                          ) -> list[dict[str, Any]]:
    """Vector retrieval over ACTIVE memories only (not deleted, not
    superseded, not expired), user-scoped, sensitivity-filtered. 'secret'
    is excluded unless explicitly requested. Fail-soft: [] — unless
    `raise_errors`, for a caller that must tell "nothing found" from "could
    not look"."""
    bad = set(include_sensitivities) - SENSITIVITIES
    if bad:
        raise ValueError(f"unknown sensitivities: {sorted(bad)}")
    try:
        emb = await _embed(query)
        from core.identity.db_context import workspace_connection

        # Filter-first exact search, never ORDER BY on the table itself: the
        # HNSW index spans every tenant and namespace. See memory/store.py,
        # above `retrieve()`.
        async with workspace_connection(principal) as conn:
            rows = await conn.fetch(
                f"""
                WITH scoped AS MATERIALIZED (
                    SELECT id, embedding <=> $1 AS distance
                      FROM memory_embeddings
                     WHERE user_id = $2
                       AND sensitivity = ANY($3::text[])
                       AND {ACTIVE_FILTER_SQL}
                       AND embedding IS NOT NULL
                ),
                nearest AS (
                    SELECT id, distance FROM scoped ORDER BY distance LIMIT $4
                )
                SELECT m.id, m.content, m.memory_type, m.source, m.sensitivity,
                       m.confidence, m.observed_at, m.valid_from, m.valid_until,
                       m.created_at, 1 - n.distance AS similarity
                  FROM nearest n JOIN memory_embeddings m ON m.id = n.id
                 ORDER BY n.distance
                """,
                str(emb), user_id, list(include_sensitivities), top_k,
            )
        return [dict(r) for r in rows]
    except Exception as exc:
        if raise_errors:
            raise
        logger.error("retrieve_active failed: %s", exc)
        return []


# ── Contradiction detection (deterministic first pass) ───────────────────────

_NUM_DATE = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}|\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)?)\b",
    re.I)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokens_of_interest(text: str) -> set[str]:
    return {m.lower().replace(" ", "") for m in _NUM_DATE.findall(text)}


@dataclass
class Contradiction:
    existing_id: str
    existing_content: str
    new_content: str
    similarity: float
    conflicting_values: list[str] = field(default_factory=list)

    def question(self) -> str:
        """The owner-facing prompt. IRA asks; it never silently overwrites."""
        detail = (f" The specific values conflict: {', '.join(self.conflicting_values)}."
                  if self.conflicting_values else "")
        return (
            f"You previously stated: “{self.existing_content}”, but the new "
            f"information says: “{self.new_content}”. The sources conflict.{detail} "
            "Should the new information replace the old memory, or should both be "
            "retained with separate provenance?"
        )


def find_contradictions(new_content: str,
                        similar_memories: list[dict[str, Any]],
                        *, similarity_threshold: float = 0.86,
                        ) -> list[Contradiction]:
    """Deterministic screen over already-retrieved similar memories.

    Flags a candidate when it is (a) semantically close (similarity ≥
    threshold) AND (b) not the same statement, with extra weight when
    numbers/dates/times differ (the '18 July vs 22 July' case: same topic,
    different value). Semantic-only near-duplicates with no differing values
    still surface — better to ask than to overwrite."""
    new_norm = _normalize(new_content)
    new_vals = _tokens_of_interest(new_content)
    out: list[Contradiction] = []
    for mem in similar_memories:
        sim = float(mem.get("similarity", 0.0))
        if sim < similarity_threshold:
            continue
        content = str(mem.get("content", ""))
        if _normalize(content) == new_norm:
            continue  # identical statement, nothing to reconcile
        old_vals = _tokens_of_interest(content)
        conflicting = sorted((old_vals ^ new_vals)) if (old_vals or new_vals) else []
        out.append(Contradiction(
            existing_id=str(mem.get("id", "")),
            existing_content=content,
            new_content=new_content,
            similarity=sim,
            conflicting_values=conflicting,
        ))
    return out


async def check_before_save(principal, content: str, *, user_id: str,
                            memory_type: str = "fact",
                            top_k: int = 5) -> list[Contradiction]:
    """Retrieval + contradiction screen for the save path. Returns [] when
    saving is safe; a non-empty list means the caller must ask the owner
    (resolutions: 'replace' → save then supersede(); 'retain_both' → save).

    Reads as `principal`, the caller's own, so only memories in the caller's
    tenant and workspace, under `user_id`, can count as a contradiction.

    Until 1 November 2026 the principal was not passed: the content arrived in
    its place, every call raised TypeError, `contradiction_guard` reported the
    check unavailable and the save went ahead unchecked (P1-04). A failed read
    still raises, deliberately: `guarded_save` then reports
    `contradiction_checked=False`, where an empty list would claim the check
    ran and found nothing."""
    similar = await retrieve_active(principal, content, user_id=user_id, top_k=top_k,
                                    include_sensitivities=("public", "shared", "private", "secret"),
                                    raise_errors=True)
    same_type = [m for m in similar if m.get("memory_type") == memory_type]
    return find_contradictions(content, same_type)


__all__ = [
    "MEMORY_TYPES", "SENSITIVITIES", "ACTIVE_FILTER_SQL", "EMBEDDING_VERSION",
    "save_memory", "supersede", "soft_forget", "get_memory", "retrieve_active",
    "Contradiction", "find_contradictions", "check_before_save",
]
