""""Remember ..." in chat asks before it saves, and saves to the Memory Vault.

Decided by the owner on 2026-10-19. Before this, the owner's "remember X" saved
X at once, filed under the chat username. The Memory Vault lists and forgets
only its own namespace, so the fact was recalled by chat but could be neither
seen in the vault nor forgotten there.

Now:
  * the contradiction check runs, and nothing is saved;
  * no conflict -> ``needs_owner_approval`` carries the proposal (Save / Not now);
  * a conflict -> ``needs_owner_decision``, as before;
  * the owner's choice (``POST /api/v1/chat/memory/resolve``) files the memory
    under the Memory Vault's namespace, whatever the chat username is.

`memory.temporal` is stubbed; no database.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
import types
from unittest.mock import AsyncMock

import pytest

from memory.namespaces import VAULT_NAMESPACE
from memory.temporal import Contradiction
from tests.conftest import make_workspace_principal

PRINCIPAL = make_workspace_principal(is_owner=True)


@pytest.fixture
def temporal(monkeypatch):
    """`check_before_save` finds nothing; `save_memory` records any call."""
    checks: list[dict] = []
    saves = AsyncMock(return_value="mem-1")

    async def no_conflict(principal, content, *, user_id, memory_type="fact", top_k=5):
        checks.append({"content": content, "user_id": user_id})
        return []

    monkeypatch.setattr("memory.temporal.check_before_save", no_conflict)
    monkeypatch.setattr("memory.temporal.save_memory", saves)
    return types.SimpleNamespace(checks=checks, saves=saves)


def _guard(message: str, *, is_owner: bool = True, user: str = "admin"):
    from memory.chat_commands import guard_chat_memory

    return asyncio.run(guard_chat_memory(PRINCIPAL, message, user=user, is_owner=is_owner))


def test_remember_proposes_and_saves_nothing(temporal):
    out = _guard("remember my passport expires in March")

    assert out["status"] == "needs_owner_approval"
    assert out["pending"]["content"] == "my passport expires in March"
    assert temporal.saves.await_count == 0, "a remember command saved without approval"


def test_the_check_runs_against_the_memory_vault(temporal):
    _guard("remember my passport expires in March", user="admin")

    assert temporal.checks == [{"content": "my passport expires in March",
                                "user_id": VAULT_NAMESPACE}]


def test_a_conflict_still_asks_the_owner_to_choose(monkeypatch, temporal):
    clash = Contradiction(existing_id="old-1", existing_content="Deadline is 18 July",
                          new_content="Deadline is 22 July", similarity=0.93,
                          conflicting_values=["18july", "22july"])

    async def conflict(principal, content, *, user_id, memory_type="fact", top_k=5):
        return [clash]

    monkeypatch.setattr("memory.temporal.check_before_save", conflict)
    out = _guard("remember Deadline is 22 July")

    assert out["status"] == "needs_owner_decision"
    assert out["pending"]["user_id"] == VAULT_NAMESPACE
    assert temporal.saves.await_count == 0


def test_someone_other_than_the_owner_is_not_handled_here(temporal):
    assert _guard("remember my passport expires in March", is_owner=False) is None
    assert temporal.checks == [] and temporal.saves.await_count == 0


def test_an_ordinary_message_is_not_a_memory_command(temporal):
    assert _guard("what should I pack for March?") is None


def test_the_reply_asks_the_question():
    from memory.chat_commands import memory_reply

    text = memory_reply({"status": "needs_owner_approval",
                         "pending": {"content": "my passport expires in March"}})
    assert "Memory Vault" in text and "my passport expires in March" in text
    assert text.rstrip().endswith("”") and "?" in text


def test_save_files_the_approved_memory_in_the_vault(temporal):
    from memory.contradiction_guard import resolve_decision

    out = asyncio.run(resolve_decision(
        PRINCIPAL, "save",
        pending={"content": "my passport expires in March", "user_id": VAULT_NAMESPACE}))

    assert out["status"] == "saved" and out["superseded_old"] is False
    assert temporal.saves.await_args.kwargs["user_id"] == VAULT_NAMESPACE
    assert temporal.saves.await_args.kwargs["owner_confirmed"] is True


def test_the_resolve_route_files_in_the_vault_whatever_the_username(monkeypatch):
    from api.routes import memory_decisions as route
    from memory import contradiction_guard

    resolver = AsyncMock(return_value={"status": "saved", "resolution": "save"})
    monkeypatch.setattr(contradiction_guard, "resolve_decision", resolver)
    monkeypatch.setattr("api.middleware.auth.get_settings",
                        lambda: types.SimpleNamespace(ira_admin_username="admin"))
    body = route.MemoryResolutionRequest(
        resolution="save",
        pending=route.PendingMemoryDecision(content="my passport expires in March"))

    out = asyncio.run(route.resolve_memory_decision(body, _user="admin"))

    assert out["status"] == "saved"
    pending = resolver.await_args.kwargs["pending"]
    assert pending["user_id"] == VAULT_NAMESPACE, (
        "an approved memory was filed under the chat username, outside the vault")
    assert resolver.await_args.args[1] == "save"
