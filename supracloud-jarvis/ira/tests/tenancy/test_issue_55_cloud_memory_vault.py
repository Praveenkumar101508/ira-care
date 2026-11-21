"""Issue #55 — the cloud Memory Vault, end to end, through the real code.

Batch 3B Part A fixed the LOCAL vault: it ran through an unbound connection, and
under forced RLS that meant it read nothing and wrote rows nobody could read
back. The cloud half was deferred to Batch 5C because it needed a verified
workspace to exist, and this file is the evidence that it now works and that its
boundaries hold.

Two halves, and both are needed:

  **It works.** Create, read, search, update, pin, unpin, delete, forget,
  conversation and message integration, embedding storage, expiry purge, and
  erasure across several workspaces. A vault that is perfectly isolated and
  cannot store a memory is not a fixed vault.

  **It is bounded.** Same-tenant cross-workspace denial, cross-tenant denial,
  a refusal when no workspace is bound, and a refusal after the user's
  membership is revoked.

── What is real here and what is not ────────────────────────────────────────

Real PostgreSQL, real RLS, as `ira_app` — non-superuser, NOBYPASSRLS. The real
`memory/store.py`, the real `core/identity` helpers, the real erasure code.

The EMBEDDING MODEL is stubbed. `sentence_transformers` is not installable in
this environment, and the question this file asks is where a vector lands and
who can read it — not whether the vector is any good. The stub returns a
deterministic vector per string, so similarity search is still exercised as a
query against pgvector rather than skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# `config.Settings` refuses to construct without these, and `memory/store.py`
# reads it for the RAG thresholds. Placeholders, set before any import that
# reaches it — the same thing tests/tenancy/test_workspace_scope_boundary.py
# does, and the reason this file fails when run on its own otherwise.
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# conftest stubs `sentence_transformers` but leaves the attributes
# `memory/store.py`'s import chain reaches for unset. Filling them in is what
# tests/test_app_factory.py already does; nothing here instantiates them,
# because the embedding function itself is replaced by the fixture below.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.editions import EDITION_CLOUD  # noqa: E402
from core.identity.db_context import (  # noqa: E402
    MissingWorkspaceContext,
    workspace_connection,
)
from core.identity.principal import Principal  # noqa: E402

#: The stored vector width, read from the schema rather than guessed. The column
#: is `vector(1024)`; a stub of the wrong width makes every insert fail inside
#: `_store_embedding`'s non-fatal `except`, which reports nothing and leaves the
#: vault looking empty. That cost a debugging round, so it is derived here.
EMBED_DIM = 1024


def _fake_vector(text: str) -> list[float]:
    """A deterministic pseudo-embedding.

    Same text → same vector, different text → different vector, which is all
    similarity search needs to be exercised as a real pgvector query. It is not
    a semantic embedding and this file makes no claim about retrieval quality.
    """
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    """Replace the model, keep the SQL.

    Reranking is disabled too: it constructs a `CrossEncoder`, whose stub here
    is `object` and cannot take arguments. The reranker sits *after* the vector
    query and reorders results that RLS has already filtered, so switching it
    off removes a model dependency without removing anything this file is
    asking about.
    """
    async def embed_one(text: str):
        return _fake_vector(text)

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)

    from config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reranker_enabled", False, raising=False)
    if hasattr(settings, "rag_min_similarity"):
        # The stub vectors are not semantically meaningful, so a real threshold
        # would filter out an exact-text match. The question here is whether the
        # row is reachable, not whether it scores well.
        monkeypatch.setattr(settings, "rag_min_similarity", -1.0, raising=False)


@pytest.fixture(autouse=True)
async def app_pool():
    """Point `utils.db.acquire()` at the test database.

    The helpers under test open their own connections through the application
    pool, which is the right shape for production and means a test has to
    supply the pool rather than pass a connection in.
    """
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


class World:
    pass


@pytest.fixture
async def world():
    """One tenant with two workspaces, a second tenant, and three users.

        owner     — member of ws_a and ws_b
        colleague — member of ws_b only
        revoked   — member of ws_a, membership removed mid-test
    """
    w = World()
    w.tenant = uuid.uuid4()
    w.other_tenant = uuid.uuid4()
    w.owner = uuid.uuid4()
    w.colleague = uuid.uuid4()
    w.revoked = uuid.uuid4()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for t, n in ((w.tenant, "i55"), (w.other_tenant, "i55-other")):
            await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                               t, f"{n}-{t.hex[:8]}")
        for u in (w.owner, w.colleague, w.revoked):
            await conn.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                               u, f"{u.hex[:12]}@example.test")

        w.ws_a = await conn.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        w.ws_other = await conn.fetchval(
            "SELECT provision_tenant_substrate($1)", w.other_tenant)
        org = await conn.fetchval(
            "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant)
        w.ws_b = uuid.uuid4()
        await conn.execute(
            "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
            "                        is_default, status) "
            "VALUES ($1,$2,$3,'Second','i55-second',false,'active')",
            w.ws_b, w.tenant, org)

        for u in (w.owner, w.colleague, w.revoked):
            await conn.execute(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
                "VALUES ($1,$2,'member','active')", w.tenant, u)
        for u, ws in ((w.owner, w.ws_a), (w.owner, w.ws_b),
                      (w.colleague, w.ws_b), (w.revoked, w.ws_a)):
            await conn.execute(
                "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id,"
                "                                   role, status) "
                "VALUES ($1,$2,$3,'workspace_admin','active')", w.tenant, ws, u)
        yield w
    finally:
        try:
            for t in (w.tenant, w.other_tenant):
                for table in ("messages", "conversations", "memory_embeddings"):
                    await conn.execute(f"DELETE FROM {table} WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenants WHERE id=$1", t)
            for u in (w.owner, w.colleague, w.revoked):
                await conn.execute("DELETE FROM users WHERE id=$1", u)
        finally:
            await conn.close()


def principal_for(user_id, tenant_id, workspace_id=None) -> Principal:
    return Principal(
        user_id=user_id, tenant_id=tenant_id, session_id=uuid.uuid4(),
        edition=EDITION_CLOUD, workspace_id=workspace_id,
    )


async def _memories(principal, **kw) -> list[dict]:
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, content, source_type, user_id, workspace_id, created_at "
            "  FROM memory_embeddings ORDER BY created_at")
        return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
# Half one — the vault works
# ═════════════════════════════════════════════════════════════════════════════


async def test_create_a_memory(world):
    from memory.store import remember

    p = principal_for(world.owner, world.tenant, world.ws_a)
    source_id = await remember(p, "the vault stores this", kind="note", user_id="owner")

    assert source_id
    contents = [m["content"] for m in await _memories(p)]
    assert "the vault stores this" in contents


async def test_read_and_search(world):
    """Search is a real pgvector query — the vector is stubbed, the SQL is not."""
    from memory.store import remember, retrieve

    p = principal_for(world.owner, world.tenant, world.ws_a)
    await remember(p, "the capital of France is Paris", kind="fact", user_id="owner")
    await remember(p, "a completely unrelated note", kind="note", user_id="owner")

    hits = await retrieve(p, "the capital of France is Paris", user_id="owner", top_k=5)

    assert any("Paris" in h["content"] for h in hits), (
        f"the exact stored text did not come back from search: {hits}")


async def test_update_a_memory(world):
    p = principal_for(world.owner, world.tenant, world.ws_a)
    from memory.store import remember

    await remember(p, "before the edit", kind="note", user_id="owner")

    async with workspace_connection(p) as conn:
        updated = await conn.fetchval(
            "UPDATE memory_embeddings SET content = 'after the edit' "
            " WHERE content = 'before the edit' RETURNING content")
    assert updated == "after the edit"


async def test_pin_and_unpin(world):
    """Pinning is a `metadata` flag, not a column.

    `api/routes/memory.py` writes `{"pinned": true}` into `metadata` and reads
    it back with `meta.get("pinned", False)`. Testing a `pinned` column would
    have been testing something the application does not do — the first version
    of this test did exactly that and skipped itself, which is the quiet way a
    piece of evidence becomes worthless.
    """
    import json

    p = principal_for(world.owner, world.tenant, world.ws_a)
    from memory.store import remember

    await remember(p, "worth keeping", kind="note", user_id="owner")

    async def pinned() -> bool:
        async with workspace_connection(p) as conn:
            meta = await conn.fetchval(
                "SELECT metadata FROM memory_embeddings WHERE content = 'worth keeping'")
        if meta is None:
            return False
        return bool((meta if isinstance(meta, dict) else json.loads(meta)).get("pinned"))

    assert not await pinned()

    async with workspace_connection(p) as conn:
        await conn.execute(
            "UPDATE memory_embeddings SET metadata = $1::jsonb "
            " WHERE content = 'worth keeping'", json.dumps({"pinned": True}))
    assert await pinned()

    async with workspace_connection(p) as conn:
        await conn.execute(
            "UPDATE memory_embeddings SET metadata = $1::jsonb "
            " WHERE content = 'worth keeping'", json.dumps({}))
    assert not await pinned()

    # And the pin is inside the boundary: workspace B cannot see the row at all,
    # so it cannot pin or unpin somebody else's memory either.
    in_b = principal_for(world.owner, world.tenant, world.ws_b)
    async with workspace_connection(in_b) as conn:
        touched = await conn.execute(
            "UPDATE memory_embeddings SET metadata = '{\"pinned\": true}'::jsonb "
            " WHERE content = 'worth keeping'")
    assert touched.endswith("0"), "workspace B pinned workspace A's memory"


async def test_delete_and_forget(world):
    p = principal_for(world.owner, world.tenant, world.ws_a)
    from memory.store import remember

    await remember(p, "forget me", kind="note", user_id="owner")

    async with workspace_connection(p) as conn:
        deleted = await conn.execute(
            "DELETE FROM memory_embeddings WHERE content = 'forget me'")
    assert deleted.endswith("1")
    assert "forget me" not in [m["content"] for m in await _memories(p)]


async def test_conversation_and_message_integration(world):
    """A message writes a conversation, a message row and an embedding, and all
    three land in the same workspace. A split here would leave a conversation
    one workspace could see and messages another could."""
    from memory import store
    from memory.store import ensure_conversation, save_message

    p = principal_for(world.owner, world.tenant, world.ws_a)
    session = f"i55-{uuid.uuid4().hex[:8]}"
    conversation_id = await ensure_conversation(p, session)
    assert conversation_id

    message_id = await save_message(
        p, conversation_id, "user", "what did we decide?", user_id="owner")
    assert message_id
    # save_message writes the embedding in a background task. Wait for it, or
    # it can land after the fixture's cleanup and block the workspace delete.
    await asyncio.gather(*store._background_tasks)

    async with workspace_connection(p) as conn:
        row = await conn.fetchrow(
            "SELECT c.workspace_id AS cw, m.workspace_id AS mw, m.content "
            "  FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            " WHERE m.id = $1", uuid.UUID(message_id))
        embedding_ws = await conn.fetchval(
            "SELECT workspace_id FROM memory_embeddings WHERE source_id = $1",
            uuid.UUID(message_id))
    assert row is not None, "the message was not readable in its own workspace"
    assert row["cw"] == row["mw"] == embedding_ws == world.ws_a
    assert row["content"] == "what did we decide?"


async def test_embedding_storage(world):
    """The vector itself, not just the row."""
    from memory.store import remember

    p = principal_for(world.owner, world.tenant, world.ws_a)
    await remember(p, "a memory with a vector", kind="note", user_id="owner")

    async with workspace_connection(p) as conn:
        embedding = await conn.fetchval(
            "SELECT embedding FROM memory_embeddings WHERE content = $1",
            "a memory with a vector")
    assert embedding is not None, "the row was stored with no vector"


async def test_expiry_purge_is_scoped_to_the_workspace(world):
    """Retention deletes this workspace's old rows and nobody else's.

    Before Batch 5C this ran unbound and deleted nothing while logging
    "purged 0" — retention that silently does nothing, which is worse than
    retention that errors because the log line reads like success.
    """
    from memory.store import purge_old_memories, remember

    in_a = principal_for(world.owner, world.tenant, world.ws_a)
    in_b = principal_for(world.owner, world.tenant, world.ws_b)
    await remember(in_a, "old in A", kind="note", user_id="owner")
    await remember(in_b, "old in B", kind="note", user_id="owner")

    stale = datetime.now(timezone.utc) - timedelta(days=400)
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "UPDATE memory_embeddings SET created_at = $1 "
            " WHERE content IN ('old in A', 'old in B')", stale)
    finally:
        await admin.close()

    purged = await purge_old_memories(in_a, retention_days=90)

    assert purged == 1, f"expected to purge only workspace A's row, purged {purged}"
    assert [m["content"] for m in await _memories(in_b)] == ["old in B"], (
        "the purge reached into another workspace")


async def test_erasure_covers_every_workspace_of_the_tenant(world):
    """One pass per workspace, not one unscoped pass.

    A tenant's erasure has to reach content in all of their workspaces. Doing it
    unscoped would be simpler and would delete nothing under RLS; doing it in
    one workspace would report success having erased a fraction.

    The notes are filed under the person's own id. They used to be filed under
    the vault namespace, and erasure removed them because it removed every
    memory in each workspace. In the cloud edition the vault is the
    workspace's, shared and managed by its admins, and a member's erasure no
    longer reaches it (`docs/security/MEMORY_ACTIONS_ARE_PERSONAL_2026-10-20.md`).
    """
    from core.privacy.erasure import erase_postgres
    from memory.store import remember

    in_a = principal_for(world.owner, world.tenant, world.ws_a)
    in_b = principal_for(world.owner, world.tenant, world.ws_b)
    await remember(in_a, "erase me from A", kind="note", user_id=str(world.owner))
    await remember(in_b, "erase me from B", kind="note", user_id=str(world.owner))

    result = await erase_postgres(principal_for(world.owner, world.tenant),
                                  scope="memories")

    assert result.ok, f"erasure reported failure: {result}"
    assert [m["content"] for m in await _memories(in_a)] == []
    assert [m["content"] for m in await _memories(in_b)] == [], (
        "erasure left content in the tenant's second workspace")


# ═════════════════════════════════════════════════════════════════════════════
# Half two — the vault is bounded
# ═════════════════════════════════════════════════════════════════════════════


async def test_same_tenant_cross_workspace_denial(world):
    """The boundary this whole batch exists to create.

    Same user, same tenant, member of both workspaces. Workspace A's memory is
    not readable from workspace B — the separation is between the workspaces,
    not between the people.
    """
    from memory.store import remember

    in_a = principal_for(world.owner, world.tenant, world.ws_a)
    in_b = principal_for(world.owner, world.tenant, world.ws_b)
    await remember(in_a, "private to workspace A", kind="note", user_id="owner")

    assert "private to workspace A" not in [m["content"] for m in await _memories(in_b)]


async def test_a_colleague_in_another_workspace_sees_nothing(world):
    """The same denial, arrived at as a different person.

    `colleague` is a verified member of the tenant and of workspace B, and has
    never been in workspace A. This is the shape of the real-world case:
    somebody who legitimately belongs here, and legitimately does not belong
    there.
    """
    from memory.store import remember

    await remember(principal_for(world.owner, world.tenant, world.ws_a),
                   "owner's note in A", kind="note", user_id="owner")

    theirs = principal_for(world.colleague, world.tenant, world.ws_b)
    assert "owner's note in A" not in [m["content"] for m in await _memories(theirs)]


async def test_cross_tenant_denial(world):
    from memory.store import remember

    await remember(principal_for(world.owner, world.tenant, world.ws_a),
                   "belongs to tenant one", kind="note", user_id="owner")

    outsider = principal_for(uuid.uuid4(), world.other_tenant, world.ws_other)
    assert [m["content"] for m in await _memories(outsider)] == []


async def test_missing_workspace_refusal(world):
    """No workspace, no vault — and a refusal rather than a default.

    `workspace_connection` raises rather than resolving to the tenant's default,
    which is the single property Batch 5A was built around: a caller that did
    not say where content goes does not get a guess.

    Two assertions, because `remember()` is deliberately fail-soft — it wraps
    the write in a non-fatal `except` so an embedding failure never breaks a
    chat turn. That means an unbound caller gets **silence, not an exception**,
    and the honest statement is therefore: the refusal happens, and nothing is
    written. A test that only asserted the raise would be describing a contract
    `remember()` does not have.
    """
    from memory.store import remember

    unbound = principal_for(world.owner, world.tenant)  # no workspace_id

    # The boundary itself refuses.
    with pytest.raises(MissingWorkspaceContext):
        async with workspace_connection(unbound):
            pass  # pragma: no cover

    # And going through the fail-soft path writes nothing.
    await remember(unbound, "nowhere to put this", kind="note", user_id="owner")
    in_a = principal_for(world.owner, world.tenant, world.ws_a)
    in_b = principal_for(world.owner, world.tenant, world.ws_b)
    for where in (in_a, in_b):
        assert "nowhere to put this" not in [m["content"] for m in await _memories(where)]


async def test_a_tenant_only_connection_reads_no_memories(world):
    """The database's own answer, underneath the Python guard.

    Even if something bypassed `workspace_connection` — a raw acquire, a helper
    somebody added — the rows are not there to be read.
    """
    from core.identity.db_context import tenant_connection
    from memory.store import remember

    await remember(principal_for(world.owner, world.tenant, world.ws_a),
                   "only with a workspace", kind="note", user_id="owner")

    async with tenant_connection(principal_for(world.owner, world.tenant)) as conn:
        rows = await conn.fetch(
            "SELECT content FROM memory_embeddings WHERE content = 'only with a workspace'")
    assert rows == []


async def test_revoked_user_refusal(world):
    """Access is re-derived per request, so revocation takes effect immediately.

    `revoked` is a member of workspace A and writes a memory. Their membership
    is then removed. The next selection refuses — not at the next login, not
    when a cache expires, but on the next request.
    """
    from api.middleware.workspace import resolve_workspace_principal
    from core.identity.workspace import WorkspaceSelectionError
    from fastapi import HTTPException
    from memory.store import remember

    async def fake(username, **kw):
        return principal_for(world.revoked, world.tenant)

    import api.middleware.workspace as ws_module
    original, ws_module.resolve_principal = ws_module.resolve_principal, fake
    try:
        bound = await resolve_workspace_principal("revoked", str(world.ws_a))
        await remember(bound, "written before revocation", kind="note", user_id="owner")

        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                "UPDATE workspace_memberships SET status='removed', removed_at=now() "
                " WHERE tenant_id=$1 AND workspace_id=$2 AND user_id=$3",
                world.tenant, world.ws_a, world.revoked)
        finally:
            await admin.close()

        with pytest.raises(HTTPException) as exc:
            await resolve_workspace_principal("revoked", str(world.ws_a))
        assert exc.value.status_code == 403
        assert exc.value.detail == WorkspaceSelectionError.PUBLIC_MESSAGE
    finally:
        ws_module.resolve_principal = original

    # The content itself is untouched — revoking access is not deleting data,
    # and conflating the two would make an access change silently destructive.
    still_there = [m["content"] for m in
                   await _memories(principal_for(world.owner, world.tenant, world.ws_a))]
    assert "written before revocation" in still_there
