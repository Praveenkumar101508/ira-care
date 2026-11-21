"""3C-01 — checkpointed conversation state stays inside its workspace.

LangGraph persists `IRAState` through `AsyncPostgresSaver`. Its tables come
from LangGraph's own schema (migration 097 creates them in the chain), so they
have no `tenant_id`, no `workspace_id` and no RLS — everything Batch 5C built
stops at their edge. And `IRAState` is not metadata: it carries the
message history, the user's query, the response, and `memory_context`, which is
vault memories retrieved and injected into the prompt.

Two consequences, and the second is the one that matters most:

  **The thread key was client-addressable.** `thread_id` was the bare
  `session_id`, which arrives on the request body, so a caller who knew another
  session's id could name that thread. There was no policy to stop them.

  **Erasure could not find these rows at all.** `core/privacy/erasure.py` had no
  reference to checkpoints, so an erasure returned `ok=True` while leaving the
  conversation behind. A privacy promise that logs success and does not keep is
  worse than one that fails.

The fix is one idea doing both jobs: `checkpoint_thread_id` puts the verified
tenant and workspace in front of the caller's session id. The client still owns
the suffix and owns nothing else, and the prefix is what finally gives erasure a
handle on the rows.
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

# conftest stubs `sentence_transformers` but leaves the attributes the
# agents/graph.py import chain reaches for unset — the same fill-in
# tests/test_app_factory.py does. Nothing here instantiates them.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.tenancy.adversarial import app_pool, matrix  # noqa: E402,F401

from agents.graph import (  # noqa: E402
    CHECKPOINT_THREAD_PREFIX,
    checkpoint_thread_id,
    checkpoint_thread_prefix,
)
from core.identity.db_context import MissingWorkspaceContext  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# The thread key
# ─────────────────────────────────────────────────────────────────────────────


def test_the_thread_key_names_the_tenant_and_the_workspace(matrix):
    thread = checkpoint_thread_id(matrix.a.principal(), "session-123")

    assert thread.startswith(CHECKPOINT_THREAD_PREFIX)
    assert str(matrix.a.tenant_id) in thread
    assert str(matrix.a.workspace_id) in thread
    assert thread.endswith("session-123")


def test_the_same_session_id_is_a_different_thread_in_each_workspace(matrix):
    """The property that makes the client's value harmless.

    A and B are the same tenant and the same person could hold both. Supplying
    one session id from either place must not reach the other's state — the
    caller controls the suffix and nothing else.
    """
    session = "shared-session-id"
    in_a = checkpoint_thread_id(matrix.a.principal(), session)
    in_b = checkpoint_thread_id(matrix.b.principal(), session)
    in_c = checkpoint_thread_id(matrix.c.principal(), session)

    assert in_a != in_b, "two workspaces of one tenant share a checkpoint thread"
    assert in_a != in_c
    assert len({in_a, in_b, in_c}) == 3


def test_a_forged_session_id_cannot_escape_its_namespace(matrix):
    """The client's value is a suffix, so it cannot climb out of the prefix.

    Even a session id shaped like another workspace's thread stays underneath
    the caller's own tenant and workspace — the server-derived part is written
    first and is not something the request can displace.
    """
    forged = f"{CHECKPOINT_THREAD_PREFIX}:{matrix.b.tenant_id}:w:{matrix.b.workspace_id}:s:x"
    thread = checkpoint_thread_id(matrix.a.principal(), forged)

    assert thread.startswith(checkpoint_thread_prefix(
        matrix.a.tenant_id, matrix.a.workspace_id))
    assert thread != checkpoint_thread_id(matrix.b.principal(), "x")


def test_a_principal_with_no_workspace_is_refused(matrix):
    """No namespace, no thread. A checkpoint written outside the namespace is
    customer content with nothing to attribute it to and no way to erase it, so
    refusing is the only safe answer — never a fallback."""
    with pytest.raises(MissingWorkspaceContext):
        checkpoint_thread_id(matrix.a.without_workspace(), "session-123")


def test_the_prefix_matcher_agrees_with_the_builder(matrix):
    """Erasure matches on a prefix, and a prefix that stopped matching what the
    builder produces would delete nothing and report success — the exact failure
    this finding is about. So the two are checked against each other."""
    thread = checkpoint_thread_id(matrix.a.principal(), "s1")

    assert thread.startswith(checkpoint_thread_prefix(matrix.a.tenant_id))
    assert thread.startswith(
        checkpoint_thread_prefix(matrix.a.tenant_id, matrix.a.workspace_id))
    # And does NOT match the neighbouring workspace's narrower prefix.
    assert not thread.startswith(
        checkpoint_thread_prefix(matrix.a.tenant_id, matrix.b.workspace_id))
    assert not thread.startswith(checkpoint_thread_prefix(matrix.c.tenant_id))
    # The user-level prefix erasure actually uses (ADR-0009).
    assert thread.startswith(checkpoint_thread_prefix(
        matrix.a.tenant_id, matrix.a.workspace_id, matrix.a.user_id))
    assert not thread.startswith(checkpoint_thread_prefix(
        matrix.a.tenant_id, matrix.a.workspace_id, matrix.b.user_id))


def test_two_people_in_one_workspace_never_share_a_thread(matrix):
    """ADR-0009. Same tenant, same workspace, same session id, different people:
    before the user segment these named ONE thread, and with it each other's
    message history and injected vault memories."""
    from dataclasses import replace

    alice = matrix.a.principal()
    bob_here = replace(alice, user_id=matrix.b.user_id)
    session = "same-session-id"

    assert checkpoint_thread_id(alice, session) != checkpoint_thread_id(bob_here, session)
    # And a session id shaped like the other person's thread cannot climb into it.
    forged = checkpoint_thread_id(alice, session).split(":s:", 1)[0] + ":s:" + session
    assert checkpoint_thread_id(bob_here, forged).startswith(checkpoint_thread_prefix(
        alice.tenant_id, alice.workspace_id, matrix.b.user_id))


def test_run_graph_uses_the_namespaced_key(matrix):
    """Read from the source, because the behavioural path needs a model.

    A `thread_id` set from the bare `session_id` anywhere in this module is the
    defect, and it is one edit away from coming back.
    """
    import pathlib
    import re

    source = pathlib.Path(
        __file__).resolve().parents[2].joinpath("agents/graph.py").read_text(encoding="utf-8")
    assignments = re.findall(r'"thread_id"\s*:\s*([^,\n]+)', source)

    assert assignments, "no thread_id assignment found — has run_graph moved?"
    for value in assignments:
        assert "checkpoint_thread_id" in value, (
            f'thread_id is set from {value.strip()!r} rather than from '
            f'checkpoint_thread_id(). A bare session_id is client-supplied and '
            f'names no tenant, which makes the checkpoint namespace addressable '
            f'from outside the workspace.')


# ─────────────────────────────────────────────────────────────────────────────
# Erasure reaches them
# ─────────────────────────────────────────────────────────────────────────────


CHECKPOINT_DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def _require_checkpoint_tables(conn) -> None:
    """Migration 097 creates LangGraph's tables; these tests use the real ones.

    Until 097 this test built simplified stand-ins (a `payload` column) and
    dropped them afterwards, because nothing else made the tables. With 097 in
    the chain, the stand-ins would collide with the real schema and the DROP
    would remove the chain's own tables from the test database.
    """
    for table in CHECKPOINT_DATA_TABLES:
        present = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
        assert present is not None, (
            f"{table} is missing: migration 097 creates it, so the chain did not run")


async def _write_state(conn, thread: str) -> None:
    """One row in each data table, shaped as LangGraph writes them."""
    checkpoint_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint) "
        "VALUES ($1, '', $2, $3::jsonb)",
        thread, checkpoint_id, '{"note": "a conversation nobody else may read"}')
    await conn.execute(
        "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
        "VALUES ($1, '', 'messages', '1', 'json', $2)",
        thread, b"state")
    await conn.execute(
        "INSERT INTO checkpoint_writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES ($1, '', $2, 'task', 0, 'messages', $3)",
        thread, checkpoint_id, b"write")


async def _threads_left(conn, table: str, threads) -> set:
    rows = await conn.fetch(
        f"SELECT DISTINCT thread_id FROM {table} WHERE thread_id = ANY($1::text[])",  # noqa: S608
        list(threads))
    return {r["thread_id"] for r in rows}


async def test_erasure_deletes_this_tenants_checkpoints(matrix, app_pool):
    """The finding, closed.

    Before the fix this test could not even be written: there was no way to
    associate a checkpoint row with a tenant, so nothing could assert that
    erasure had reached it.
    """
    from core.privacy.erasure import erase_postgres

    admin = await asyncpg.connect(ADMIN_DSN)
    mine = checkpoint_thread_id(matrix.a.principal(), "mine")
    neighbour = checkpoint_thread_id(matrix.b.principal(), "theirs")
    stranger = checkpoint_thread_id(matrix.c.principal(), "stranger")
    # A thread written before ADR-0009, with no user segment. Nothing builds
    # such an id any more, so its state is unreachable; erasure removes it.
    legacy = (f"{CHECKPOINT_THREAD_PREFIX}:{matrix.a.tenant_id}:w:"
              f"{matrix.a.workspace_id}:s:from-before-adr-0009")
    threads = (mine, neighbour, stranger, legacy)
    try:
        await _require_checkpoint_tables(admin)
        for thread in threads:
            await _write_state(admin, thread)

        result = await erase_postgres(matrix.a.principal(), scope="conversations")
        assert result.ok, f"erasure reported failure: {result}"

        for table in CHECKPOINT_DATA_TABLES:
            surviving = await _threads_left(admin, table, threads)
            assert mine not in surviving, f"erasure left the user's own state in {table}"
            assert legacy not in surviving, (
                f"erasure left a pre-ADR-0009 thread of this tenant in {table}; nothing "
                "can reach it any more, so nothing else would ever remove it")
            # User B is in the same tenant but is a DIFFERENT PERSON. Before
            # ADR-0009 this asserted the opposite: erasure matched the whole
            # tenant prefix, so A's erasure deleted B's agent state.
            assert neighbour in surviving, f"A's erasure deleted another user's state in {table}"
            # Tenant C is untouched. Erasure that reached further than it was
            # asked to is a different and worse failure than one that reached
            # too little.
            assert stranger in surviving, f"erasure deleted another tenant's state in {table}"
    finally:
        for table in CHECKPOINT_DATA_TABLES:
            await admin.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY($1::text[])",  # noqa: S608
                list(threads))
        await admin.close()


async def test_erasure_tolerates_absent_checkpoint_tables(matrix, app_pool):
    """A database without these tables (one where the checkpointer never ran,
    from before 097) must not make erasure fail: there was nothing to erase.

    The chain now creates them, so the test hides them by renaming, and puts
    them back whatever happens. The suite runs one test at a time.
    """
    from core.privacy.erasure import erase_postgres

    admin = await asyncpg.connect(ADMIN_DSN)
    hidden = []
    try:
        for table in CHECKPOINT_DATA_TABLES:
            await admin.execute(f"ALTER TABLE {table} RENAME TO {table}_hidden_3c01")
            hidden.append(table)
        result = await erase_postgres(matrix.a.principal(), scope="conversations")
        assert result.ok, f"erasure failed with no checkpoint tables present: {result}"
    finally:
        for table in hidden:
            await admin.execute(f"ALTER TABLE {table}_hidden_3c01 RENAME TO {table}")
        await admin.close()


async def test_erasure_reports_failure_rather_than_silently_skipping(
    matrix, app_pool, monkeypatch
):
    """If the checkpoint pass throws, the result must say so.

    The whole finding is that this store was missed while `ok=True` was
    returned. A pass that swallowed its own errors would recreate that exactly,
    one layer further in.
    """
    from core.privacy import erasure

    async def boom(principal):
        raise RuntimeError("checkpoint store unreachable")

    monkeypatch.setattr(erasure, "_erase_checkpoints", boom)

    result = await erasure.erase_postgres(matrix.a.principal(), scope="conversations")

    assert not result.ok
    assert "checkpoint" in (result.error or "").lower()


def test_erasure_names_the_checkpoint_store(matrix):
    """A source-level guard on the omission itself.

    The behavioural tests above need the tables to exist. This one fails if the
    pass is deleted, whatever the environment.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2].joinpath(
        "core/privacy/erasure.py").read_text(encoding="utf-8")

    assert "_erase_checkpoints" in source, (
        "core/privacy/erasure.py no longer erases LangGraph checkpoints. Those "
        "tables hold the message history and the memory context injected into "
        "the prompt, and an erasure without them reports ok=True while leaving "
        "the conversation behind.")
