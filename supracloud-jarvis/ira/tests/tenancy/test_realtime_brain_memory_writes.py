"""The realtime brain's long-term memory writes land, and as whom.

── The defect ───────────────────────────────────────────────────────────────

`agents/cortex_realtime_brain.py::IraMemorySink.remember` called
`memory.store.remember(text, kind=..., user_id=...)`. Since commit 19c3415 the
store's signature is `remember(principal, content, *, kind, user_id)`: the
thought arrived as the principal, `content` was missing, and every call raised
TypeError. `_deliberate` and `consolidate` caught it and logged it at DEBUG,
so no brain memory was ever stored and nothing said so. The unit tests in
`tests/test_realtime_brain.py` give the brain a recording sink, so they could
not see it.

── The identity (protected: the owner decides) ──────────────────────────────

A background loop has no request, so no request principal. The brain writes
as `agents.cortex_realtime_brain.brain_memory_principal()` resolves at write
time: the private tenant's canonical default workspace (the one
`core/identity/provisioning.py` gives the `.env` owner), through
`default_workspace_job_principal`, the path the heartbeat, the briefing and the
weekly reflection already use. Nothing is read from a percept. In the cloud
edition, or when that workspace cannot be resolved, nothing is written and a
WARNING says why, without the text.

── What is real ─────────────────────────────────────────────────────────────

Real PostgreSQL as `ira_app` (NOSUPERUSER, NOBYPASSRLS, RLS forced), the real
`IraMemorySink`, the real `memory.store.remember` and `retrieve`, the real
background identity path and the private tenant the migrations create. Two
things are replaced: the LLM, by a fixed JSON reply, and the embedding model,
by a deterministic hash vector as in `test_vault_chat_recall.py`. So these
tests show which rows land and which rows recall can reach. They say nothing
about how well a real embedding ranks them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# conftest stubs `sentence_transformers` without the attributes the import
# chain reaches for; nothing here instantiates them.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
if _st is not None and not hasattr(_st, "CrossEncoder"):
    setattr(_st, "CrossEncoder", object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from agents.cortex_realtime_brain import IraMemorySink, RealtimeBrain  # noqa: E402
from core.editions import EDITION_PRIVATE  # noqa: E402
from core.identity.principal import (  # noqa: E402
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
)
from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

#: `memory_embeddings.embedding` is `vector(1024)`.
EMBED_DIM = 1024

OWNER_USERNAME = "admin"
DELEGATE_USERNAME = "family-member"


def _fake_vector(text: str) -> list[float]:
    """Deterministic pseudo-embedding: same text, same vector."""
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    """Replace the model, keep the SQL. The reranker's stub cannot be built,
    and it only reorders rows the SQL already chose, so it is switched off."""
    async def embed_one(text: str):
        return _fake_vector(text)

    monkeypatch.setattr("memory.store.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)

    from config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reranker_enabled", False, raising=False)
    monkeypatch.setattr(settings, "memory_hybrid_retrieval", False, raising=False)
    # Hash vectors are not semantic; the floor is not what this file tests.
    monkeypatch.setattr(settings, "rag_min_similarity", -1.0, raising=False)


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    """The brain runs only in the private edition; tests that need the cloud
    edition set it themselves."""
    monkeypatch.setenv("IRA_EDITION", EDITION_PRIVATE)


@pytest.fixture(autouse=True)
async def app_pool():
    """Point `utils.db.acquire()` at the test database as `ira_app`."""
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


@pytest.fixture
async def owner_workspace():
    """The private tenant's default workspace, as the migrations created it.

    This is where the owner's chat reads (`legacy_owner_workspace`) and where
    the brain should write. Read as the superuser so RLS cannot hide it.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id FROM workspaces WHERE tenant_id = $1 AND is_default "
            "AND status = 'active'", PRIVATE_TENANT_ID)
    finally:
        await conn.close()
    assert len(rows) == 1, (
        "the private tenant has no single active default workspace; "
        "039_workspace_substrate.sql should have created it")
    return rows[0]["id"]


@pytest.fixture
async def thought():
    """A unique thought, and the removal of every row that carries it."""
    text = f"SYNTHETIC brain thought {uuid.uuid4().hex[:12]}: the owner wants the GPU watched"
    yield text
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("DELETE FROM memory_embeddings WHERE content = $1", text)
    finally:
        await conn.close()


class _FixedLLM:
    """System 2 as a fixed JSON reply: one thought, important enough to keep."""

    def __init__(self, thought: str, importance: float = 0.9):
        self.reply = json.dumps({"thought": thought, "speak": "", "new_goal": "",
                                 "importance": importance})

    async def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        return self.reply


async def _think(thought: str) -> None:
    """One idle deliberation of a brain wired with the real sink, as
    `api/routes/brain.py::start_brain` wires it."""
    brain = RealtimeBrain(llm=_FixedLLM(thought), memory=IraMemorySink())
    await brain._deliberate(None, "idle")


async def _rows(content: str) -> list:
    """Every row with this content, read as the superuser, so RLS cannot hide
    a row that did land."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return await conn.fetch(
            "SELECT tenant_id, workspace_id, user_id, source_type, owner_confirmed,"
            "       embedding IS NOT NULL AS has_vector"
            "  FROM memory_embeddings WHERE content = $1", content)
    finally:
        await conn.close()


def _brain_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "ira.brain" and r.levelno >= logging.WARNING]


def _logged_anywhere(caplog, text: str) -> list[str]:
    return [f"{r.name}:{r.levelname}" for r in caplog.records if text in r.getMessage()]


def _chat_principal(workspace_id, *, owner: bool) -> Principal:
    """What a private-edition request resolves to in the owner's workspace:
    the `.env` owner, or a delegated account that is a member of it."""
    username = OWNER_USERNAME if owner else DELEGATE_USERNAME
    return Principal(
        user_id=private_user_id(username), tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(), edition=EDITION_PRIVATE,
        roles=frozenset({"primary_owner" if owner else "trusted_user"}),
        username=username, is_owner=owner, workspace_id=workspace_id,
    )


# ── A write lands, in the owner's tenant and workspace, labelled ─────────────


async def test_a_brain_thought_is_stored_in_the_owners_workspace(
        owner_workspace, thought, caplog):
    with caplog.at_level(logging.DEBUG):
        await _think(thought)

    rows = await _rows(thought)
    assert len(rows) == 1, (
        f"expected the thought stored once, found {len(rows)} rows; "
        f"the brain logged: {_brain_warnings(caplog) or 'nothing at WARNING'}")
    row = rows[0]
    assert row["tenant_id"] == PRIVATE_TENANT_ID
    assert row["workspace_id"] == owner_workspace
    assert row["user_id"] == VAULT_NAMESPACE
    # Labelled as the brain's own, and never as something the owner confirmed.
    assert row["source_type"] == "brain_thought"
    assert row["owner_confirmed"] is False
    assert row["has_vector"] is True
    assert _brain_warnings(caplog) == []


async def test_the_sink_stores_a_consolidation(owner_workspace, thought):
    """The sink called directly, as `consolidate()` calls it. On `main` this
    is where the TypeError surfaces."""
    await IraMemorySink().remember(thought, kind="consolidation", importance=0.7)

    rows = await _rows(thought)
    assert len(rows) == 1, f"expected one row, found {len(rows)}"
    assert rows[0]["tenant_id"] == PRIVATE_TENANT_ID
    assert rows[0]["workspace_id"] == owner_workspace
    assert rows[0]["source_type"] == "brain_consolidation"


# ── It is recalled by the owner's chat, as reference data only ───────────────


async def test_the_owner_recalls_it_as_reference_data_and_a_delegate_does_not(
        owner_workspace, thought):
    """Through the call chat makes, `retrieve(principal, message, user_id=username)`.

    A delegated account in the same workspace passes every RLS policy the owner
    does; the vault namespace is what keeps the brain's memories the owner's.
    """
    from memory.store import retrieve
    from reasoning.memory_context import MEMORY_REFERENCE_LABEL, select_memory_context

    await _think(thought)

    owner = _chat_principal(owner_workspace, owner=True)
    hits = await retrieve(owner, thought, user_id=owner.username)
    mine = [h for h in hits if h["content"] == thought]
    assert len(mine) == 1, f"the owner's chat did not recall the brain's memory: {hits}"
    assert mine[0]["source_type"] == "brain_thought"

    # A6: what reaches the model is labelled data, never an instruction.
    block = select_memory_context(mine)
    assert block.startswith(MEMORY_REFERENCE_LABEL)
    assert thought in block

    delegate = _chat_principal(owner_workspace, owner=False)
    seen = [h["content"] for h in await retrieve(delegate, thought, user_id=delegate.username)]
    assert thought not in seen, "a delegated account recalled the brain's memory"


# ── No identity, no write, and a WARNING that does not carry the text ────────


async def test_the_cloud_edition_writes_nothing(thought, caplog, monkeypatch):
    monkeypatch.setenv("IRA_EDITION", "cloud")

    with caplog.at_level(logging.DEBUG):
        await _think(thought)

    assert await _rows(thought) == []
    warnings = _brain_warnings(caplog)
    assert any("cloud" in w for w in warnings), (
        f"the refusal was not reported at WARNING: {warnings}")
    assert _logged_anywhere(caplog, thought) == [], "the log carried the thought"


async def test_no_owner_workspace_writes_nothing(thought, caplog, monkeypatch):
    """A private tenant with no default workspace: there is nowhere the owner's
    memory belongs, so the brain must not pick somewhere."""
    monkeypatch.setattr("core.identity.principal.PRIVATE_TENANT_ID", uuid.uuid4())

    with caplog.at_level(logging.DEBUG):
        await _think(thought)

    assert await _rows(thought) == []
    warnings = _brain_warnings(caplog)
    assert any("default workspace" in w for w in warnings), (
        f"the refusal was not reported at WARNING: {warnings}")
    assert _logged_anywhere(caplog, thought) == [], "the log carried the thought"


async def test_a_failed_write_is_reported_without_its_text(
        owner_workspace, thought, caplog, monkeypatch):
    """The store fails after the identity resolved (here, the embedder). The
    store used to swallow that at DEBUG with the exception's text; the brain
    now hears it and reports the exception's type only."""
    async def broken(text: str):
        raise RuntimeError(f"embedder down while reading: {text}")

    monkeypatch.setattr("memory.store.embed_one", broken, raising=False)

    with caplog.at_level(logging.DEBUG):
        await _think(thought)

    assert await _rows(thought) == []
    warnings = _brain_warnings(caplog)
    assert any("RuntimeError" in w for w in warnings), (
        f"the failed write was not reported at WARNING: {warnings}")
    assert _logged_anywhere(caplog, thought) == [], "the log carried the thought"
