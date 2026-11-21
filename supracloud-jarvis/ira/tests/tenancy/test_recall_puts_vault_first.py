"""Recall puts relevant Memory Vault facts ahead of chat transcripts.

── The defect ───────────────────────────────────────────────────────────────

Chat recall ranked the owner's vault and their chat transcripts together. On a
copy of a real install upgraded from 18 October
(`docs/testing/evidence/upgrade-089-2026-10-19/`), a fact saved in the vault
came fifth of five for the question it answers: two transcript copies of the
question and two of IRA's earlier "there is no such project" answers ranked
above it, and the model repeated the denial. With more chat history the fact
would not have reached the 20-row candidate pool, so it would not have been
recalled at all.

── What this file proves ────────────────────────────────────────────────────

With 25 transcript rows closer to the question than the vault fact:

  * the relevant vault fact is recalled, first, and marked as the vault's;
  * a vault entry below the similarity floor is still left out, so "first"
    never means "every vault entry in every answer";
  * the transcripts are left out of that answer (the owner's decision of
    20 October): with the fact on top and the transcripts below it the model
    still repeated IRA's own earlier denial; without them it answered from
    the fact (`docs/testing/evidence/recall-vault-first-2026-10-20/`);
  * anything else filed under the owner's name, here a note, still competes
    for the remaining places;
  * a question no vault fact answers recalls the transcripts as before;
  * both prompt builders keep that order: `select_memory_context` (the
    streaming and architect paths) and the agent graph's `retrieve_memory`;
  * all of it in the vector and hybrid retrievers, with the reranker off and
    with a reranker that prefers the transcripts. The transcripts name every
    term of the question, so the hybrid retriever's keyword half reaches them
    too and must leave them out as well.

Real PostgreSQL as `ira_app` with RLS, the real `memory/store.py`. The rows'
embeddings are written as explicit vectors and the query's embedding is
stubbed, so every similarity is known exactly; the real floor (0.6) applies.
"""
from __future__ import annotations

import math
import os
import sys
import uuid
from typing import Any

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# conftest stubs `sentence_transformers`; `memory.reranker` imports CrossEncoder
# by name. Nothing here instantiates either: the reranker is replaced below.
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

from core.editions import EDITION_PRIVATE  # noqa: E402
from core.identity.principal import Principal  # noqa: E402
from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

EMBED_DIM = 1024
OWNER_USERNAME = "admin"
QUESTION = "What is the synthetic test project codename?"
#: A question no vault fact answers.
OTHER_QUESTION = "What did we talk about earlier?"

FACT = "The synthetic test project codename is vault-first-7f3a."
UNRELATED_VAULT = "SYNTHETIC: the owner's favourite colour is teal."
NOTE = "SYNTHETIC note filed under the owner's name: the launch review is on Friday."
TRANSCRIPTS = [
    f"SYNTHETIC test transcript {i}: there is no known project with that codename."
    for i in range(25)
]


def _unit(*weights: tuple[int, float]) -> str:
    """A vector literal with the given (dimension, weight) entries."""
    v = [0.0] * EMBED_DIM
    for dim, w in weights:
        v[dim] = w
    return "[" + ",".join(map(str, v)) + "]"


#: The question's embedding: dimension 0.
QUERY_VEC = _unit((0, 1.0))
#: Every transcript is as close to the question as a row can be (similarity 1).
TRANSCRIPT_VEC = QUERY_VEC
#: The fact: cosine similarity 0.7 with the question, above the 0.6 floor.
FACT_VEC = _unit((0, 0.7), (1, math.sqrt(1 - 0.49)))
#: An unrelated vault entry: similarity 0, below the floor.
UNRELATED_VEC = _unit((2, 1.0))
#: A note under the owner's name, not a transcript: similarity 0.9.
NOTE_VEC = _unit((0, 0.9), (3, math.sqrt(1 - 0.81)))
#: The other question: transcripts 0.8, note 0.72, the fact 0.13 (below the
#: floor), the unrelated entry 0. No vault fact answers it.
OTHER_QUERY_VEC = _unit((0, 0.8), (1, -0.6))


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def recall_settings(monkeypatch):
    """The production recall settings, stated so the test does not depend on them
    drifting, and the query's embedding fixed."""
    vectors = {QUESTION: QUERY_VEC, OTHER_QUESTION: OTHER_QUERY_VEC}

    async def embed_one(text: str):
        return [float(x) for x in vectors[text].strip("[]").split(",")]

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    from config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "rag_min_similarity", 0.6, raising=False)
    monkeypatch.setattr(s, "rag_top_k", 5, raising=False)
    monkeypatch.setattr(s, "rag_candidate_k", 20, raising=False)


@pytest.fixture(params=["vector", "hybrid"])
def retrieval_mode(request, monkeypatch):
    from config import get_settings

    monkeypatch.setattr(get_settings(), "memory_hybrid_retrieval",
                        request.param == "hybrid", raising=False)
    return request.param


@pytest.fixture(params=["reranker-off", "reranker-prefers-transcripts"])
def reranker(request, monkeypatch):
    """Off, or a stand-in cross-encoder that scores every transcript above the
    fact: the ordering must not depend on the reranker agreeing."""
    from config import get_settings

    on = request.param != "reranker-off"
    monkeypatch.setattr(get_settings(), "reranker_enabled", on, raising=False)
    if on:
        async def rerank(query: str, docs: list[str]) -> list[float]:
            return [1.0 if d in (FACT, UNRELATED_VAULT) else 10.0 for d in docs]

        monkeypatch.setattr("memory.reranker.rerank", rerank)
    return request.param


@pytest.fixture
async def owner(monkeypatch):
    """A private owner bound to their own workspace, with the rows above."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                           tenant, f"vault-first-{tenant.hex[:8]}")
        await conn.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                           user, f"{user.hex[:12]}@example.test")
        ws = await conn.fetchval("SELECT provision_tenant_substrate($1)", tenant)
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", tenant, user)
        await conn.execute(
            "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", tenant, ws, user)
        rows = [(VAULT_NAMESPACE, "projects", FACT, FACT_VEC),
                (VAULT_NAMESPACE, "preferences", UNRELATED_VAULT, UNRELATED_VEC)]
        rows += [(OWNER_USERNAME, "note", NOTE, NOTE_VEC)]
        rows += [(OWNER_USERNAME, "message", t, TRANSCRIPT_VEC) for t in TRANSCRIPTS]
        for user_id, source_type, content, vec in rows:
            await conn.execute(
                "INSERT INTO memory_embeddings (tenant_id, workspace_id, user_id, "
                "source_type, content, embedding) VALUES ($1,$2,$3,$4,$5,$6::vector)",
                tenant, ws, user_id, source_type, content, vec)

        import api.middleware.workspace as ws_module

        unscoped = Principal(
            user_id=user, tenant_id=tenant, session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE, roles=frozenset({"primary_owner"}),
            username=OWNER_USERNAME, is_owner=True,
        )

        async def fake_resolve(username, **kw):
            return unscoped

        monkeypatch.setattr(ws_module, "resolve_principal", fake_resolve)
        bound = await ws_module.resolve_workspace_principal(OWNER_USERNAME, str(ws))
        assert bound.workspace_id == ws and bound.is_owner
        yield bound
    finally:
        await conn.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await conn.execute("DELETE FROM users WHERE id=$1", user)
        await conn.close()


async def _recall(principal, question: str = QUESTION) -> list[dict]:
    from memory.store import retrieve

    return await retrieve(principal, question, user_id=OWNER_USERNAME)


async def test_the_vault_fact_comes_first_however_many_transcripts_outscore_it(
        owner, retrieval_mode, reranker):
    results = await _recall(owner)

    assert [r["content"] for r in results][:1] == [FACT], (
        f"the saved fact was not recalled first ({retrieval_mode}, {reranker}): "
        f"{[r['content'][:40] for r in results]}")
    assert results[0].get("vault") is True
    assert not any(r.get("vault") for r in results[1:])


async def test_transcripts_are_left_out_when_a_vault_fact_answers(
        owner, retrieval_mode, reranker):
    contents = [r["content"] for r in await _recall(owner)]

    assert not set(contents) & set(TRANSCRIPTS), (
        f"chat transcripts were recalled beside the vault fact ({retrieval_mode}, "
        f"{reranker}); with them the model repeated IRA's old denial")
    assert contents == [FACT, NOTE], (
        "only transcripts go: a note filed under the owner's name still follows "
        f"the fact ({contents})")


async def test_a_question_no_vault_fact_answers_recalls_transcripts_as_before(
        owner, retrieval_mode, reranker):
    results = await _recall(owner, OTHER_QUESTION)

    assert len(results) == 5, results
    assert all(r["content"] in TRANSCRIPTS for r in results), (
        "with no relevant vault fact, recall must be unchanged: the transcripts "
        f"are the closest rows ({[r['content'][:40] for r in results]})")
    assert not any(r.get("vault") for r in results)


async def test_a_vault_entry_below_the_floor_is_still_left_out(owner, retrieval_mode, reranker):
    contents = [r["content"] for r in await _recall(owner)]

    assert UNRELATED_VAULT not in contents, (
        "an unrelated vault entry was recalled: first must mean first among "
        "relevant memories, not every vault entry in every answer")


async def test_chats_prompt_block_keeps_the_fact_first(owner, retrieval_mode, reranker):
    """`select_memory_context` re-sorts and trims to 2,000 characters; by score
    alone it would put the fact back last, where the trim cuts first."""
    from reasoning.memory_context import select_memory_context

    block = select_memory_context(await _recall(owner))
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]

    assert lines and lines[0] == f"- {FACT}", lines[:2]
    assert not any("transcript" in ln for ln in lines), lines


async def test_the_agent_graphs_prompt_block_keeps_the_fact_first(
        owner, retrieval_mode, reranker):
    from agents.graph import CONFIG_PRINCIPAL_KEY, retrieve_memory

    turn: Any = {"user_query": QUESTION, "user_id": OWNER_USERNAME}
    state = await retrieve_memory(turn, {"configurable": {CONFIG_PRINCIPAL_KEY: owner}})
    memory_lines = [ln for ln in state["memory_context"].splitlines() if ln.startswith("[")]

    assert memory_lines and memory_lines[0].endswith(FACT), memory_lines[:2]
    assert not any(ln.startswith("[message") for ln in memory_lines), memory_lines
