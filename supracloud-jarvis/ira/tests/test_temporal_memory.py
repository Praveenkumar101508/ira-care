"""Temporal + provenance memory (PR 71) — memory/temporal.py + migration 017.

DB-free: `acquire` is replaced with a fake capturing every statement, and the
embedder is stubbed. What these tests pin:

  - provenance fields travel through save/read SQL
  - supersession preserves the old row and is cross-user safe
  - soft deletion is user-scoped and idempotent-safe
  - active retrieval excludes deleted/superseded/expired and 'secret' rows
  - contradictions are DETECTED and surfaced as owner questions, never
    silently overwritten
  - migration 017 is strictly additive
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import memory.temporal as temporal
from memory.temporal import (

    ACTIVE_FILTER_SQL, Contradiction, find_contradictions,
)

from tests.conftest import make_workspace_principal

PRINCIPAL = make_workspace_principal()



class FakeConn:
    def __init__(self, *, fetchrow_result=None, fetch_result=None,
                 execute_result="UPDATE 1", execute_error=None):
        self.calls = []
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result or []
        self._execute_result = execute_result
        self._execute_error = execute_error

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self._execute_error:
            raise self._execute_error
        return self._execute_result

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._fetchrow_result

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch_result


def _wire(monkeypatch, conn):
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    async def fake_embed(text):
        return [0.1, 0.2, 0.3]

    __import__("tests.conftest", fromlist=["x"]).patch_db_connections(monkeypatch, conn)
    monkeypatch.setattr(temporal, "_embed", fake_embed)
    return conn


# ── save_memory ───────────────────────────────────────────────────────────────

def test_save_validates_inputs(monkeypatch):
    _wire(monkeypatch, FakeConn())
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "  ", user_id="owner"))
    with pytest.raises(ValueError, match="memory_type"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "x", user_id="owner", memory_type="gossip"))
    with pytest.raises(ValueError, match="sensitivity"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "x", user_id="owner", sensitivity="loud"))
    with pytest.raises(ValueError, match="confidence"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "x", user_id="owner", confidence=1.5))
    with pytest.raises(ValueError, match="user_id"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "x", user_id=""))


def test_save_writes_provenance_columns(monkeypatch):
    conn = _wire(monkeypatch, FakeConn(execute_result="INSERT 0 1"))
    mem_id = asyncio.run(temporal.save_memory(PRINCIPAL, 
        "The deadline is 18 July", user_id="owner", memory_type="commitment",
        source="document:contract.pdf", sensitivity="shared",
        confidence=0.8, owner_confirmed=True))
    uuid.UUID(mem_id)  # valid id returned
    kind, sql, args = conn.calls[0]
    assert kind == "execute" and "INSERT INTO memory_embeddings" in sql
    for col in ("memory_type", "source", "sensitivity", "confidence",
                "owner_confirmed", "valid_from", "valid_until", "embedding_version"):
        assert col in sql
    assert "commitment" in args and "document:contract.pdf" in args
    assert "owner" in args and 0.8 in args and True in args


def test_save_failure_is_loud_not_silent(monkeypatch):
    _wire(monkeypatch, FakeConn(execute_error=RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(temporal.save_memory(PRINCIPAL, "x", user_id="owner"))


# ── supersede ─────────────────────────────────────────────────────────────────

def test_supersede_links_old_to_new_scoped_by_user(monkeypatch):
    new_id, old_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn = _wire(monkeypatch, FakeConn(
        fetchrow_result={"user_id": "owner"}, execute_result="UPDATE 1"))
    ok = asyncio.run(temporal.supersede(PRINCIPAL, old_id, new_id, user_id="owner",
                                        reason="newer document"))
    assert ok is True
    kind, sql, args = conn.calls[-1]
    assert "SET superseded_by" in sql
    assert "user_id = $3" in sql and "superseded_by IS NULL" in sql
    assert "DELETE" not in sql.upper()  # supersession never destroys


def test_supersede_refuses_foreign_replacement(monkeypatch):
    conn = _wire(monkeypatch, FakeConn(fetchrow_result={"user_id": "rahul"}))
    ok = asyncio.run(temporal.supersede(PRINCIPAL, str(uuid.uuid4()), str(uuid.uuid4()),
                                        user_id="owner"))
    assert ok is False
    assert all(kind != "execute" for kind, _, _ in conn.calls), \
        "no UPDATE may run when the replacement belongs to another user"


def test_supersede_reports_no_match(monkeypatch):
    _wire(monkeypatch, FakeConn(fetchrow_result={"user_id": "owner"},
                                execute_result="UPDATE 0"))
    ok = asyncio.run(temporal.supersede(PRINCIPAL, str(uuid.uuid4()), str(uuid.uuid4()),
                                        user_id="owner"))
    assert ok is False


# ── soft_forget ───────────────────────────────────────────────────────────────

def test_soft_forget_sets_deleted_at_user_scoped(monkeypatch):
    conn = _wire(monkeypatch, FakeConn(execute_result="UPDATE 1"))
    assert asyncio.run(temporal.soft_forget(PRINCIPAL, str(uuid.uuid4()), user_id="owner")) is True
    _, sql, args = conn.calls[0]
    assert "SET deleted_at = NOW()" in sql
    assert "user_id = $2" in sql and "deleted_at IS NULL" in sql
    assert "owner" in args


def test_soft_forget_missing_row_returns_false(monkeypatch):
    _wire(monkeypatch, FakeConn(execute_result="UPDATE 0"))
    assert asyncio.run(temporal.soft_forget(PRINCIPAL, str(uuid.uuid4()), user_id="owner")) is False


# ── retrieve_active ───────────────────────────────────────────────────────────

def test_retrieve_active_filters_dead_and_secret_rows(monkeypatch):
    conn = _wire(monkeypatch, FakeConn(fetch_result=[]))
    asyncio.run(temporal.retrieve_active(PRINCIPAL, "deadline", user_id="owner"))
    _, sql, args = conn.calls[0]
    assert ACTIVE_FILTER_SQL in sql, "active filter must be the shared constant"
    assert "user_id = $2" in sql
    sens = args[2]
    assert "secret" not in sens and "private" in sens  # secret opt-in only


def test_retrieve_active_rejects_unknown_sensitivity(monkeypatch):
    _wire(monkeypatch, FakeConn())
    with pytest.raises(ValueError, match="sensitivities"):
        asyncio.run(temporal.retrieve_active(PRINCIPAL, "q", user_id="owner",
                                             include_sensitivities=("loud",)))


def test_retrieve_active_fails_soft(monkeypatch):
    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db down")
    conn = FakeConn()
    conn.fetch = Boom().fetch
    _wire(monkeypatch, conn)
    assert asyncio.run(temporal.retrieve_active(PRINCIPAL, "q", user_id="owner")) == []


# ── Contradiction detection ───────────────────────────────────────────────────

def _mem(content, sim, mem_id="m1", mtype="fact"):
    return {"id": mem_id, "content": content, "similarity": sim,
            "memory_type": mtype}


def test_deadline_conflict_detected_with_values():
    hits = find_contradictions(
        "The deadline is 22 July",
        [_mem("The deadline is 18 July", 0.93)])
    assert len(hits) == 1
    c = hits[0]
    assert c.existing_id == "m1" and c.similarity == 0.93
    assert "18july" in c.conflicting_values and "22july" in c.conflicting_values
    q = c.question()
    assert "18 July" in q and "22 July" in q
    assert "replace" in q and "retained" in q  # asks, never overwrites


def test_identical_statement_is_not_a_contradiction():
    assert find_contradictions("The deadline is 18 July",
                               [_mem("the deadline is  18 july", 0.99)]) == []


def test_low_similarity_not_flagged():
    assert find_contradictions("The deadline is 22 July",
                               [_mem("I like green tea", 0.30)]) == []


def _stand_in_retrieve(monkeypatch, rows):
    """Replace `retrieve_active`, accepting only calls the real one accepts.

    The previous stand-in declared `(query, *, user_id, ...)`: the signature
    `retrieve_active` had before it took a principal. `check_before_save` kept
    calling it that way, the real function raised TypeError on every call, and
    this test passed (P1-04). Binding each call to the real signature makes the
    stand-in fail wherever the real function would.
    """
    real = inspect.signature(temporal.retrieve_active)
    calls: list[dict] = []

    async def fake_retrieve(*args, **kwargs):
        calls.append(dict(real.bind(*args, **kwargs).arguments))
        return rows

    monkeypatch.setattr(temporal, "retrieve_active", fake_retrieve)
    return calls


def test_check_before_save_screens_same_type_only(monkeypatch):
    _stand_in_retrieve(monkeypatch, [_mem("Deadline is 18 July", 0.95, "a", "commitment"),
                                     _mem("Deadline is 19 July", 0.95, "b", "fact")])
    hits = asyncio.run(temporal.check_before_save(PRINCIPAL,
        "Deadline is 22 July", user_id="owner", memory_type="commitment"))
    assert [h.existing_id for h in hits] == ["a"]


def test_check_before_save_reads_as_the_callers_principal(monkeypatch):
    calls = _stand_in_retrieve(monkeypatch, [])
    asyncio.run(temporal.check_before_save(PRINCIPAL, "Deadline is 22 July", user_id="owner"))
    [call] = calls
    assert call["principal"] is PRINCIPAL
    assert call["query"] == "Deadline is 22 July"
    assert call["user_id"] == "owner"


def test_a_failed_check_is_reported_as_unchecked_not_as_clear(monkeypatch):
    """A failed read must not look like "checked, nothing found": the guard
    saves anyway, and says `contradiction_checked=False`."""
    from memory.contradiction_guard import guarded_save

    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db down")
    conn = FakeConn()
    conn.fetch = Boom().fetch
    _wire(monkeypatch, conn)

    result = asyncio.run(guarded_save(PRINCIPAL, "Deadline is 22 July", user_id="owner",
                                      auto_save_when_clear=False))
    assert result == {"status": "clear", "contradiction_checked": False,
                      "content": "Deadline is 22 July"}


# ── Migration 017 shape ───────────────────────────────────────────────────────

def _migration_text():
    p = Path(__file__).resolve().parent.parent.parent / "postgres" / "017_temporal_memory.sql"
    return p.read_text(encoding="utf-8")


def test_migration_exists_and_orders_after_016():
    d = Path(__file__).resolve().parent.parent.parent / "postgres"
    names = sorted(f.name for f in d.glob("0*.sql"))
    assert "017_temporal_memory.sql" in names
    assert names.index("017_temporal_memory.sql") == names.index("016_command_center.sql") + 1


def test_migration_is_strictly_additive():
    sql_lines = [l for l in _migration_text().splitlines()
                 if not l.strip().startswith("--")]
    body = "\n".join(sql_lines)
    assert "ADD COLUMN IF NOT EXISTS memory_type" in body
    assert "superseded_by" in body and "deleted_at" in body
    assert "DROP TABLE" not in body.upper()
    assert "DROP COLUMN" not in body.upper()   # rollback lives in comments only
    assert "DELETE FROM" not in body.upper()


def test_migration_covers_all_memory_types():
    body = _migration_text()
    for t in temporal.MEMORY_TYPES:
        assert f"'{t}'" in body, f"memory type {t} missing from CHECK constraint"
