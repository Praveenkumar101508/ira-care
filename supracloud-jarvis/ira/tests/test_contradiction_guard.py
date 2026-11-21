"""Contradiction guard save-path wiring (PR 71 wiring) —
memory/contradiction_guard.py. temporal.* is stubbed; no DB."""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio

import pytest

import memory.contradiction_guard as guard
from memory.temporal import Contradiction

from tests.conftest import make_workspace_principal

PRINCIPAL = make_workspace_principal()




def test_clear_content_saves(monkeypatch):
    async def no_conflict(principal, content, *, user_id, memory_type="fact", top_k=5):
        return []
    saved = {}
    async def fake_save(principal, content, **kw):
        saved["content"] = content
        return "mem-123"
    monkeypatch.setattr("memory.temporal.check_before_save", no_conflict)
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)

    out = asyncio.run(guard.guarded_save(PRINCIPAL, "Deadline is 18 July", user_id="owner"))
    assert out["status"] == "saved" and out["memory_id"] == "mem-123"
    assert out["contradiction_checked"] is True
    assert saved["content"] == "Deadline is 18 July"


def test_conflict_does_not_save_and_asks_owner(monkeypatch):
    c = Contradiction(existing_id="old-1",
                      existing_content="Deadline is 18 July",
                      new_content="Deadline is 22 July",
                      similarity=0.93, conflicting_values=["18july", "22july"])
    async def conflict(principal, content, *, user_id, memory_type="fact", top_k=5):
        return [c]
    save_called = {"n": 0}
    async def fake_save(principal, content, **kw):
        save_called["n"] += 1
        return "should-not-happen"
    monkeypatch.setattr("memory.temporal.check_before_save", conflict)
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)

    out = asyncio.run(guard.guarded_save(PRINCIPAL, "Deadline is 22 July", user_id="owner"))
    assert out["status"] == "needs_owner_decision"
    assert save_called["n"] == 0, "must NOT save while a contradiction is unresolved"
    assert out["candidates"][0]["existing_id"] == "old-1"
    assert "18 July" in out["questions"][0] and "22 July" in out["questions"][0]
    assert out["pending"]["content"] == "Deadline is 22 July"


def test_detection_unavailable_saves_but_flags(monkeypatch):
    async def boom(principal, content, *, user_id, memory_type="fact", top_k=5):
        raise RuntimeError("db down")
    async def fake_save(principal, content, **kw):
        return "mem-x"
    monkeypatch.setattr("memory.temporal.check_before_save", boom)
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)

    out = asyncio.run(guard.guarded_save(PRINCIPAL, "x", user_id="owner"))
    assert out["status"] == "saved" and out["contradiction_checked"] is False


def test_resolve_replace_saves_and_supersedes(monkeypatch):
    saved = {}
    async def fake_save(principal, content, **kw):
        saved["content"] = content
        saved["owner_confirmed"] = kw.get("owner_confirmed")
        return "new-1"
    superseded = {}
    async def fake_supersede(principal, old_id, new_id, *, user_id, reason=""):
        superseded["old"], superseded["new"] = old_id, new_id
        return True
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)
    monkeypatch.setattr("memory.temporal.supersede", fake_supersede)

    pending = {"content": "Deadline is 22 July", "user_id": "owner",
               "memory_type": "commitment"}
    out = asyncio.run(guard.resolve_decision(PRINCIPAL, "replace", pending=pending,
                                             existing_id="old-1"))
    assert out["status"] == "saved" and out["superseded_old"] is True
    assert superseded == {"old": "old-1", "new": "new-1"}
    assert saved["owner_confirmed"] is True


def test_resolve_retain_both_saves_without_supersede(monkeypatch):
    async def fake_save(principal, content, **kw):
        return "new-2"
    called = {"supersede": 0}
    async def fake_supersede(*a, **kw):
        called["supersede"] += 1
        return True
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)
    monkeypatch.setattr("memory.temporal.supersede", fake_supersede)

    out = asyncio.run(guard.resolve_decision(PRINCIPAL, 
        "retain_both", pending={"content": "x", "user_id": "owner"}))
    assert out["status"] == "saved" and called["supersede"] == 0


def test_resolve_discard_does_not_save(monkeypatch):
    called = {"save": 0}
    async def fake_save(principal, content, **kw):
        called["save"] += 1
        return "x"
    monkeypatch.setattr("memory.temporal.save_memory", fake_save)
    out = asyncio.run(guard.resolve_decision(PRINCIPAL, 
        "discard", pending={"content": "x", "user_id": "owner"}))
    assert out["status"] == "discarded" and called["save"] == 0


def test_resolve_unknown_rejected():
    with pytest.raises(ValueError, match="unknown resolution"):
        asyncio.run(guard.resolve_decision(PRINCIPAL, 
            "maybe", pending={"content": "x", "user_id": "owner"}))
