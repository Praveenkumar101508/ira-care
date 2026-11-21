"""
Memory store — persists conversations and retrieves relevant memories via pgvector.

Two operations drive IRA's memory:
  store_message()  — saves each exchange and its embedding asynchronously
  retrieve()       — finds the top-K most relevant past memories for a given query

Every function that touches the database takes a `principal` carrying a
verified workspace, positionally and first. `user_id` stays alongside it and
keeps doing what Fix #34 gave it — separating one person's notes from the
shared corpus WITHIN a workspace — but it was never an isolation boundary: it
is a free-form string ("owner", "system", a username) that means the same thing
in every install. The boundary is `tenant_id` and `workspace_id`, enforced by
RLS on the bound connection.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from memory.embeddings import embed_one
from memory.namespaces import (
    RETENTION_EXEMPT_NAMESPACES,
    SYSTEM_NAMESPACE,
    VAULT_NAMESPACE,
)
from memory.vault import may_recall_vault

logger = logging.getLogger("ira.memory")

#: The `source_type` of a chat transcript row: every message `save_message`
#: embeds, the person's and IRA's. Recall leaves these out of an answer that
#: has a relevant Memory Vault fact (see `retrieve`).
TRANSCRIPT_SOURCE_TYPE = "message"

# Fix #44: module-level set keeps strong references to background embedding tasks
# so Python's garbage collector cannot collect them before they finish.
# The asyncio event loop only holds a *weak* reference to tasks; without a live
# strong reference the task can be silently dropped mid-execution.
_background_tasks: set[asyncio.Task] = set()


# ── Conversation persistence ───────────────────────────────────────────────────

async def ensure_conversation(principal, session_id: str) -> str:
    """Return the caller's conversation for a session in this workspace, creating one if needed.

    The SELECT and the INSERT run in one bound transaction, so a session id that
    exists in another workspace is invisible here and a new conversation is
    created rather than the other workspace's being reused. That is the correct
    outcome: a session identifier is not a capability.

    Since ADR-0009 (093) the same holds between people in ONE workspace: the
    policy admits only `owner_user_id = app.user_id`, so another user's session
    id finds nothing and this creates the caller's own conversation, owned by
    the caller through the column default.
    """
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT id FROM conversations WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
        if row:
            return str(row["id"])
        conv_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO conversations (id, session_id, tenant_id, workspace_id)
               VALUES ($1, $2,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid)""",
            uuid.UUID(conv_id), session_id,
        )
        return conv_id


async def save_message(
    principal,
    conversation_id: str,
    role: str,
    content: str,
    *,
    model_used: str | None = None,
    latency_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    user_id: str,   # Fix P31: required — every embedding must be scoped to a user
) -> str:
    """Persist a message and kick off async embedding storage.

    A user message that carries the Armory master key is stored and embedded with
    the key replaced by a placeholder (utils/secret_redaction.py): the agent has
    already read the live message, and the key must not sit in the database or
    be replayed as history on later turns.
    """
    msg_id = str(uuid.uuid4())
    from core.identity.db_context import workspace_connection
    if role == "user":
        from utils.secret_redaction import redact_master_key
        content = redact_master_key(content)

    async with workspace_connection(principal) as conn:
        await conn.execute(
            """INSERT INTO messages
               (id, conversation_id, role, content, model_used, latency_ms,
                tokens_in, tokens_out, tenant_id, workspace_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid)""",
            uuid.UUID(msg_id),
            uuid.UUID(conversation_id),
            role, content, model_used, latency_ms, tokens_in, tokens_out,
        )
    # Fix #44: keep a strong reference in the module-level set so the task
    # cannot be GC'd before it finishes; discard on completion to prevent growth.
    # The principal is captured by the task, not re-derived when it runs. A
    # background embedding write that resolved its own context could land in a
    # different workspace from the message it describes.
    _t = asyncio.create_task(
        _store_embedding(principal, msg_id, content, TRANSCRIPT_SOURCE_TYPE, user_id=user_id)
    )
    _background_tasks.add(_t)
    _t.add_done_callback(_background_tasks.discard)
    _t.add_done_callback(lambda t: t.exception() and logger.warning(f"Embedding task failed: {t.exception()}"))
    return msg_id


async def remember(
    principal, content: str, *, kind: str = "note", user_id: str = VAULT_NAMESPACE,
    strict: bool = False,
) -> str:
    """Persist a standalone memory (not tied to a conversation) into the vector store.

    Used by always-on components (e.g. the realtime brain) to write durable
    thoughts / consolidations straight into ``memory_embeddings``. Awaits the
    embedding write so the caller can treat it as best-effort and fail soft.

    By default a failed write is logged at DEBUG and the call returns as if it
    had succeeded. ``strict=True`` raises instead, so a caller that reports its
    own failures can. The realtime brain does: from commit 19c3415 (6 August
    2026) every one of its writes failed, and only a DEBUG line said so.
    """
    source_id = str(uuid.uuid4())
    await _store_embedding(principal, source_id, content, kind, user_id=user_id,
                           strict=strict)
    return source_id


async def _store_embedding(
    principal,
    source_id: str,
    content: str,
    source_type: str,
    *,
    user_id: str = SYSTEM_NAMESPACE,   # Fix #34: tag every embedding with the owning user
    strict: bool = False,
) -> None:
    """Embed content and store the vector — runs as a background task."""
    try:
        vector = await embed_one(content)
        vector_str = "[" + ",".join(map(str, vector)) + "]"
        from core.identity.db_context import workspace_connection

        async with workspace_connection(principal) as conn:
            await conn.execute(
                """INSERT INTO memory_embeddings
                   (source_id, source_type, content, embedding, user_id,
                    tenant_id, workspace_id)
                   VALUES ($1, $2, $3, $4::vector, $5,
                           NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                           NULLIF(current_setting('app.workspace_id', true), '')::uuid)""",
                uuid.UUID(source_id), source_type, content, vector_str, user_id,
            )
    except Exception as e:
        if strict:
            raise
        logger.debug(f"Embedding store failed (non-fatal): {e}")


# ── Memory retrieval (RAG) ────────────────────────────────────────────────────
#
# Every vector query over `memory_embeddings` filters first and orders exactly:
#
#     WITH scoped AS MATERIALIZED (
#              SELECT id, embedding <=> $1::vector AS distance
#                FROM memory_embeddings
#               WHERE <namespace filters> AND embedding IS NOT NULL),
#          nearest AS (SELECT id, distance FROM scoped ORDER BY distance LIMIT $2)
#     SELECT m.<columns>, 1 - n.distance AS similarity
#       FROM nearest n JOIN memory_embeddings m ON m.id = n.id
#      ORDER BY n.distance
#
# and never `... WHERE <filters> ORDER BY embedding <=> $1 LIMIT $2`. The short
# form lets the planner answer the ORDER BY from `idx_memory_embedding_hnsw`,
# which covers every tenant, workspace and namespace in the table. HNSW returns
# about `hnsw.ef_search` (40) nearest rows of the WHOLE table, and only then do
# RLS and the namespace filter run, so on a crowded table the caller gets back
# few of their rows or none, though every one is visible on the same connection.
# The planner picks that plan by itself once the table is big enough.
#
# The MATERIALIZED CTE is a planner fence. The index can serve neither the scan
# inside it, which has no ORDER BY, nor the sort outside it, which orders the
# CTE rather than the table. So the search is exact over the caller's own rows,
# whatever the planner settings. The CTE carries only id and distance, so a large
# store does not materialise every row's content before the top-n are known.
# Rows stored without an embedding (the vault keeps text when the model is
# down) are excluded, as the index plan always excluded them: their distance is
# NULL, and a NULL similarity cannot be compared with the floor.
#
# The keyword half of `_retrieve_hybrid` excludes them too, for the same reason:
# a text-only row naming every query term used to reach `float(None)` there and
# fail the whole call. It is excluded in the WHERE clause rather than skipped in
# Python, so it never takes a LIMIT slot an embedded match needs. The Technical
# Library deliberately leaves its keyword list unfloored; personal memory floors
# every result, so the two differ here on purpose.
#
# Only ACTIVE memories are recalled: not deleted, not replaced, not past their
# validity (the same predicate as `memory.temporal.ACTIVE_FILTER_SQL`, written
# out because a formatted SQL string is what the security gate refuses). A
# memory deleted on the privacy page is only marked `deleted_at`, and a fact the
# owner replaced is only marked `superseded_by`; without this, chat went on
# recalling both. `tests/tenancy/test_recall_skips_inactive_memories.py`.
#
# Exact search costs time in proportion to the rows it scores, and to the rows
# its index visits to find them. The measurements, that caveat, and the
# approximate options that were rejected are in
# docs/architecture/adr/ADR-0005-vector-search-is-exact-over-the-callers-scope.md.
# `tests/tenancy/test_vector_recall_crowded_table.py` proves the shape on a real
# database; `tests/test_vector_query_shape.py` stops the short form coming back.

async def retrieve(
    principal,
    query: str,
    user_id: str = SYSTEM_NAMESPACE,  # Fix #34: filter to the calling user's memories only
    top_k: int | None = None,
) -> list[dict]:
    """
    Find the top-K most semantically relevant memories for a query.

    Fix #34: results are scoped to ``user_id`` so users cannot see each other's
    memories within a workspace. When ``principal`` is the owner, the Memory
    Vault's rows (``user_id = VAULT_NAMESPACE``) are searched as well: the vault
    writes under that marker while chat asks under the signed-in username, so
    without this the owner's curated memories were never recalled. See
    :func:`_namespaces`.

    Relevant Memory Vault rows come first, marked ``"vault": True``, then the
    rest; see the note in the body.

    Returns a list of {"content": str, "source_type": str, "similarity": float}.
    """
    from config import get_settings
    cfg = get_settings()
    k = top_k or cfg.rag_top_k
    namespaces = _namespaces(principal, user_id)

    query_vec = await embed_one(query)
    vector_str = "[" + ",".join(map(str, query_vec)) + "]"

    # Fix P20: read threshold from config so it's tunable without a code change
    threshold = cfg.rag_min_similarity

    # A2: when reranking, over-fetch a wider candidate pool by vector distance and
    # let the cross-encoder pick the best. Otherwise fetch exactly k (today's
    # behavior). The vector query and stored embeddings are unchanged either way.
    fetch_n = cfg.rag_candidate_k if cfg.reranker_enabled else k

    # The Memory Vault comes first. A fact the owner saved is what they asked IRA
    # to know; chat transcripts are what was said, including IRA's own earlier
    # answers. Ranked together, the transcripts won: on the 18 October upgrade
    # copy, two copies of the question and two of IRA's old "there is no such
    # project" answers outranked the saved fact, which came fifth of five and
    # the model repeated the denial (docs/testing/evidence/upgrade-089-2026-10-19/).
    # With more chat history the fact would not have reached the candidate pool
    # at all. So each group is searched and ranked on its own, with the same
    # floor, and relevant vault rows go ahead of everything else before the cut
    # to k. A vault row below the floor is still left out: first means first
    # among relevant memories, not every vault entry in every answer.
    #
    # And when a relevant vault fact IS found, chat transcripts are left out of
    # that answer. First place was not enough: with the fact on top and the
    # transcripts below it, the model still repeated IRA's own earlier denial,
    # 0 runs of 2; with the transcripts left out it answered from the fact, 2 of
    # 2 (docs/testing/evidence/recall-vault-first-2026-10-20/). The owner chose
    # this on 20 October. Only transcripts go: anything else filed under the
    # person's name still competes for the remaining places. With no relevant
    # vault fact, transcripts are recalled exactly as before.
    results: list[dict] = []
    vault_found = False
    for group, from_vault in _vault_first_groups(namespaces):
        ranked = await _ranked(
            principal, query, vector_str, group, k=k, fetch_n=fetch_n,
            threshold=threshold, cfg=cfg,
            exclude_source_types=[TRANSCRIPT_SOURCE_TYPE] if vault_found else [],
        )
        if from_vault:
            for r in ranked:
                r["vault"] = True
            vault_found = bool(ranked)
        results.extend(ranked)
    return results[:k]


def _vault_first_groups(namespaces: list[str]) -> list[tuple[list[str], bool]]:
    """The namespace groups to rank separately, the vault's first.

    One group, unchanged from before and unmarked, when the vault is not among
    the namespaces or is the only one: with nothing to rank it against, there
    is no order to keep.
    """
    others = [n for n in namespaces if n != VAULT_NAMESPACE]
    if VAULT_NAMESPACE not in namespaces or not others:
        return [(namespaces, False)]
    return [([VAULT_NAMESPACE], True), (others, False)]


async def _ranked(
    principal, query: str, vector_str: str, namespaces: list[str], *,
    k: int, fetch_n: int, threshold: float, cfg,
    exclude_source_types: list[str],
) -> list[dict]:
    """The best `k` memories in `namespaces` above the floor, best first.

    Rows of `exclude_source_types` are left out inside the query, before the
    cut, so they cannot take places the other rows need.
    """
    # Opt-in hybrid (vector + full-text) retrieval. With the flag off — the
    # default — nothing below this branch changes.
    if getattr(cfg, "memory_hybrid_retrieval", False):
        return await _retrieve_hybrid(
            principal, query, vector_str, namespaces=namespaces, k=k,
            fetch_n=max(fetch_n, cfg.rag_candidate_k), threshold=threshold,
            exclude_source_types=exclude_source_types,
        )

    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """WITH scoped AS MATERIALIZED (
                   SELECT id, embedding <=> $1::vector AS distance
                     FROM memory_embeddings
                    WHERE user_id = ANY($3::text[]) AND embedding IS NOT NULL
                      AND deleted_at IS NULL AND superseded_by IS NULL
                      AND (valid_until IS NULL OR valid_until > NOW())
                      AND source_type <> ALL($4::text[])
               ),
               nearest AS (
                   SELECT id, distance FROM scoped ORDER BY distance LIMIT $2
               )
               SELECT m.content, m.source_type, 1 - n.distance AS similarity
                 FROM nearest n JOIN memory_embeddings m ON m.id = n.id
                ORDER BY n.distance""",
            vector_str, fetch_n, namespaces, exclude_source_types,
        )

    if cfg.reranker_enabled and rows:
        # A2: reorder candidates by query<->doc relevance (separate CPU model),
        # then apply the same cosine floor + top_k cut so "nothing relevant"
        # still returns []. Adds a "rerank_score" field; keeps "similarity".
        from memory.reranker import rerank
        scores = await rerank(query, [r["content"] for r in rows])
        ranked = sorted(zip(rows, scores), key=lambda rs: rs[1], reverse=True)
        results = [
            {"content": r["content"], "source_type": r["source_type"],
             "similarity": float(r["similarity"]), "rerank_score": float(s)}
            for r, s in ranked
            if float(r["similarity"]) >= threshold
        ][:k]
    else:
        results = [
            {"content": r["content"], "source_type": r["source_type"], "similarity": float(r["similarity"])}
            for r in rows
            if float(r["similarity"]) >= threshold
        ][:k]

    # Fix P20: log best similarity when all results are below threshold so operators
    # can tune RAG_MIN_SIMILARITY from logs without guessing.
    if not results and rows:
        best = max(float(r["similarity"]) for r in rows)
        logger.debug(
            "memory retrieve: best similarity %.3f below threshold %.2f — returning empty",
            best, threshold,
        )

    return results


def _namespaces(principal, user_id: str) -> list[str]:
    """The `user_id` values one retrieval may match.

    The caller's own namespace, plus the vault's when the verified principal is
    the owner (:func:`memory.vault.may_recall_vault`). Nobody else gains it: a
    delegated user in the owner's workspace passes every RLS policy the owner
    does, so this is the check that keeps the vault theirs. The tenant and
    workspace boundary is unchanged — it is still RLS on the bound connection.
    """
    namespaces = [user_id]
    if user_id != VAULT_NAMESPACE and may_recall_vault(principal):
        namespaces.append(VAULT_NAMESPACE)
    return namespaces


async def _retrieve_hybrid(
    principal, query: str, vector_str: str, *, namespaces: list[str], k: int,
    fetch_n: int, threshold: float, exclude_source_types: list[str],
) -> list[dict]:
    """Vector + full-text retrieval fused with Reciprocal Rank Fusion.

    Behind `memory_hybrid_retrieval` (default OFF). The same cosine floor as the
    vector-only path applies to every result, so turning this on can add a
    memory that NAMES the query's terms but was outside the vector top-N — it
    cannot add one the floor would have rejected. A text-only row (no vector)
    is the limiting case: it has no similarity, so neither half returns it.
    Personal memory has no full-text index (088 indexes only the Technical
    Library), so the keyword half scans the caller's namespaces; personal
    memory is small enough for that.
    """
    from config import get_settings
    from core.identity.db_context import workspace_connection
    from memory.hybrid import rrf_fuse

    cfg = get_settings()
    async with workspace_connection(principal) as bound:
        conn: Any = bound
        # Filter-first exact search; see the note above `retrieve()`.
        vector_rows = await conn.fetch(
            """WITH scoped AS MATERIALIZED (
                   SELECT id, embedding <=> $1::vector AS distance
                     FROM memory_embeddings
                    WHERE user_id = ANY($3::text[]) AND embedding IS NOT NULL
                      AND deleted_at IS NULL AND superseded_by IS NULL
                      AND (valid_until IS NULL OR valid_until > NOW())
                      AND source_type <> ALL($4::text[])
               ),
               nearest AS (
                   SELECT id, distance FROM scoped ORDER BY distance LIMIT $2
               )
               SELECT m.id, m.content, m.source_type, 1 - n.distance AS similarity
                 FROM nearest n JOIN memory_embeddings m ON m.id = n.id
                ORDER BY n.distance""",
            vector_str, fetch_n, namespaces, exclude_source_types,
        )
        keyword_rows = await conn.fetch(
            """SELECT id, content, source_type,
                      1 - (embedding <=> $1::vector) AS similarity
               FROM memory_embeddings, websearch_to_tsquery('english', $4) AS q
               WHERE user_id = ANY($3::text[]) AND embedding IS NOT NULL
                 AND deleted_at IS NULL AND superseded_by IS NULL
                 AND (valid_until IS NULL OR valid_until > NOW())
                 AND source_type <> ALL($5::text[])
                 AND to_tsvector('english', content) @@ q
               ORDER BY ts_rank_cd(to_tsvector('english', content), q) DESC
               LIMIT $2""",
            vector_str, fetch_n, namespaces, query, exclude_source_types,
        )

    by_id = {}
    ranked: dict[str, list] = {}
    for name, rows in (("vector", vector_rows), ("keyword", keyword_rows)):
        ids = []
        for r in rows:
            if float(r["similarity"]) < threshold:
                continue
            by_id[r["id"]] = r
            ids.append(r["id"])
        if ids:
            ranked[name] = ids
    fused = rrf_fuse(ranked, k=getattr(cfg, "technical_library_rrf_k", 60))
    results = [
        {"content": by_id[f.key]["content"], "source_type": by_id[f.key]["source_type"],
         "similarity": float(by_id[f.key]["similarity"]), "retrievers": f.retrievers}
        for f in fused
    ]

    if cfg.reranker_enabled and results:
        from memory.reranker import rerank
        scores = await rerank(query, [r["content"] for r in results])
        for r, s in zip(results, scores):
            r["rerank_score"] = float(s)
        results.sort(key=lambda r: r["rerank_score"], reverse=True)
    return results[:k]


# ── Memory retention (Fix #75) ────────────────────────────────────────────────

async def purge_old_memories(principal, retention_days: int = 90) -> int:
    """
    Delete this workspace's memory embeddings older than ``retention_days``.

    Fix #75: without this the memory_embeddings table grows unbounded. Run
    weekly via the scheduler. Returns the number of rows deleted.

    Note: messages and conversations are NOT deleted — only the embedding
    vectors are purged. Chat history remains intact.

    The Technical Library and self-knowledge corpora share this table and are
    NOT purged: they are reference material with their own ingest, and the
    Technical Library's incremental ingest would never re-index a purged
    document. Meeting transcripts are purged. The list and the reasons are
    `RETENTION_EXEMPT_NAMESPACES` in `memory.namespaces`.

    **This ran through an unbound connection until Batch 5C, which since
    `023_tenancy.sql` forced RLS on `memory_embeddings` meant it deleted
    nothing and reported "purged 0" every week.** Retention that silently does
    nothing is worse than retention that errors: the log line reads like
    success, so the table grows unbounded exactly as Fix #75 set out to
    prevent. `purge_old_memories_for_every_tenant` below is how the scheduler
    now covers the whole estate — N scoped passes, never one unscoped pass.
    """
    try:
        from core.identity.db_context import workspace_connection

        async with workspace_connection(principal) as conn:
            result = await conn.execute(
                # Fix #94: make_interval() avoids asyncpg type-inference ambiguity
                # with parameterised interval arithmetic ($1 * INTERVAL '1 day').
                "DELETE FROM memory_embeddings"
                " WHERE created_at < NOW() - make_interval(days => $1)"
                "   AND user_id <> ALL($2::text[])",
                retention_days, sorted(RETENTION_EXEMPT_NAMESPACES),
            )
        # asyncpg returns "DELETE N" as the status string
        count = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
        logger.info(f"Memory retention: purged {count} embeddings older than {retention_days} days")
        return count
    except Exception as e:
        logger.error(f"Memory retention purge failed: {e}")
        return 0


async def purge_old_memories_for_every_tenant(retention_days: int = 90) -> int:
    """Run the retention purge once per tenant, each pass scoped to that tenant.

    The supported shape for cross-tenant background work
    (`core.identity.background`): iterate and bind, rather than run once with
    the boundary off. A bug in the loop body can then affect one tenant's rows,
    not everybody's.

    The tenant list is read through a bare connection because `tenants` is the
    platform registry and carries no RLS — see R-46, which is about that being
    readable at all and is not closed by this function. No customer content is
    read here; only tenant identifiers, to decide which scoped passes to run.

    Each tenant is purged in its DEFAULT workspace only. That is a deliberate
    limitation and not a silent one: until every writer supplies a workspace,
    the rows a purge would need to reach in other workspaces are the same rows
    Batch 5C is still binding, and a retention job that outran the writers
    would delete from workspaces nothing has correctly written to yet.
    """
    from core.identity.background import default_workspace_job_principal
    from utils.db import acquire

    total = 0
    try:
        async with acquire() as conn:
            tenants = await conn.fetch("SELECT id FROM tenants")
    except Exception as e:
        logger.error(f"Memory retention: could not list tenants: {e}")
        return 0

    for row in tenants:
        try:
            principal = await default_workspace_job_principal(
                row["id"], job="memory_retention"
            )
            total += await purge_old_memories(principal, retention_days)
        except Exception as e:  # noqa: BLE001 — one tenant must not stop the rest
            logger.error("Memory retention failed for tenant %s: %s", row["id"], e)

    logger.info(
        "Memory retention: purged %d embeddings across %d tenants", total, len(tenants)
    )
    return total


async def get_recent_messages(principal, conversation_id: str, limit: int = 20) -> list[dict]:
    """Retrieve the most recent messages for context injection."""
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM messages
               WHERE conversation_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            uuid.UUID(conversation_id), limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
