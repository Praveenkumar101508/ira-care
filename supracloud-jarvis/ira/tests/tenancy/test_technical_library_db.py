"""Technical Library against real PostgreSQL: the SQL, RLS and the namespaces.

`tests/test_technical_library.py` proves ingestion and fusion logic with fakes.
This file proves the parts a fake cannot: that the full-text query 088 indexes
actually matches, that library rows are invisible to personal memory and
self-knowledge retrieval (and the other way round), and that a second tenant
sees none of it.

Real PostgreSQL, real RLS, as `ira_app` — non-superuser, NOBYPASSRLS. The
EMBEDDING MODEL is stubbed with a deterministic pseudo-vector, exactly as
`test_issue_55_cloud_memory_vault.py` does: the question here is where rows
land and who can read them, not whether the vectors are any good. That is also
why the similarity floors are set explicitly per test — a stub vector's cosine
means nothing, so each test says which retriever it is exercising.
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

from core.editions import EDITION_CLOUD  # noqa: E402
from core.identity.principal import Principal  # noqa: E402
from memory import technical_library as tl  # noqa: E402
from tests.fixtures.technical_library.corpus import build_corpus  # noqa: E402

EMBED_DIM = 1024


def _fake_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def stub_models(monkeypatch):
    async def embed_one(text):
        return _fake_vector(text)

    async def embed(texts):
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed", embed, raising=False)

    from config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "reranker_enabled", False, raising=False)
    monkeypatch.setattr(s, "rag_min_similarity", -1.0, raising=False)
    monkeypatch.setattr(s, "self_knowledge_min_similarity", -1.0, raising=False)

    def set_cfg(**values):
        """Settings is process-wide; every change goes through monkeypatch so
        it is undone when the test ends."""
        for key, value in values.items():
            monkeypatch.setattr(s, key, value, raising=False)

    return set_cfg


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
    """Two tenants, one default workspace each, one member in each."""
    w = World()
    w.tenant, w.other_tenant = uuid.uuid4(), uuid.uuid4()
    w.user, w.other_user = uuid.uuid4(), uuid.uuid4()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for t, u in ((w.tenant, w.user), (w.other_tenant, w.other_user)):
            await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                               t, f"techlib-{t.hex[:8]}")
            await conn.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                               u, f"{u.hex[:12]}@example.test")
        w.ws = await conn.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        w.other_ws = await conn.fetchval("SELECT provision_tenant_substrate($1)", w.other_tenant)
        for t, u, ws in ((w.tenant, w.user, w.ws), (w.other_tenant, w.other_user, w.other_ws)):
            await conn.execute(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
                "VALUES ($1,$2,'member','active')", t, u)
            await conn.execute(
                "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id,"
                " role, status) VALUES ($1,$2,$3,'workspace_admin','active')", t, ws, u)
        yield w
    finally:
        try:
            for t in (w.tenant, w.other_tenant):
                await conn.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenants WHERE id=$1", t)
            for u in (w.user, w.other_user):
                await conn.execute("DELETE FROM users WHERE id=$1", u)
        finally:
            await conn.close()


def _principal(user, tenant, ws) -> Principal:
    return Principal(user_id=user, tenant_id=tenant, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=ws,
                     workspace_role="workspace_admin")


@pytest.fixture
async def library(world, tmp_path):
    p = _principal(world.user, world.tenant, world.ws)
    docs, report = tl.build_plan(build_corpus(tmp_path, include_huge=False))
    await tl.ingest(p, docs, report)
    return p, report


# ── The SQL works ────────────────────────────────────────────────────────────

async def test_ingest_lands_in_the_workspace_and_status_counts_it(library):
    p, report = library
    status = await tl.library_status(p)
    assert status["chunks"] == report.chunks_written > 0
    assert status["documents"] == report.documents_written


async def test_keyword_retrieval_finds_an_exact_term_through_postgres_fts(library, stub_models):
    """The vector list is floored away entirely, so any hit came from the
    full-text query 088 indexes."""
    p, _ = library
    stub_models(technical_library_min_similarity=2.0)
    hits = await tl.retrieve_technical(p, "ef_search", top_k=3)
    assert hits, "the full-text query found nothing for a term the corpus contains"
    assert hits[0]["title"] == "hnsw index notes"
    assert hits[0]["retrievers"] == ["keyword"]
    assert hits[0]["citation"] == "[hnsw index notes, part 1]"


async def test_hybrid_reports_which_retrievers_found_each_chunk(library, stub_models):
    p, _ = library
    stub_models(technical_library_min_similarity=-1.0)
    hits = await tl.retrieve_technical(p, "DENSE_RANK ties gaps", top_k=5)
    assert all(set(h["retrievers"]) <= {"vector", "keyword"} for h in hits)
    assert any("keyword" in h["retrievers"] for h in hits)
    sql_hit = next(h for h in hits if h["title"] == "SQL Aggregation and Windows")
    assert sql_hit["page"] == 2 and sql_hit["citation"] == "[SQL Aggregation and Windows, p.2]"


async def test_reingesting_against_the_database_writes_nothing(library, tmp_path_factory):
    p, first = library
    docs, again = tl.build_plan(build_corpus(tmp_path_factory.mktemp("again"), include_huge=False))
    await tl.ingest(p, docs, again)
    assert again.chunks_written == 0
    assert again.documents_unchanged == first.documents_written


async def test_ocr_rewrites_a_document_indexed_from_text_and_is_then_stable(
        world, tmp_path, stub_models, monkeypatch):
    """The `extraction` key read back through the real query (COALESCE for rows
    written before it existed), not a fake table's idea of it."""
    from memory import pdf_ocr
    from tests.fixtures.technical_library.corpus import SCANNED_PAGE, SQL_PAGE_1, write_pdf

    p = _principal(world.user, world.tenant, world.ws)
    write_pdf(tmp_path / "Scans.pdf", [SQL_PAGE_1, "Figure 2"])
    docs, first = tl.build_plan(tmp_path)
    await tl.ingest(p, docs, first)

    tools = pdf_ocr.OcrTools(pdftoppm="/stub/pdftoppm", tesseract="/stub/tesseract")
    monkeypatch.setattr(pdf_ocr, "find_tools", lambda: tools)
    monkeypatch.setattr(pdf_ocr, "ocr_page", lambda path, page, **k: SCANNED_PAGE)
    ocr = tl.LibraryOptions(ocr=True)

    docs, second = tl.build_plan(tmp_path, opts=ocr)
    await tl.ingest(p, docs, second)
    assert second.documents_replaced == 1
    assert second.chunks_deleted == first.chunks_written

    docs, third = tl.build_plan(tmp_path, opts=ocr)
    await tl.ingest(p, docs, third)
    docs, plain = tl.build_plan(tmp_path)
    await tl.ingest(p, docs, plain)
    assert third.chunks_written == plain.chunks_written == 0
    assert third.documents_unchanged == plain.documents_unchanged == 1

    stub_models(technical_library_min_similarity=2.0)
    hits = await tl.retrieve_technical(p, "Bloom filter", top_k=3)
    assert hits and hits[0]["page"] == 2 and hits[0]["citation"] == "[Scans, p.2]"


# ── Namespaces and tenants ───────────────────────────────────────────────────

async def test_library_rows_never_reach_personal_memory_retrieval(library):
    from memory.store import retrieve

    p, _ = library
    for user_id in ("owner", "system", str(p.user_id)):
        hits = await retrieve(p, "ef_search candidate list", user_id=user_id, top_k=20)
        assert not any("ef_search" in h["content"] for h in hits), user_id


async def test_library_rows_never_reach_self_knowledge(library):
    from memory.self_knowledge import retrieve_self_knowledge

    p, _ = library
    hits = await retrieve_self_knowledge(p, "ef_search candidate list", top_k=20)
    assert not any("ef_search" in h["content"] for h in hits)


async def test_personal_and_self_knowledge_rows_never_reach_the_library(library, stub_models):
    from core.identity.db_context import workspace_connection
    from memory.store import remember

    p, _ = library
    await remember(p, "my private note mentions ef_search too", kind="note", user_id="owner")
    async with workspace_connection(p) as conn:
        await conn.execute(
            """INSERT INTO memory_embeddings
               (source_id, source_type, content, embedding, user_id, metadata, tenant_id, workspace_id)
               VALUES (gen_random_uuid(), 'self_knowledge', 'owner cv says ef_search',
                       $1::vector, 'self_knowledge', '{}'::jsonb,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid)""",
            "[" + ",".join(map(str, _fake_vector("x"))) + "]",
        )
    stub_models(technical_library_min_similarity=-1.0)
    hits = await tl.retrieve_technical(p, "ef_search", top_k=30)
    assert hits
    assert not any("private note" in h["content"] or "owner cv" in h["content"] for h in hits)


async def test_another_tenant_sees_none_of_the_library(library, world, stub_models):
    stub_models(technical_library_min_similarity=-1.0)
    other = _principal(world.other_user, world.other_tenant, world.other_ws)
    assert await tl.retrieve_technical(other, "ef_search", top_k=10) == []
    assert (await tl.library_status(other))["chunks"] == 0


# ── The personal-memory hybrid flag ──────────────────────────────────────────

async def test_personal_hybrid_flag_off_keeps_the_vector_only_shape(world, stub_models):
    from memory.store import remember, retrieve

    p = _principal(world.user, world.tenant, world.ws)
    await remember(p, "the invoice for Kestrel is due Friday", kind="note", user_id="owner")
    stub_models(memory_hybrid_retrieval=False)
    hits = await retrieve(p, "Kestrel invoice", user_id="owner", top_k=5)
    assert hits and all("retrievers" not in h for h in hits)


async def test_personal_hybrid_flag_on_fuses_keyword_hits(world, stub_models):
    from memory.store import remember, retrieve

    p = _principal(world.user, world.tenant, world.ws)
    await remember(p, "the invoice for Kestrel is due Friday", kind="note", user_id="owner")
    stub_models(memory_hybrid_retrieval=True)
    hits = await retrieve(p, "Kestrel invoice", user_id="owner", top_k=5)
    kestrel = next(h for h in hits if "Kestrel" in h["content"])
    assert "keyword" in kestrel["retrievers"]
