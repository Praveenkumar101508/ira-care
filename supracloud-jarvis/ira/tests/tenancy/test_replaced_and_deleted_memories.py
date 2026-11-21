"""Replaced and deleted memories: the vault shows current facts, forget takes
the history with it, and chat recalls neither.

── The defects ──────────────────────────────────────────────────────────────

Since #60 a "remember ..." that conflicts with a Memory Vault fact can be
resolved with "Replace old". `memory.temporal.supersede` then keeps the old row,
marked `superseded_by` the new one. Three things went wrong from there
(reproduced on PostgreSQL, 19 October):

  * the vault went on listing the replaced version, with nothing to say so;
  * forgetting the current version failed: the old row's foreign key still
    pointed at it;
  * chat recall had no `superseded_by` filter, so IRA could still answer with
    the version the owner had corrected. `memory/temporal.py` documents the
    opposite ("excluded from active retrieval").

And one with the same shape: the privacy page's "delete memory" only marks
`deleted_at`, and recall had no filter for that either, so a deleted memory
stayed recallable until retention, which is off by default, removed it.

── What the owner decided (19 October) ──────────────────────────────────────

The vault lists only current facts. Forgetting one removes it together with
every earlier version it replaced, and the confirmation says how many. Recall
skips deleted, replaced and expired rows, the predicate
`memory.temporal.ACTIVE_FILTER_SQL` names.

── What is real ─────────────────────────────────────────────────────────────

Real PostgreSQL as `ira_app` with RLS, the real workspace selection, the real
vault router over ASGI, the real `memory/store.retrieve` (vector and hybrid) and
`core/privacy/store.delete_memory`. Only the embedding model is stubbed, as in
`test_vault_chat_recall.py`.
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

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
QUESTION = "When is the synthetic project deadline?"


def _fake_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


def _vec(text: str) -> str:
    return "[" + ",".join(map(str, _fake_vector(text))) + "]"


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    async def embed_one(text: str):
        return _fake_vector(text)

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)
    from config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reranker_enabled", False, raising=False)
    monkeypatch.setattr(settings, "rag_min_similarity", -1.0, raising=False)
    # Every seeded row must be within reach of the vector search, so a row
    # missing from the answer is missing because it was filtered.
    monkeypatch.setattr(settings, "rag_top_k", 50, raising=False)
    monkeypatch.setattr(settings, "rag_candidate_k", 50, raising=False)


@pytest.fixture(params=["vector", "hybrid"])
def retrieval_mode(request, monkeypatch):
    from config import get_settings

    monkeypatch.setattr(get_settings(), "memory_hybrid_retrieval",
                        request.param == "hybrid", raising=False)
    return request.param


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
async def world(monkeypatch):
    """One private-edition tenant; the owner is workspace_admin of its default
    workspace. Rows are seeded through the admin DSN with fixed embeddings."""
    w = World()
    w.tenant, w.owner = uuid.uuid4(), uuid.uuid4()
    tag = w.tenant.hex[:8]
    w.ids = {}

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                            w.tenant, f"replaced-{tag}")
        await admin.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                            w.owner, f"{w.owner.hex[:12]}@example.test")
        w.ws = await admin.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", w.tenant, w.owner)
        await admin.execute(
            "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", w.tenant, w.ws, w.owner)

        async def add(label, content, *, user_id=VAULT_NAMESPACE, source_type="fact",
                      superseded_by=None, deleted=False):
            w.ids[label] = uuid.uuid4()
            await admin.execute(
                "INSERT INTO memory_embeddings (id, tenant_id, workspace_id, user_id,"
                " source_type, source, memory_type, content, embedding, superseded_by,"
                " deleted_at) VALUES ($1,$2,$3,$4,$5,'owner_chat',$6,$7,$8::vector,$9,"
                " CASE WHEN $10 THEN NOW() END)",
                w.ids[label], w.tenant, w.ws, user_id, source_type,
                "superseded_fact" if superseded_by else "fact", content, _vec(content),
                w.ids.get(superseded_by) if superseded_by else None, deleted)

        # Newest first, so each older version can point at its replacement.
        await add("deadline now", f"The synthetic project deadline is 22 July ({tag})")
        await add("deadline before", f"The synthetic project deadline is 18 July ({tag})",
                  superseded_by="deadline now")
        await add("venue now", f"The synthetic venue is Leeds ({tag})")
        await add("venue before", f"The synthetic venue is York ({tag})",
                  superseded_by="venue now")
        await add("venue first", f"The synthetic venue is Hull ({tag})",
                  superseded_by="venue before")
        await add("standalone", f"The synthetic project sponsor is Dana ({tag})")
        await add("deleted on privacy page",
                  f"The synthetic project deadline was secret-{tag}", deleted=True)
        w.content = {}
        for label, i in w.ids.items():
            w.content[label] = await admin.fetchval(
                "SELECT content FROM memory_embeddings WHERE id = $1", i)

        w.principal = await _bind(monkeypatch, Principal(
            user_id=w.owner, tenant_id=w.tenant, session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE, roles=frozenset({"primary_owner"}),
            username=OWNER_USERNAME, is_owner=True), w.ws)
        w.admin = admin
        yield w
    finally:
        try:
            await admin.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM workspaces WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM organisations WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", w.tenant)
            await admin.execute("DELETE FROM users WHERE id=$1", w.owner)
        finally:
            await admin.close()


async def _bind(monkeypatch, unscoped: Principal, workspace_id) -> Principal:
    import api.middleware.workspace as ws_module

    async def fake_resolve(username, **kw):
        return unscoped

    monkeypatch.setattr(ws_module, "resolve_principal", fake_resolve)
    bound = await ws_module.resolve_workspace_principal(unscoped.username, str(workspace_id))
    assert bound.workspace_id == workspace_id
    return bound


def _vault(principal: Principal):
    import httpx
    from fastapi import FastAPI

    from api.middleware.workspace import require_workspace_principal
    from api.routes.memory import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_workspace_principal] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://vault.test")


async def _forget(principal: Principal, memory_id: uuid.UUID) -> tuple[str, dict]:
    """Draft, then confirm — the vault screen's two steps."""
    async with _vault(principal) as client:
        draft = await client.delete(f"/api/v1/memory/{memory_id}")
        assert draft.status_code == 200, draft.text
        assert draft.json()["status"] == "confirmation_required"
        done = await client.delete(f"/api/v1/memory/{memory_id}",
                                   params={"confirm_token": draft.json()["token"]})
        assert done.status_code == 200, done.text
    return draft.json()["preview"], done.json()


async def _left(w) -> set[str]:
    rows = await w.admin.fetch("SELECT id FROM memory_embeddings WHERE tenant_id=$1", w.tenant)
    by_id = {i: label for label, i in w.ids.items()}
    return {by_id[r["id"]] for r in rows}


# ── The vault shows current facts ────────────────────────────────────────────


async def test_the_vault_lists_only_current_facts(world):
    async with _vault(world.principal) as client:
        listed = await client.get("/api/v1/memory", params={"limit": 200})
    assert listed.status_code == 200, listed.text
    shown = {m["content"] for m in listed.json()["memories"]}
    c = world.content

    assert {c["deadline now"], c["venue now"], c["standalone"]} <= shown
    assert not {c["deadline before"], c["venue before"], c["venue first"]} & shown, (
        "the vault listed a version the owner replaced")
    assert c["deleted on privacy page"] not in shown, "the vault listed a deleted memory"


# ── Forget takes the history with it ─────────────────────────────────────────


async def test_forgetting_a_replaced_fact_removes_the_version_it_replaced(world):
    preview, done = await _forget(world.principal, world.ids["deadline now"])

    assert "and the 1 earlier version it replaced" in preview
    assert done["earlier_versions_deleted"] == 1
    left = await _left(world)
    assert not {"deadline now", "deadline before"} & left
    assert {"venue now", "venue before", "venue first", "standalone"} <= left, (
        "forget reached rows outside the fact's own history")


async def test_a_longer_history_goes_whole(world):
    preview, done = await _forget(world.principal, world.ids["venue now"])

    assert "and the 2 earlier versions it replaced" in preview
    assert done["earlier_versions_deleted"] == 2
    assert not {"venue now", "venue before", "venue first"} & await _left(world)


async def test_a_fact_with_no_history_is_forgotten_alone(world):
    preview, done = await _forget(world.principal, world.ids["standalone"])

    assert "earlier version" not in preview
    assert done["earlier_versions_deleted"] == 0
    assert await _left(world) == set(world.ids) - {"standalone"}


# ── Chat recalls only active memories ────────────────────────────────────────


async def _recall(principal) -> list[str]:
    from memory.store import retrieve

    return [m["content"] for m in await retrieve(principal, QUESTION, user_id=OWNER_USERNAME)]


async def test_chat_recalls_the_current_fact_not_the_replaced_one(world, retrieval_mode):
    recalled = await _recall(world.principal)
    c = world.content

    assert c["deadline now"] in recalled, "positive control: the current fact is recalled"
    assert c["deadline before"] not in recalled, (
        f"{retrieval_mode}: chat recalled a fact the owner replaced")
    assert not {c["venue before"], c["venue first"]} & set(recalled)


async def test_chat_does_not_recall_a_memory_deleted_on_the_privacy_page(world, retrieval_mode):
    from core.privacy.store import delete_memory

    c = world.content
    assert c["deleted on privacy page"] not in await _recall(world.principal)

    # The same, through the privacy route's own function, on a live row.
    assert c["standalone"] in await _recall(world.principal), "positive control"
    assert await delete_memory(world.principal, world.ids["standalone"])
    assert c["standalone"] not in await _recall(world.principal), (
        f"{retrieval_mode}: chat recalled a memory deleted on the privacy page")
