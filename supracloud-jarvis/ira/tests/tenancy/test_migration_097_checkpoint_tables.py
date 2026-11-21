"""Migration 097: LangGraph's checkpoint tables come from the chain, owned and granted like every other.

Before 097 the runtime role had to create these tables itself through LangGraph's
`setup()`, which it may not (042), so every native install kept conversation-graph
state in memory. 097 creates them as the migration role. These tests pin what that
must mean, on the database the migration chain built:

  * the four tables exist and belong to ira_schema_owner, never to a runtime role;
  * ira_app may SELECT, INSERT, UPDATE and DELETE the state, and only SELECT the
    migration record (LangGraph writes it only in `setup()`, which the runtime
    role never runs); no TRUNCATE, no REFERENCES, and still no CREATE on `public`;
  * ira_worker and PUBLIC get nothing;
  * LangGraph's ten migrations are recorded, so the application skips `setup()`;
  * the columns are the ones LangGraph 3.1.3 reads and writes, so a row it writes
    fits. Where the package is installed, the schema is also compared with what
    LangGraph's own migrations build, which catches a later LangGraph that
    changes them;
  * as ira_app, the real `init_checkpointer` uses these tables without `setup()`
    and a checkpoint round-trips (needs the package; skipped without it).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")

#: (column, type, nullable, default) as information_schema reports them, for the
#: schema langgraph-checkpoint-postgres 3.1.3's ten migrations leave behind.
EXPECTED = {
    "checkpoint_migrations": [("v", "integer", "NO", None)],
    "checkpoints": [
        ("thread_id", "text", "NO", None),
        ("checkpoint_ns", "text", "NO", "''::text"),
        ("checkpoint_id", "text", "NO", None),
        ("parent_checkpoint_id", "text", "YES", None),
        ("type", "text", "YES", None),
        ("checkpoint", "jsonb", "NO", None),
        ("metadata", "jsonb", "NO", "'{}'::jsonb"),
    ],
    "checkpoint_blobs": [
        ("thread_id", "text", "NO", None),
        ("checkpoint_ns", "text", "NO", "''::text"),
        ("channel", "text", "NO", None),
        ("version", "text", "NO", None),
        ("type", "text", "NO", None),
        ("blob", "bytea", "YES", None),
    ],
    "checkpoint_writes": [
        ("thread_id", "text", "NO", None),
        ("checkpoint_ns", "text", "NO", "''::text"),
        ("checkpoint_id", "text", "NO", None),
        ("task_id", "text", "NO", None),
        ("idx", "integer", "NO", None),
        ("channel", "text", "NO", None),
        ("type", "text", "YES", None),
        ("blob", "bytea", "NO", None),
        ("task_path", "text", "NO", "''::text"),
    ],
}


async def _columns(conn, schema: str, table: str):
    rows = await conn.fetch(
        """SELECT column_name, data_type, is_nullable, column_default
             FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position""", schema, table)
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def _indexes(conn, schema: str, table: str):
    rows = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = $2",
        schema, table)
    return sorted(r[0].replace(f"{schema}.", "") for r in rows)


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def test_tables_belong_to_the_schema_owner(admin):
    owners = dict(await admin.fetch(
        "SELECT tablename, tableowner FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename = ANY($1::text[])", list(TABLES)))
    assert set(owners) == set(TABLES), f"missing after the chain: {set(TABLES) - set(owners)}"
    assert set(owners.values()) == {"ira_schema_owner"}, owners


async def test_the_runtime_role_gets_dml_and_nothing_else(admin):
    for table in TABLES:
        grants = {}
        for r in await admin.fetch(
                "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND table_name = $1", table):
            grants.setdefault(r[0], set()).add(r[1])
        expected = ({"SELECT"} if table == "checkpoint_migrations"
                    else {"SELECT", "INSERT", "UPDATE", "DELETE"})
        assert grants.get("ira_app") == expected, (table, grants)
        assert "ira_worker" not in grants, (table, grants)
        assert "PUBLIC" not in grants, (table, grants)
    may_create = await admin.fetchval(
        "SELECT has_schema_privilege('ira_app', 'public', 'CREATE')")
    assert may_create is False, "097 must not give the runtime role CREATE on public"


async def test_langgraphs_migrations_are_recorded(admin):
    recorded = [r[0] for r in await admin.fetch("SELECT v FROM checkpoint_migrations ORDER BY v")]
    assert recorded == list(range(10)), recorded


async def test_columns_are_the_ones_langgraph_uses(admin):
    for table, columns in EXPECTED.items():
        assert await _columns(admin, "public", table) == columns, table


async def test_schema_matches_what_langgraph_itself_builds(admin):
    """Drift check: run LangGraph's own migrations in a scratch schema and compare."""
    base = pytest.importorskip(
        "langgraph.checkpoint.postgres.base",
        reason="langgraph-checkpoint-postgres not installed")
    schema = f"lg_ref_{uuid.uuid4().hex[:8]}"
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        await admin.execute(f"SET search_path TO {schema}")
        for statement in base.BasePostgresSaver.MIGRATIONS:
            await admin.execute(statement)
        await admin.execute("SET search_path TO public")
        assert len(base.BasePostgresSaver.MIGRATIONS) == 10, (
            "LangGraph's migration count changed: a new migration (098) must create "
            "what it adds, and 097's recorded versions move with it")
        for table in TABLES:
            assert await _columns(admin, "public", table) == await _columns(admin, schema, table), table
            assert await _indexes(admin, "public", table) == await _indexes(admin, schema, table), table
    finally:
        await admin.execute("SET search_path TO public")
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")


async def test_the_application_uses_the_tables_without_setup(monkeypatch):
    """As ira_app, the real `init_checkpointer` picks the persistent saver and a
    checkpoint written through it can be read back and deleted."""
    pytest.importorskip("langgraph.checkpoint.postgres.aio",
                        reason="langgraph-checkpoint-postgres not installed")
    for k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
              "REDIS_PASSWORD", "VLLM_API_KEY"):
        os.environ.setdefault(k, "test-placeholder")
    st = sys.modules.get("sentence_transformers")
    if st is not None and not hasattr(st, "SentenceTransformer"):
        st.SentenceTransformer = object
        st.CrossEncoder = object
    import agents.graph as graph
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    setup_calls = []

    async def no_setup(self):  # the runtime role cannot run it; it must not be needed
        setup_calls.append(1)
        raise AssertionError("setup() was called although 097's tables are current")

    monkeypatch.setattr(AsyncPostgresSaver, "setup", no_setup)
    try:
        await graph.init_checkpointer(DSN)
        saver = graph._checkpointer
        assert isinstance(saver, AsyncPostgresSaver), type(saver)
        assert not setup_calls
        thread = f"ira:test-097:{uuid.uuid4()}"
        config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        await saver.aput(config, checkpoint, {"source": "input", "step": -1}, {})
        got = await saver.aget_tuple(config)
        assert got is not None and got.checkpoint["id"] == checkpoint["id"]
        await saver.adelete_thread(thread)
        assert await saver.aget_tuple(config) is None
    finally:
        await graph.close_checkpointer()
