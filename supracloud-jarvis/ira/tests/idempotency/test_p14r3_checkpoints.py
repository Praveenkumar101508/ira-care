"""P14-R3 — the checkpoint writers, established against a real database.

The third of P14-04's dropped subjects:

    the governed action path, the notifier and the checkpoint writers are not
    established either way.

── Why this is a test and not a fix ───────────────────────────────────────

The checkpoint writer is `AsyncPostgresSaver` from
`langgraph-checkpoint-postgres`, a pinned third-party component. IRA does not
write checkpoint rows itself; it supplies the thread id
(`agents/graph.py::checkpoint_thread_id`) and lets the saver persist state.

So the question is not "is our code idempotent" but "does the component we
depend on behave the way the recovery path assumes". That assumption is real
and load-bearing: a graph that is interrupted and resumed re-executes from the
last checkpoint, and re-executing a node writes the checkpoint for that step
AGAIN. If those writes accumulated instead of replacing, a resumed thread would
grow a row per attempt and `get_tuple` would have to pick among them.

**A dependency's behaviour is not established by reading its README**, which is
the whole subject of Phase 16. It is established by writing the same checkpoint
twice and counting rows. That is what this does, on the pinned version.

── Why this is not a claim about LangGraph in general ─────────────────────

It is a claim about `langgraph-checkpoint-postgres==3.1.3`, the version in
`requirements.txt` (3.1.2 until 23 October 2026; both pass it). If the pin
moves and this behaviour changes, this test fails — which is the point of
asserting it here rather than trusting it.
"""
from __future__ import annotations

import os
import uuid

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = [
    pytest.mark.skipif(
        not DSN,
        reason="IRA_TEST_DATABASE_DSN not set. The question is how a real "
               "PostgreSQL-backed checkpointer behaves on a repeated write; "
               "a stub would answer about the stub."),
    pytest.mark.asyncio,
]


def _checkpoint(checkpoint_id: str, value: str) -> dict:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-08-13T00:00:00+00:00",
        "channel_values": {"step": value},
        "channel_versions": {"step": 1},
        "versions_seen": {},
    }


@pytest.fixture
async def saver():
    saver_mod = pytest.importorskip(
        "langgraph.checkpoint.postgres.aio",
        reason="langgraph-checkpoint-postgres is not installed here. "
               "requirements-test.txt pins it at production's version; install "
               "that file to run this. In the tenancy CI job a missing saver "
               "fails tests/tenancy/test_harness_is_configured.py instead.")
    async with saver_mod.AsyncPostgresSaver.from_conn_string(DSN) as s:
        # Migration 097 creates these tables. `setup()` always begins with a
        # CREATE, which the runtime role may not run (042), so it runs here only
        # where the tables are missing, as on a superuser's scratch database.
        async with s.conn.cursor() as cur:
            await cur.execute(
                "SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS present")
            present = (await cur.fetchone())["present"]
        if not present:
            await s.setup()
        yield s


async def _rows(saver, thread_id: str) -> int:
    async with saver.conn.cursor() as cur:
        # Aliased, and read by name. The saver configures its connection with a
        # dict row factory, so indexing the row by position raises `KeyError: 0`
        # — which reads like a missing row rather than a row shaped differently
        # from the assumption.
        await cur.execute(
            "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s",
            (thread_id,))
        row = await cur.fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])


async def test_rewriting_a_checkpoint_replaces_it_rather_than_accumulating(saver):
    """The property the resume path depends on.

    A graph interrupted mid-step and resumed re-executes that step, which
    writes the SAME checkpoint id again. One row must remain, carrying the
    later value — not two rows for one step.
    """
    thread_id = f"p14r3-{uuid.uuid4()}"
    checkpoint_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    await saver.aput(config, _checkpoint(checkpoint_id, "first"), {}, {})
    await saver.aput(config, _checkpoint(checkpoint_id, "second"), {}, {})

    count = await _rows(saver, thread_id)
    assert count == 1, (
        f"writing one checkpoint id twice produced {count} rows. A resumed "
        f"thread re-executes its last step, so this accumulates a row per "
        f"attempt and the reader has to choose among them.")

    restored = await saver.aget_tuple(config)
    assert restored is not None, "the checkpoint could not be read back"
    assert restored.checkpoint["channel_values"]["step"] == "second", (
        "the rewrite did not take effect — the row is the FIRST write, so a "
        "resumed step's progress is silently discarded")


async def test_two_checkpoints_of_one_thread_are_both_kept(saver):
    """The negative control.

    Without it, a saver that stored nothing — or that overwrote every
    checkpoint of a thread regardless of id — would satisfy the test above
    while destroying the history a resume walks back through.
    """
    thread_id = f"p14r3-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    await saver.aput(config, _checkpoint(str(uuid.uuid4()), "step-1"), {}, {})
    await saver.aput(config, _checkpoint(str(uuid.uuid4()), "step-2"), {}, {})

    count = await _rows(saver, thread_id)
    assert count == 2, (
        f"two DISTINCT checkpoints of one thread produced {count} rows. The "
        f"saver is collapsing history, not deduplicating retries — which "
        f"passes the idempotency assertion above for the wrong reason.")


async def test_two_threads_do_not_share_a_checkpoint(saver):
    """The thread id is the isolation boundary the tenant prefix relies on.

    `agents/graph.py::checkpoint_thread_id` builds the id from the tenant and
    workspace precisely because these tables have no tenant column and no RLS.
    If a checkpoint id collided across threads, that prefix would be decorative.
    """
    checkpoint_id = str(uuid.uuid4())
    first, second = f"p14r3-{uuid.uuid4()}", f"p14r3-{uuid.uuid4()}"

    for thread_id, value in ((first, "a"), (second, "b")):
        await saver.aput(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            _checkpoint(checkpoint_id, value), {}, {})

    assert await _rows(saver, first) == 1
    assert await _rows(saver, second) == 1, (
        "one thread's checkpoint overwrote another's because they shared a "
        "checkpoint id. The thread id is what separates tenants here.")
