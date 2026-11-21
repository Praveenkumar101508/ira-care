"""Text-only memories must not break personal-memory recall.

The Memory Vault (`api/routes/memory.py`) and the command executor
(`commands/executor.py`) store a row with `embedding = NULL` when the embedding
model is unavailable: the memory is kept, it just has no vector. For such a row
`1 - (embedding <=> $1::vector)` is NULL, and `memory.store` turned every
similarity into a float with `float(r["similarity"])`. `float(None)` raises
`TypeError`, so one text-only row failed the whole `retrieve()` call.

It reached that code by three routes:

  **The keyword half of hybrid retrieval.** Any text-only row that names every
  query term matches the full-text query, however many embedded rows the user
  has. This is the route this file was written for.

  **The vector half of hybrid retrieval, and the default vector-only path.**
  Postgres sorts NULL distances last, so a text-only row comes back only when
  the caller has fewer embedded rows than the query fetches. A new or small
  memory is exactly that. The exact-search change (ADR-0005) excludes these
  rows from both vector queries, and `test_vector_recall_crowded_table.py`
  covers them there; the small-scope test below re-checks them with hybrid
  retrieval and the reranker switched on, which that file does not.

The chosen behaviour is that a text-only row is never returned. Both paths
apply a cosine floor to every result, and a row with no vector has no
similarity to clear it with. Passing it through, as the Technical Library's
keyword list does, would also hand `similarity: None` to callers that format or
compare it as a float.

Real PostgreSQL, real RLS, as `ira_app`. The embedding model is stubbed with a
deterministic pseudo-vector, as in `test_issue_55_cloud_memory_vault.py`. The
question here is which rows come back and whether the call survives, not
whether the vectors are any good.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import uuid
from typing import Any

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# The conftest stub names `SentenceTransformer` only. `memory/reranker.py`
# imports `CrossEncoder` at module scope, and the reranker fixture below has to
# import that module to replace `rerank`. Nothing constructs either class.
_st = sys.modules.get("sentence_transformers")
if _st is not None:
    for _name in ("SentenceTransformer", "CrossEncoder"):
        if not hasattr(_st, _name):
            setattr(_st, _name, object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.editions import EDITION_CLOUD  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402
from core.identity.principal import Principal  # noqa: E402
from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

EMBED_DIM = 1024

#: Names every term of the query, so the full-text half matches it.
QUERY = "Kestrel invoice"
TEXT_ONLY = "Kestrel invoice: written while the embedding model was down"
EMBEDDED = "the invoice for Kestrel is due Friday"


def _fake_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


def _cosine(a: str, b: str) -> float:
    va, vb = _fake_vector(a), _fake_vector(b)
    dot = sum(x * y for x, y in zip(va, vb))
    return dot / (math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(y * y for y in vb)))


@pytest.fixture(autouse=True)
def stub_models(monkeypatch):
    async def embed_one(text):
        return _fake_vector(text)

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)

    from config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)
    # A stub vector's cosine means nothing, so the floor is opened unless a
    # test is about the floor. A NULL similarity still cannot clear -1.0.
    monkeypatch.setattr(s, "rag_min_similarity", -1.0, raising=False)
    monkeypatch.setattr(s, "memory_hybrid_retrieval", False, raising=False)

    def set_cfg(**values):
        for key, value in values.items():
            monkeypatch.setattr(s, key, value, raising=False)

    return set_cfg


@pytest.fixture
def reranker(monkeypatch):
    """A stand-in cross-encoder that records what it was asked to score.

    It ranks by a marker word so the expected order is known, which is enough
    to show the reranker still reorders the candidates and never sees a
    text-only row.
    """
    seen: list[list[str]] = []

    async def rerank(query, docs):
        seen.append(list(docs))
        return [1.0 if "Friday" in d else 0.0 for d in docs]

    monkeypatch.setattr("memory.reranker.rerank", rerank)
    return seen


@pytest.fixture(autouse=True)
async def app_pool():
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
    """One tenant with two workspaces and one member in both."""
    w = World()
    w.tenant, w.user = uuid.uuid4(), uuid.uuid4()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                           w.tenant, f"textonly-{w.tenant.hex[:8]}")
        await conn.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                           w.user, f"{w.user.hex[:12]}@example.test")
        w.ws = await conn.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        org = await conn.fetchval(
            "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant)
        w.other_ws = uuid.uuid4()
        await conn.execute(
            "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
            "                        is_default, status) "
            "VALUES ($1,$2,$3,'Second','textonly-second',false,'active')",
            w.other_ws, w.tenant, org)
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", w.tenant, w.user)
        for ws in (w.ws, w.other_ws):
            await conn.execute(
                "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id,"
                " role, status) VALUES ($1,$2,$3,'workspace_admin','active')",
                w.tenant, ws, w.user)
        yield w
    finally:
        try:
            await conn.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", w.tenant)
            await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", w.tenant)
            await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", w.tenant)
            await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", w.tenant)
            await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", w.tenant)
            await conn.execute("DELETE FROM tenants WHERE id=$1", w.tenant)
            await conn.execute("DELETE FROM users WHERE id=$1", w.user)
        finally:
            await conn.close()


def _principal(world, ws=None) -> Principal:
    return Principal(user_id=world.user, tenant_id=world.tenant, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=ws or world.ws,
                     workspace_role="workspace_admin")


async def _save_text_only(p, content: str, monkeypatch) -> None:
    """Write through the real Memory Vault route with the embedder down."""
    import api.routes.memory as vault

    async def no_embedding(_content):
        return None

    monkeypatch.setattr(vault, "_embed_or_none", no_embedding)
    await vault.create_memory(vault.CreateMemoryRequest(content=content, kind="note"),
                              principal=p)

    # Guard: if this row had a vector, every test below would pass vacuously.
    async with workspace_connection(p) as bound:
        conn: Any = bound
        has_vector = await conn.fetchval(
            "SELECT embedding IS NOT NULL FROM memory_embeddings WHERE content = $1",
            content)
    assert has_vector is False, "the vault stored a vector; the row is not text-only"


async def _save_embedded(p, content: str) -> None:
    from memory.store import remember

    # The vault's namespace, so the vault's text-only rows are in the same scope.
    await remember(p, content, kind="note", user_id=VAULT_NAMESPACE)


# ── The keyword half, in isolation ──────────────────────────────────────────

async def test_hybrid_keyword_half_skips_a_text_only_row_naming_every_term(
        world, stub_models, monkeypatch):
    """The case reported against `_retrieve_hybrid`.

    The vector half is crowded: there are at least as many embedded rows as it
    fetches, so the text-only row (sorted last, with a NULL distance) falls past
    its LIMIT. Only the keyword half can return the text-only row here, so a
    failure in this test is a failure in that half.
    """
    from memory.store import retrieve

    p = _principal(world)
    fetch_n = 3
    await _save_embedded(p, EMBEDDED)
    for i in range(fetch_n):
        await _save_embedded(p, f"unrelated note number {i}")
    await _save_text_only(p, TEXT_ONLY, monkeypatch)
    # Same terms, another workspace of the same tenant: RLS must still hide it.
    await _save_embedded(_principal(world, world.other_ws), "Kestrel invoice in workspace two")

    stub_models(memory_hybrid_retrieval=True, rag_candidate_k=fetch_n)
    hits = await retrieve(p, QUERY, user_id=VAULT_NAMESPACE, top_k=fetch_n)

    contents = [h["content"] for h in hits]
    assert TEXT_ONLY not in contents
    assert "Kestrel invoice in workspace two" not in contents
    kestrel = next(h for h in hits if h["content"] == EMBEDDED)
    assert "keyword" in kestrel["retrievers"]
    assert all(isinstance(h["similarity"], float) for h in hits)


# ── A small scope, where both vector queries reach the text-only row ────────

@pytest.mark.parametrize("hybrid", [False, True], ids=["vector-only", "hybrid"])
@pytest.mark.parametrize("rerank", [False, True], ids=["no-rerank", "rerank"])
async def test_a_small_scope_with_a_text_only_row_still_recalls(
        world, stub_models, reranker, monkeypatch, hybrid, rerank):
    """Fewer embedded rows than the query fetches, so the vector query returns
    the text-only row too. That is the default path (`memory_hybrid_retrieval`
    off) as well as the vector half of hybrid retrieval.

    With the reranker on, it still reorders the candidates (the "Friday" row
    scores highest) and is never asked to score the text-only row.
    """
    from memory.store import retrieve

    p = _principal(world)
    await _save_embedded(p, "a note about the garden")
    await _save_embedded(p, EMBEDDED)
    await _save_text_only(p, "a text-only note with none of the query terms", monkeypatch)

    stub_models(memory_hybrid_retrieval=hybrid, reranker_enabled=rerank)
    hits = await retrieve(p, QUERY, user_id=VAULT_NAMESPACE, top_k=5)

    contents = [h["content"] for h in hits]
    assert sorted(contents) == sorted([EMBEDDED, "a note about the garden"])
    assert all(isinstance(h["similarity"], float) for h in hits)
    if rerank:
        assert contents[0] == EMBEDDED
        assert all("rerank_score" in h for h in hits)
        assert reranker and all(
            "text-only" not in doc for docs in reranker for doc in docs)
    else:
        assert reranker == []


# ── The floor is unchanged ──────────────────────────────────────────────────

async def test_hybrid_floor_still_drops_a_weak_embedded_keyword_match(
        world, stub_models, monkeypatch):
    """Turning hybrid on can add a memory that names the query's terms. It
    cannot add one the cosine floor rejects, and the text-only row is the
    limiting case of that rule.

    With the stub, a row whose text equals the query has cosine 1.0, and any
    other text scores near zero (checked here, not assumed).
    """
    from memory.store import retrieve

    floor = 0.9
    assert _cosine(QUERY, EMBEDDED) < floor, "stub vectors collided; pick other strings"

    p = _principal(world)
    await _save_embedded(p, QUERY)
    await _save_embedded(p, EMBEDDED)
    await _save_text_only(p, TEXT_ONLY, monkeypatch)

    stub_models(memory_hybrid_retrieval=True, rag_min_similarity=floor)
    hits = await retrieve(p, QUERY, user_id=VAULT_NAMESPACE, top_k=5)

    assert [h["content"] for h in hits] == [QUERY]
    assert hits[0]["similarity"] >= floor
