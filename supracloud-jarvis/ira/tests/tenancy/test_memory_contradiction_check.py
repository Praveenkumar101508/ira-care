"""P1-04: the memory contradiction check runs, and sees only the caller's memories.

── The defect ───────────────────────────────────────────────────────────────

`memory/temporal.py::check_before_save(principal, content, ...)` called
`retrieve_active(content, user_id=...)`. Since the store took a principal its
signature is `retrieve_active(principal, query, *, user_id, ...)`: the content
arrived as the principal, `query` was missing, and every call raised
TypeError. `memory.contradiction_guard.guarded_save` catches a failed check so
that a down database cannot block a save, so the TypeError was logged as
"contradiction check unavailable" and the owner's "Remember that ..." went out
as a plain save proposal with `contradiction_checked: False`. No contradiction
was ever detected. The unit test's stand-in for `retrieve_active` had the same
wrong signature, so it could not see it.

Found on a clean CPU-only Linux install on 31 October 2026 (PR #190).

── What is real ─────────────────────────────────────────────────────────────

PostgreSQL as `ira_app` (NOSUPERUSER, NOBYPASSRLS, RLS forced), the real
temporal store (`save_memory`, `retrieve_active`, `check_before_save`), the
contradiction guard and the chat command path (`guard_chat_memory`). One thing
is replaced: the embedding model, by a deterministic "topic" vector, a hash of
the text with its digits removed. Two statements that differ only in a number
or a date get the same vector, and anything else gets an unrelated one. So
these tests show which memories the check can reach and that it runs; they say
nothing about how a real embedding scores two sentences.

── Isolation ────────────────────────────────────────────────────────────────

The check runs as the principal it is handed, the request's own. A memory in
another tenant, in another workspace of the same tenant, or under another
namespace in the same workspace is never reported as a contradiction.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import uuid

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# conftest stubs `sentence_transformers` without the attributes the import
# chain reaches for; nothing here instantiates them.
_st = sys.modules.get("sentence_transformers")
if _st is not None:
    for _name in ("SentenceTransformer", "CrossEncoder"):
        if not hasattr(_st, _name):
            setattr(_st, _name, object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402
from core.identity.principal import (  # noqa: E402
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
)
from memory import temporal  # noqa: E402
from memory.chat_commands import guard_chat_memory  # noqa: E402
from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

#: `memory_embeddings.embedding` is `vector(1024)`.
EMBED_DIM = 1024


def _topic_vector(text: str) -> list[float]:
    """Same text apart from its digits, same vector."""
    topic = re.sub(r"\d+", "#", text.lower())
    digest = hashlib.sha256(topic.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    async def embed_one(text: str):
        return _topic_vector(text)

    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)


@pytest.fixture(autouse=True)
async def app_pool():
    """Point `utils.db.acquire()` at the test database as `ira_app`."""
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


class World:
    """Two tenants. Tenant A has its default workspace and a second one."""

    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    ws_a: uuid.UUID
    ws_a2: uuid.UUID
    ws_b: uuid.UUID


@pytest.fixture(scope="module")
def world():
    """Created as the superuser, because creating tenants is not under test;
    every read and write below goes through `ira_app` and RLS."""
    w = World()
    w.tenant_a, w.tenant_b = uuid.uuid4(), uuid.uuid4()
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        for t, name in ((w.tenant_a, "contradiction-a"), (w.tenant_b, "contradiction-b")):
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)",
                        (str(t), f"{name}-{t.hex[:8]}"))
        cur.execute("SELECT provision_tenant_substrate(%s)", (str(w.tenant_a),))
        w.ws_a = cur.fetchone()[0]
        cur.execute("SELECT provision_tenant_substrate(%s)", (str(w.tenant_b),))
        w.ws_b = cur.fetchone()[0]
        cur.execute("SELECT id FROM organisations WHERE tenant_id = %s", (str(w.tenant_a),))
        org = cur.fetchone()[0]
        w.ws_a2 = uuid.uuid4()
        cur.execute(
            "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
            "                        is_default, status) "
            "VALUES (%s, %s, %s, 'Second workspace', %s, false, 'active')",
            (str(w.ws_a2), str(w.tenant_a), str(org), f"contradiction-{w.ws_a2.hex[:8]}"))
        yield w
    finally:
        try:
            for t in (w.tenant_a, w.tenant_b):
                cur.execute("DELETE FROM memory_embeddings WHERE tenant_id = %s", (str(t),))
                cur.execute("DELETE FROM workspace_memberships WHERE tenant_id = %s", (str(t),))
                cur.execute("DELETE FROM tenant_memberships WHERE tenant_id = %s", (str(t),))
                cur.execute("DELETE FROM workspaces WHERE tenant_id = %s", (str(t),))
                cur.execute("DELETE FROM organisations WHERE tenant_id = %s", (str(t),))
                cur.execute("DELETE FROM tenants WHERE id = %s", (str(t),))
        finally:
            conn.close()


@pytest.fixture
def marker():
    """A unique prefix for this test's memories, and their removal afterwards
    (the private tenant is not torn down with the module's tenants)."""
    tag = f"SYNTHETIC-{uuid.uuid4().hex[:10]}"
    yield tag
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    try:
        conn.cursor().execute("DELETE FROM memory_embeddings WHERE content LIKE %s", (f"{tag}%",))
    finally:
        conn.close()


def _cloud_principal(tenant, workspace) -> Principal:
    return Principal(user_id=uuid.uuid4(), tenant_id=tenant, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=workspace)


async def _private_owner() -> Principal:
    """What the `.env` owner's request resolves to on a private install: the
    private tenant's default workspace, as the migrations created it."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id FROM workspaces WHERE tenant_id = $1 AND is_default "
            "AND status = 'active'", PRIVATE_TENANT_ID)
    finally:
        await conn.close()
    assert len(rows) == 1, "the private tenant has no single active default workspace"
    return Principal(
        user_id=private_user_id("admin"), tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(), edition=EDITION_PRIVATE,
        roles=frozenset({"primary_owner"}), username="admin", is_owner=True,
        workspace_id=rows[0]["id"],
    )


def _dentist(marker: str, day: int) -> str:
    return f"{marker} my dentist appointment is on {day} July"


def _check_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "ira.memory.contradiction_guard" and r.levelno >= logging.WARNING]


# ── The owner's chat path: a contradiction is found and the owner is asked ──


async def test_the_owners_contradicting_remember_is_asked_about(marker, caplog):
    owner = await _private_owner()
    old = _dentist(marker, 18)
    await temporal.save_memory(owner, old, user_id=VAULT_NAMESPACE)

    with caplog.at_level(logging.WARNING):
        result = await guard_chat_memory(
            owner, f"Remember that {_dentist(marker, 22)}", user="admin", is_owner=True)

    assert _check_warnings(caplog) == [], f"the check did not run: {_check_warnings(caplog)}"
    assert result is not None
    assert result["status"] == "needs_owner_decision", result
    assert result["contradiction_checked"] is True
    [candidate] = result["candidates"]
    assert candidate["existing_content"] == old
    assert candidate["conflicting_values"] == ["18july", "22july"]
    assert old in result["questions"][0]


async def test_a_statement_with_no_conflict_is_proposed_as_checked(marker, caplog):
    """The check ran and found nothing: `contradiction_checked` now says so,
    where before it said False on every save."""
    owner = await _private_owner()
    await temporal.save_memory(owner, _dentist(marker, 18), user_id=VAULT_NAMESPACE)

    with caplog.at_level(logging.WARNING):
        result = await guard_chat_memory(
            owner, f"Remember that {marker} I prefer green tea in the morning", user="admin",
            is_owner=True)

    assert _check_warnings(caplog) == []
    assert result is not None
    assert result["status"] == "needs_owner_approval", result
    assert result["contradiction_checked"] is True


async def test_check_before_save_finds_the_callers_contradiction(world, marker):
    caller = _cloud_principal(world.tenant_a, world.ws_a)
    old = _dentist(marker, 18)
    old_id = await temporal.save_memory(caller, old, user_id=VAULT_NAMESPACE)

    hits = await temporal.check_before_save(caller, _dentist(marker, 22), user_id=VAULT_NAMESPACE)

    assert [(h.existing_id, h.existing_content) for h in hits] == [(old_id, old)]


# ── Isolation: what the check must never report ─────────────────────────────


async def test_another_tenants_memory_is_never_a_contradiction(world, marker):
    """Tenant B holds the conflicting fact under the same namespace. Tenant A's
    caller saying the opposite finds nothing, and so does a positive control
    in B, which proves the row was there to find."""
    other = _cloud_principal(world.tenant_b, world.ws_b)
    await temporal.save_memory(other, _dentist(marker, 18), user_id=VAULT_NAMESPACE)

    caller = _cloud_principal(world.tenant_a, world.ws_a)
    assert await temporal.check_before_save(
        caller, _dentist(marker, 22), user_id=VAULT_NAMESPACE) == []

    control = await temporal.check_before_save(
        other, _dentist(marker, 22), user_id=VAULT_NAMESPACE)
    assert len(control) == 1, "the other tenant's own check did not see its own memory"


async def test_another_workspaces_memory_is_never_a_contradiction(world, marker):
    elsewhere = _cloud_principal(world.tenant_a, world.ws_a2)
    await temporal.save_memory(elsewhere, _dentist(marker, 18), user_id=VAULT_NAMESPACE)

    caller = _cloud_principal(world.tenant_a, world.ws_a)
    assert await temporal.check_before_save(
        caller, _dentist(marker, 22), user_id=VAULT_NAMESPACE) == []

    control = await temporal.check_before_save(
        elsewhere, _dentist(marker, 22), user_id=VAULT_NAMESPACE)
    assert len(control) == 1


async def test_another_namespaces_memory_is_never_a_contradiction(world, marker):
    """Same tenant and workspace: the namespace (`user_id`) still separates one
    person's memories from another's."""
    caller = _cloud_principal(world.tenant_a, world.ws_a)
    await temporal.save_memory(caller, _dentist(marker, 18), user_id="someone-else")

    assert await temporal.check_before_save(
        caller, _dentist(marker, 22), user_id=VAULT_NAMESPACE) == []

    control = await temporal.check_before_save(
        caller, _dentist(marker, 22), user_id="someone-else")
    assert len(control) == 1
