"""P0 performance (10 Nov 2026): semantic memory retrieval runs only when a turn can
use it. A first turn about the world or a pasted document skips the embedding, the
vector queries and the memory block; a turn that refers to the user's own facts, a
memory command, or any follow-up (history present) retrieves as before. The owner
profile is still injected every turn. The gate is opt-in (IRA_MEMORY_RETRIEVAL_GATE=true):
with the flag unset or false, retrieval runs on every turn as before, because the
gate trades recall on neutral-phrased first-turn questions about stored facts.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.test_injection_framing_routes import _chat, _owner_app, _stream, stand_in  # noqa: E402,F401
from utils.memory_gate import message_refers_to_memory, retrieval_wanted  # noqa: E402


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    """These tests describe the gate when it is enabled; a test that wants the
    default (off) sets the flag to "false" itself."""
    monkeypatch.setenv("IRA_MEMORY_RETRIEVAL_GATE", "true")

RECALLS = [
    "SYNTHETIC check: what is my locker code? Reply with it only.",
    "SYNTHETIC check, new chat: tell me my library card number. Reply with it only.",
    "Remember that my SYNTHETIC parcel tracking code is K2210-A.",
    "SYNTHETIC check: what is my bank PIN? If you were never told, say so.",
    "remind me what I said about the garden hose reel",
    "do you know our wifi name?",
]
WORLD = [
    "SYNTHETIC: what is the capital of Peru? One word.",
    "SYNTHETIC: Spell the word 'drawer' backwards.",
    "who prepared this invoice? Reply with the name only.\n\n---\nSYNTHETIC doc 44\nPrepared by Priya Natarajan\nTotal amount: 5725 EUR",
    "hello",
    "summarise this customer email in one sentence. Email: \"Hello, invoice 4471 is due on 30 April.\"",
]


@pytest.mark.parametrize("message", RECALLS)
def test_a_message_about_the_users_own_facts_retrieves(message):
    assert message_refers_to_memory(message)
    assert retrieval_wanted(message, has_history=False)


@pytest.mark.parametrize("message", WORLD)
def test_a_first_turn_about_the_world_or_a_document_does_not(message):
    assert not message_refers_to_memory(message)
    assert not retrieval_wanted(message, has_history=False)


@pytest.mark.parametrize("message", WORLD)
def test_a_follow_up_always_retrieves(message):
    assert retrieval_wanted(message, has_history=True)


def test_the_flag_restores_retrieval_on_every_turn(monkeypatch):
    monkeypatch.setenv("IRA_MEMORY_RETRIEVAL_GATE", "false")
    assert all(retrieval_wanted(m, has_history=False) for m in WORLD)


def test_the_gate_is_off_unless_the_flag_enables_it(monkeypatch):
    monkeypatch.delenv("IRA_MEMORY_RETRIEVAL_GATE", raising=False)
    assert all(retrieval_wanted(m, has_history=False) for m in WORLD)


def test_every_memory_prompt_of_the_acceptance_dataset_retrieves_and_no_document_prompt_does():
    import json
    import pathlib
    d = pathlib.Path(__file__).resolve().parents[3] / "benchmarks" / "acceptance" / "dataset"
    if not d.exists():
        pytest.skip("dataset not checked out")
    mem = [i["message"] for i in json.load(open(d / "memory.json"))["items"]]
    docs = [i["message"] for i in json.load(open(d / "documents.json"))["items"]]
    assert all(message_refers_to_memory(m) for m in mem), [m for m in mem if not message_refers_to_memory(m)][:3]
    assert not any(message_refers_to_memory(m) for m in docs), [m[:60] for m in docs if message_refers_to_memory(m)][:3]


async def _calls(monkeypatch, message, route, history=()):
    import agents.graph as graph
    import api.routes.chat as chatmod
    app = _owner_app(monkeypatch)
    rc = AsyncMock(return_value=[]); rg = AsyncMock(return_value=[])
    monkeypatch.setattr(chatmod, "retrieve", rc)
    monkeypatch.setattr(graph, "retrieve", rg)
    monkeypatch.setattr(chatmod, "get_recent_messages", AsyncMock(return_value=list(history)))
    if route == "stream":
        await _stream(app, message)
    else:
        await _chat(app, message)
    return rc.await_count + rg.await_count


@pytest.mark.parametrize("route", ["stream", "chat"])
async def test_both_routes_skip_retrieval_for_a_world_question_and_run_it_for_a_recall(stand_in, monkeypatch, route):  # noqa: F811
    assert await _calls(monkeypatch, WORLD[0], route) == 0
    assert await _calls(monkeypatch, RECALLS[0], route) == 1
    assert len(stand_in.answers) == 2, "both turns still reached the model"


async def test_the_stream_route_retrieves_on_a_follow_up(stand_in, monkeypatch):  # noqa: F811
    history = [{"role": "user", "content": "I'm planning a trip to Oslo. Say OK."}, {"role": "assistant", "content": "OK."}]
    assert await _calls(monkeypatch, "Name one thing I could see in that city, naming the city.", "stream", history) == 1


async def test_the_flag_restores_retrieval_on_the_routes(stand_in, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IRA_MEMORY_RETRIEVAL_GATE", "false")
    assert await _calls(monkeypatch, WORLD[0], "stream") == 1
    assert await _calls(monkeypatch, WORLD[0], "chat") == 1


async def test_a_dispatched_turn_no_longer_pays_for_retrieval(stand_in, monkeypatch):  # noqa: F811
    """The feature dispatcher used to run after retrieval, so a dispatched turn
    embedded and searched for nothing. It now runs first."""
    from fastapi.responses import JSONResponse
    import api.routes._dispatch as dispatch_mod
    import api.routes.chat as chatmod

    async def _dispatch(req, user):
        return JSONResponse({"dispatched": True}) if "DISPATCH-ME" in req.message else None

    monkeypatch.setenv("IRA_MEMORY_RETRIEVAL_GATE", "false")  # retrieval would run on every turn
    app = _owner_app(monkeypatch)          # builds the app first: it reloads route modules
    monkeypatch.setattr(dispatch_mod, "dispatch", _dispatch)
    rc = AsyncMock(return_value=[])
    monkeypatch.setattr(chatmod, "retrieve", rc)
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.post("/api/v1/chat/stream", json={"message": "DISPATCH-ME: what is the capital of Peru? One word.", "session_id": "s1"})
    assert r.status_code == 200, r.text
    assert r.text.strip().startswith("{"), r.text[:300]
    assert r.json() == {"dispatched": True}
    assert rc.await_count == 0, "retrieval ran before the dispatcher answered"
