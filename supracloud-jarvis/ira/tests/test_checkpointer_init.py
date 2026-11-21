"""`init_checkpointer` uses the tables migration 097 made, and says why when it cannot.

Until 097, `init_checkpointer` always called LangGraph's `setup()`. `setup()`
begins with `CREATE TABLE IF NOT EXISTS checkpoint_migrations`, PostgreSQL checks
CREATE on the schema before it looks at IF NOT EXISTS, and the runtime role has
no CREATE on `public` (042). So every native install fell back to an in-memory
checkpointer, with a warning telling the owner to install a package that was
installed (docs/testing/evidence/install-check-listeners-2026-10-23).

These tests pin the decision, with a fake saver standing in for LangGraph's, so
they run where `langgraph-checkpoint-postgres` is not installed (CI):

  * tables at LangGraph's latest migration: `setup()` is NOT called;
  * tables missing or older: `setup()` is called (a role that may create them,
    such as a development superuser, still works);
  * `setup()` refused: memory, and the warning says to run the migrations;
  * newer tables than this LangGraph: used as they are, with a warning;
  * package missing: memory, and only then the "install" advice;
  * database unreachable: memory, and the warning does not claim the tables
    are missing, because nothing looked.

tests/tenancy/test_migration_097_checkpoint_tables.py covers the tables
themselves on a real PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import sys
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

import agents.graph as graph  # noqa: E402

LATEST = 9  # langgraph-checkpoint-postgres 3.1.3 has ten migrations, 0-9
AIO = "langgraph.checkpoint.postgres.aio"


class _Cursor:
    def __init__(self, conn):
        self._conn, self._query = conn, ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, *args):
        self._query = query

    async def fetchone(self):
        if "to_regclass" in self._query:
            return {"present": self._conn.present}
        if "max(v)" in self._query:
            return {"v": self._conn.recorded}
        raise AssertionError(f"unexpected query: {self._query}")


class _Conn:
    def __init__(self, present, recorded):
        self.present, self.recorded = present, recorded

    def cursor(self):
        return _Cursor(self)


def _fake_saver(*, present=True, recorded=LATEST, setup_error=None, enter_error=None):
    """A stand-in for AsyncPostgresSaver: records `setup()` calls and closes."""

    class FakeSaver:
        MIGRATIONS = ["-- migration"] * (LATEST + 1)
        made: list = []
        contexts: list = []

        def __init__(self):
            self.setup_calls = 0
            self.conn = _Conn(present, recorded)

        @classmethod
        def from_conn_string(cls, dsn):
            ctx = _Ctx(cls)
            cls.contexts.append(ctx)
            return ctx

        async def setup(self):
            self.setup_calls += 1
            if setup_error is not None:
                raise setup_error

    class _Ctx:
        def __init__(self, cls):
            self.cls, self.exited = cls, False

        async def __aenter__(self):
            if enter_error is not None:
                raise enter_error
            saver = self.cls()
            self.cls.made.append(saver)
            return saver

        async def __aexit__(self, *exc):
            self.exited = True

    return FakeSaver


class _Compiled:
    def __init__(self, checkpointer):
        self.checkpointer = checkpointer


class _Builder:
    def compile(self, checkpointer):
        return _Compiled(checkpointer)


@pytest.fixture
def install(monkeypatch):
    """Put a fake saver where `init_checkpointer` imports it from."""
    monkeypatch.setattr(graph, "build_graph", lambda: _Builder())
    monkeypatch.setattr(graph, "_checkpointer", None)
    monkeypatch.setattr(graph, "_compiled_graph", None)
    monkeypatch.setattr(graph, "_pg_checkpointer_ctx", None)

    def _install(saver_cls):
        for name in ("langgraph.checkpoint.postgres",):
            monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
        monkeypatch.setitem(sys.modules, AIO, types.SimpleNamespace(AsyncPostgresSaver=saver_cls))
        return saver_cls

    return _install


def _memory():
    from langgraph.checkpoint.memory import MemorySaver
    return isinstance(graph._compiled_graph.checkpointer, MemorySaver)


async def test_current_tables_skip_setup(install, caplog):
    saver = install(_fake_saver(recorded=LATEST))
    with caplog.at_level(logging.WARNING, logger="ira.graph"):
        await graph.init_checkpointer("postgresql://app@db/ira")
    assert saver.made[0].setup_calls == 0, (
        "setup() ran although 097's tables are current; as the runtime role it "
        "fails with 'permission denied for schema public' and IRA drops to memory")
    assert graph._compiled_graph.checkpointer is saver.made[0]
    assert not caplog.records


async def test_missing_tables_run_setup(install):
    saver = install(_fake_saver(present=False, recorded=None))
    await graph.init_checkpointer("postgresql://dev@db/ira")
    assert saver.made[0].setup_calls == 1
    assert graph._compiled_graph.checkpointer is saver.made[0]


async def test_older_tables_run_setup(install):
    saver = install(_fake_saver(recorded=LATEST - 3))
    await graph.init_checkpointer("postgresql://dev@db/ira")
    assert saver.made[0].setup_calls == 1
    assert graph._compiled_graph.checkpointer is saver.made[0]


async def test_refused_setup_falls_back_and_names_the_cause(install, caplog):
    saver = install(_fake_saver(present=False, recorded=None,
                                setup_error=Exception("permission denied for schema public")))
    with caplog.at_level(logging.WARNING, logger="ira.graph"):
        await graph.init_checkpointer("postgresql://app@db/ira")
    assert _memory()
    message = caplog.text
    assert "No checkpoint tables" in message
    assert "Run the database migrations" in message
    assert "Install langgraph-checkpoint-postgres" not in message, (
        "the old warning: it sent the owner to install a package that was installed")
    assert saver.contexts[0].exited, "the saver's connection was left open"


async def test_newer_tables_are_used_as_they_are(install, caplog):
    saver = install(_fake_saver(recorded=LATEST + 2))
    with caplog.at_level(logging.WARNING, logger="ira.graph"):
        await graph.init_checkpointer("postgresql://app@db/ira")
    assert saver.made[0].setup_calls == 0
    assert graph._compiled_graph.checkpointer is saver.made[0]
    assert "newer" in caplog.text


async def test_missing_package_says_install(install, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, AIO, None)  # `import` raises ImportError
    monkeypatch.setattr(graph, "build_graph", lambda: _Builder())
    with caplog.at_level(logging.WARNING, logger="ira.graph"):
        await graph.init_checkpointer("postgresql://app@db/ira")
    assert _memory()
    assert "Install langgraph-checkpoint-postgres" in caplog.text


async def test_unreachable_database_does_not_claim_the_tables_are_missing(install, caplog):
    install(_fake_saver(enter_error=OSError("connection refused")))
    with caplog.at_level(logging.WARNING, logger="ira.graph"):
        await graph.init_checkpointer("postgresql://app@db/ira")
    assert _memory()
    assert "could not be read" in caplog.text
    assert "No checkpoint tables" not in caplog.text


async def test_a_second_start_never_reuses_the_first_saver(install, monkeypatch):
    first = install(_fake_saver(recorded=LATEST))
    await graph.init_checkpointer("postgresql://app@db/ira")
    assert graph._compiled_graph.checkpointer is first.made[0]
    monkeypatch.setitem(sys.modules, AIO, None)
    await graph.init_checkpointer("postgresql://app@db/ira")
    assert _memory(), "a failed second start kept the first start's saver"
