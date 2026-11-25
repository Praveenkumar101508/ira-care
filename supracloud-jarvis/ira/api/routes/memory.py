"""
Memory Vault v1 — workspace CRUD over long-term memories (/api/v1/memory).

GET    /api/v1/memory             — list recent memories (?kind= filter, ?q= substring)
POST   /api/v1/memory             — save a memory {content, kind, pinned}
PUT    /api/v1/memory/{id}        — edit content (re-embeds; fail-soft on embed error)
POST   /api/v1/memory/{id}/pin    — pin / unpin (metadata flag)
DELETE /api/v1/memory/{id}        — forget (capability-gated + confirmation-gated)

Memories are DATA, never instructions: this API stores and returns content
verbatim as opaque text. The reference-only treatment happens at the prompt
layer (memory_context + the injection guard), which has its own tests.

Kinds are free-form slugs so a workspace can organise the vault however it
likes (profile, projects, job_search, decisions, preferences, documents,
reminders, goals, ...).

── Issue #55, cloud half ────────────────────────────────────────────────────

Every handler here reached the database through a bare `acquire()`. Since
`023_tenancy.sql` gave `memory_embeddings` a NOT NULL `tenant_id` and forced
RLS, that failed in both directions: reads matched nothing (an unset
`app.tenant_id` GUC is NULL, and `tenant_id = NULL` is NULL rather than true)
and writes were refused outright. The local edition was fixed on `main` by
binding the single private tenant. That is not the cloud answer — the cloud
answer is a *verified workspace*, which is what every handler below now takes.

The request never says whose data this is. `require_workspace_principal`
resolves the authenticated user, verifies tenant membership, verifies the
workspace belongs to that tenant, verifies workspace membership, and only then
produces a Principal that `workspace_connection` will accept. There is no path
through this file that reaches customer data without all of that having
happened.
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.middleware.workspace import (
    require_capability_or_403,
    require_workspace_principal,
)
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal
from memory.namespaces import VAULT_NAMESPACE
from api.routes._ledger import raise_if_ledger_refused
from api.routes._lockdown import refuse_during_lockdown
from utils.approval import owner_gated_action

logger = logging.getLogger("ira.memory_vault")

router = APIRouter(prefix="/memory", tags=["memory"])

_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

#: Marks a row as a curated vault entry rather than a chat-transcript
#: embedding. It is a KIND MARKER, not an isolation boundary — every existing
#: vault row carries it, and rewriting them would be a data migration disguised
#: as a route change.
#:
#: What changed in Batch 5C is what the boundary IS. `user_id = 'owner'` was
#: never one: it is the same literal string in every install and every tenant,
#: so on a shared database it would have selected everybody's vault at once.
#: Isolation now comes from `tenant_id` and `workspace_id`, enforced by RLS on
#: the bound connection, and this constant is left doing only the job it can
#: actually do — keeping `source_type = 'message'` transcripts out of the
#: vault listing, alongside the explicit filter that does the same.
#:
#: The value lives in memory/namespaces.py. Chat retrieval reads the same
#: constant — the owner's recall includes rows under this marker, so the writer
#: and the reader must not drift apart — and the delegated-account check
#: reserves it, so no account can be named after it and read the vault.
_VAULT_MARKER = VAULT_NAMESPACE


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20_000)
    kind: str = Field("note", description="Category slug, e.g. profile / projects / goals")
    pinned: bool = False


class UpdateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20_000)


class PinRequest(BaseModel):
    pinned: bool = True


def _validate_kind(kind: str) -> str:
    kind = kind.strip().lower()
    if not _KIND_RE.match(kind):
        raise HTTPException(
            status_code=422,
            detail="kind must be a slug: lowercase letters, digits, '-' or '_' (max 64 chars)",
        )
    return kind


def _row_out(row) -> dict:
    meta = row["metadata"] or {}
    if isinstance(meta, str):
        import json
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return {
        "id": str(row["id"]),
        "content": row["content"],
        "kind": row["source_type"],
        "pinned": bool(meta.get("pinned", False)),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def _embed_or_none(content: str):
    """Vector string for pgvector, or None when the embedder is unavailable."""
    try:
        from memory.embeddings import embed_one
        vector = await embed_one(content)
        return "[" + ",".join(map(str, vector)) + "]"
    except Exception as e:
        logger.warning(f"Memory vault: embedding unavailable, storing text only ({type(e).__name__})")
        return None


@router.get("")
async def list_memories(
    kind: str | None = Query(None, description="Filter by category slug"),
    q: str | None = Query(None, description="Substring filter over content"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_workspace_principal),
):
    """List the workspace's curated memories, newest first."""
    # Current facts only. A fact the owner replaced ("Replace old" in chat) is
    # kept as history, linked to its replacement by `superseded_by`, and goes
    # when the current one is forgotten; one deleted on the privacy page is
    # marked `deleted_at`. Neither is a memory the owner has now.
    clauses = ["user_id = $1", "source_type <> 'message'",
               "superseded_by IS NULL", "deleted_at IS NULL"]
    args: list = [_VAULT_MARKER]
    if kind:
        args.append(_validate_kind(kind))
        clauses.append(f"source_type = ${len(args)}")
    if q:
        args.append(f"%{q}%")
        clauses.append(f"content ILIKE ${len(args)}")
    args += [limit, offset]

    # No `tenant_id = ...` or `workspace_id = ...` clause here on purpose. The
    # scope comes from the connection's GUCs via RLS, so it applies to every
    # statement in the transaction including one added later by somebody who
    # did not read this comment. A hand-written scope clause is a second
    # opinion that can disagree with the policy; there is nothing to gain by
    # having two answers to the same question.
    sql = (
        "SELECT id, content, source_type, metadata, created_at FROM memory_embeddings "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY created_at DESC LIMIT ${len(args)-1} OFFSET ${len(args)}"
    )
    async with workspace_connection(principal) as conn:
        await require_capability_or_403(conn, principal, "workspace.read")
        rows = await conn.fetch(sql, *args)
    items = [_row_out(r) for r in rows]
    return {"memories": items, "count": len(items)}


@router.post("")
async def create_memory(
    body: CreateMemoryRequest,
    principal: Principal = Depends(require_workspace_principal),
):
    """Save a memory into the vault (embedded for recall when possible)."""
    kind = _validate_kind(body.kind)
    vector_str = await _embed_or_none(body.content)
    import json
    meta = json.dumps({"pinned": body.pinned}) if body.pinned else json.dumps({})

    async with workspace_connection(principal) as conn:
        await require_capability_or_403(conn, principal, "workspace.write")
        # `tenant_id` is NOT NULL with no column default and `workspace_id` has
        # no usable default until every writer supplies one, so the INSERT has
        # to name both. Both are read from the transaction's own GUCs rather
        # than passed in from Python, and that is the point: the value the row
        # gets and the value the RLS policy checks come from the same place in
        # the same transaction, so they cannot disagree. A Python-side value
        # could drift from the bound context and the row would be refused with
        # no obvious reason why — or worse, accepted into the wrong workspace
        # if the policy were ever relaxed.
        row = await conn.fetchrow(
            """INSERT INTO memory_embeddings
                   (source_id, source_type, content, embedding, user_id, metadata,
                    tenant_id, workspace_id)
               VALUES ($1, $2, $3, $4::vector, $5, $6::jsonb,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid)
               RETURNING id, content, source_type, metadata, created_at""",
            uuid.uuid4(), kind, body.content, vector_str, _VAULT_MARKER, meta,
        )
    return _row_out(row)


@router.put("/{memory_id}")
async def update_memory(
    memory_id: uuid.UUID,
    body: UpdateMemoryRequest,
    principal: Principal = Depends(require_workspace_principal),
):
    """Edit a memory's content; re-embeds so recall matches the new text."""
    vector_str = await _embed_or_none(body.content)
    async with workspace_connection(principal) as conn:
        await require_capability_or_403(conn, principal, "workspace.write")
        row = await conn.fetchrow(
            """UPDATE memory_embeddings
               SET content = $2, embedding = $3::vector
               WHERE id = $1 AND user_id = $4
               RETURNING id, content, source_type, metadata, created_at""",
            memory_id, body.content, vector_str, _VAULT_MARKER,
        )
    if row is None:
        # A memory in another workspace is invisible on this connection, so it
        # reports as "not found" rather than "forbidden". That is the correct
        # answer to give: distinguishing the two tells a caller which ids exist
        # elsewhere.
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    return _row_out(row)


@router.post("/{memory_id}/pin")
async def pin_memory(
    memory_id: uuid.UUID,
    body: PinRequest,
    principal: Principal = Depends(require_workspace_principal),
):
    """Pin or unpin a memory (a vault flag; pinned memories sort first in UIs)."""
    async with workspace_connection(principal) as conn:
        await require_capability_or_403(conn, principal, "workspace.write")
        row = await conn.fetchrow(
            """UPDATE memory_embeddings
               SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('pinned', $2::bool)
               WHERE id = $1 AND user_id = $3
               RETURNING id, content, source_type, metadata, created_at""",
            memory_id, body.pinned, _VAULT_MARKER,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    return _row_out(row)


# ── Forgetting a fact forgets the versions it replaced ───────────────────────
#
# "Replace old" (chat, `memory.contradiction_guard`) keeps the old fact as
# history: it stays in the vault, marked `superseded_by` the new one. Decided by
# the owner on 2026-10-19: forgetting a fact removes it together with every
# earlier version it replaced, and the confirmation says how many.
#
# The other two choices are both wrong. Deleting the current row alone is
# refused by `superseded_by`'s foreign key, which is what made forget fail on a
# replaced fact. Clearing the link instead would make the replaced version
# current again, so IRA would start recalling what the owner corrected.
#
# The chain follows `superseded_by` backwards from the fact, through vault rows
# only. UNION, not UNION ALL, so a malformed cycle ends instead of recursing.
# Both statements are complete literals: the security gate refuses SQL built by
# formatting or concatenation, so the chain is written out twice.

async def _count_earlier_versions(conn, memory_id: uuid.UUID) -> int:
    count = await conn.fetchval(
        """WITH RECURSIVE chain(id) AS (
               SELECT id FROM memory_embeddings WHERE id = $1 AND user_id = $2
               UNION
               SELECT m.id FROM memory_embeddings m JOIN chain c ON m.superseded_by = c.id
                WHERE m.user_id = $2
           )
           SELECT count(*) - 1 FROM chain""",
        memory_id, _VAULT_MARKER,
    )
    return max(int(count or 0), 0)


async def _delete_with_earlier_versions(conn, memory_id: uuid.UUID) -> int:
    """Delete the fact and the versions it replaced, in one statement, so the
    foreign key is checked once the whole chain is gone. Returns rows deleted."""
    result = await conn.execute(
        """WITH RECURSIVE chain(id) AS (
               SELECT id FROM memory_embeddings WHERE id = $1 AND user_id = $2
               UNION
               SELECT m.id FROM memory_embeddings m JOIN chain c ON m.superseded_by = c.id
                WHERE m.user_id = $2
           )
           DELETE FROM memory_embeddings
            WHERE user_id = $2 AND id IN (SELECT id FROM chain)""",
        memory_id, _VAULT_MARKER,
    )
    return int(str(result).rsplit(" ", 1)[-1] or 0)


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: uuid.UUID,
    confirm_token: str | None = Query(None, description="Approval token; omit to receive a draft"),
    principal: Principal = Depends(require_workspace_principal),
):
    """Forget a memory — capability-gated + confirmation-gated (destructive)."""
    async with workspace_connection(principal) as conn:
        # `workspace.delete_content` is carried by `workspace_admin` alone
        # (`040_workspace_memberships.sql`), so a `member` who may write cannot
        # destroy. This replaces the `.env`-owner check the private edition
        # used, which has no meaning in a multi-tenant process — and widens
        # nothing there, because the private edition's only workspace member is
        # that same owner.
        await require_capability_or_403(conn, principal, "workspace.delete_content")
        row = await conn.fetchrow(
            "SELECT content FROM memory_embeddings WHERE id = $1 AND user_id = $2",
            memory_id, _VAULT_MARKER,
        )
        earlier = 0 if row is None else await _count_earlier_versions(conn, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found.")
    snippet = (row["content"] or "")[:80]
    versions = ("" if not earlier else
                f" and the {earlier} earlier version{'s' if earlier != 1 else ''} "
                "it replaced")

    # The staged action captures `principal` by closure, so the DELETE that runs
    # on confirmation is bound to the same verified workspace as the preview.
    # Re-deriving the context at confirmation time would let a workspace switch
    # between draft and confirm delete a row the preview never described.
    async def _do():
        async with workspace_connection(principal) as conn:
            removed = await _delete_with_earlier_versions(conn, memory_id)
        return {"deleted": str(memory_id), "earlier_versions_deleted": max(removed - 1, 0)}

    # After the capability check, which is this route's authorisation; before
    # the draft and the confirmation, which both pass through here.
    await refuse_during_lockdown(risk="high", action_type="forget_memory", is_owner=True)
    outcome = await owner_gated_action(
        # The authorisation decision was made above by the capability check;
        # what this call still provides is the two-step confirmation guardrail
        # for a destructive action, which is why the flag is already settled.
        owner_username=principal.username or str(principal.user_id),
        is_owner=True,
        action="forget_memory",
        preview=(f"Forget memory {memory_id} ({snippet!r}...){versions} from your "
                 "Memory Vault — cannot be undone. What was said about it in chat "
                 "stays until you delete your conversations."),
        # The Trust Ledger is append-only: the snippet written there would
        # outlive the forget it records. It gets the id, never the text.
        ledger_preview=f"Forget memory {memory_id}{versions} from your Memory Vault",
        execute=_do, confirm_token=confirm_token,
    )
    raise_if_ledger_refused(outcome)   # 503: nothing ran, the token still works
    if outcome["status"] == "forbidden":
        raise HTTPException(status_code=403, detail=outcome["detail"])
    if outcome["status"] in ("expired", "not_found"):
        raise HTTPException(status_code=409, detail=outcome["detail"])
    if outcome["status"] == "executed":
        return outcome["result"]
    return outcome  # confirmation_required
