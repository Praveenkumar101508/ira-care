"""The coding agent and the architect are off unless the owner turns them on.

STATUS.md lists both under "Experimental (flag-gated, OFF by default)", but neither
had a flag, and their chat detectors also match everyday requests. On `main` at
e193eba5, through the real chat route:

- "find the file I saved yesterday" started a coding task: Aider on a new branch of
  IRA's own source, then the test suite, then a commit;
- "what's missing from my shopping list" started the five-agent architect debate;
- "my leave got approved" asked the architect to write code for a feature.

With `IRA_CODING_AGENT_ENABLED` and `IRA_ARCHITECT_ENABLED` unset, those requests
reach normal chat, a message that names the architect is told it is off, the
/architect routes that run it answer 503, and the 12-hour background cycle does
not run. With the flags on, everything behaves as it did.

The chat-route tests drive `chat_stream` itself, with the model, the database and
search replaced. Reaching search is how a test knows a request got to normal chat.
"""
from __future__ import annotations

import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import json
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

# Not a lockdown suite: the lockdown state is pinned INACTIVE here, because
# with no Redis or PostgreSQL it reads UNKNOWN and refuses high-risk actions.
# See `lockdown_inactive` in tests/conftest.py; the policy has its own suites.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)

import agents.architect_agent as aa
import agents.coding_agent as ca
import api.routes.architect as arch_routes
import api.routes.chat as chatmod
from api.routes.chat import ChatRequest, chat_stream

FLAGS = ("IRA_CODING_AGENT_ENABLED", "IRA_ARCHITECT_ENABLED")

# Everyday requests that the detectors match. Measured on main at e193eba5.
CODING_PHRASES = [
    "find the file I saved yesterday",
    "write a readme for my project",
    "change the class time to 6pm",
    "remind me to fix the bug in the garden hose",
]
ARCHITECT_PHRASES = [
    "what's missing from my shopping list",
    "my leave got approved",
]


class _Cfg:
    ira_admin_username = "owner"
    ira_voice_service_username = "ira-voice"
    dev_mode = True            # skips the biometric/restricted-domain gate
    owner_name = "Praveen"


class _ReachedNormalChat(Exception):
    """Raised by the search stub: the request got past every special route."""


@pytest.fixture
def flags_off(monkeypatch):
    for key in FLAGS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def route(monkeypatch):
    """Drive the real chat route and report which branch took the request."""
    calls: dict[str, list] = {"coding": [], "proposal": [], "implement": [], "graph": []}

    async def _coding(instruction, repo_path=None, *, is_owner):
        calls["coding"].append(instruction)
        return {"ok": False, "reason": "test", "summary": "stub"}

    async def _proposal(query, memory_context="", background_mode=False):
        calls["proposal"].append(query)
        yield {"architect_done": True, "proposal": "", "latency_ms": 0}

    async def _implement(feature_name, proposal_context=""):
        calls["implement"].append(feature_name)
        yield {"implement_done": True, "implementation": ""}

    async def _graph(*a, **k):
        calls["graph"].append(a)
        raise _ReachedNormalChat("graph")

    async def _search(*a, **k):
        raise _ReachedNormalChat("search")

    async def _strategy(*a, **k):
        raise _ReachedNormalChat("strategy")

    monkeypatch.setattr(chatmod, "get_settings", lambda: _Cfg())
    monkeypatch.setattr("api.middleware.auth.get_settings", lambda: _Cfg())
    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv1"))
    monkeypatch.setattr(chatmod, "retrieve", AsyncMock(return_value=[]))
    monkeypatch.setattr(chatmod, "guard_chat_memory", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "_USE_CORTEX", False)
    monkeypatch.setattr(chatmod, "run_graph", _graph)
    # The real classifier: these requests are short, so it decides by keyword and
    # calls no model.
    monkeypatch.setattr("agents.coding_agent.run_coding_task", _coding)
    monkeypatch.setattr("agents.architect_agent.stream_architect_proposal", _proposal)
    monkeypatch.setattr("agents.architect_agent.stream_auto_implement", _implement)
    monkeypatch.setattr("api.routes.architect._get_state", AsyncMock(return_value={}))
    monkeypatch.setattr("api.routes.architect._set_state", AsyncMock())
    monkeypatch.setattr("utils.search_tools.get_search_context", _search)
    monkeypatch.setattr("agents.strategy_mode.run_strategy", _strategy)

    async def _send(message: str) -> tuple[str, list[dict]]:
        try:
            resp = await chat_stream(
                ChatRequest.model_validate({"message": message, "session_id": "s1"}), _user="owner")
        except _ReachedNormalChat as reached:
            # "graph": the classifier sent it to the career, digital or browser agents.
            return ("graph" if str(reached) == "graph" else "chat"), []
        events = []
        try:
            async for item in resp.body_iterator:
                data = item.get("data") if isinstance(item, dict) else None
                if data:
                    events.append(json.loads(data))
        except _ReachedNormalChat as reached:
            return str(reached), events   # "strategy": strategy mode took it
        if calls["coding"]:
            return "coding", events
        if calls["proposal"] or calls["implement"]:
            return "architect", events
        done = [e for e in events if e.get("done")]
        return (done[-1].get("agent") if done else "unknown"), events

    return _send, calls


# ── The flags ────────────────────────────────────────────────────────────────


def test_both_flags_are_off_by_default(flags_off):
    assert ca.coding_agent_enabled() is False
    assert aa.architect_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_the_owner_can_turn_each_one_on(monkeypatch, value):
    monkeypatch.setenv("IRA_CODING_AGENT_ENABLED", value)
    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", value)
    assert ca.coding_agent_enabled() is True
    assert aa.architect_enabled() is True


# ── Through the chat route ───────────────────────────────────────────────────


@pytest.mark.parametrize("phrase", CODING_PHRASES)
async def test_an_everyday_request_does_not_start_a_coding_task(flags_off, route, phrase):
    send, calls = route
    taken, _ = await send(phrase)
    assert calls["coding"] == [], f"{phrase!r} started a coding task with the agent off"
    assert taken == "chat", f"{phrase!r} was taken by {taken!r}"


@pytest.mark.parametrize("phrase", ARCHITECT_PHRASES)
async def test_an_everyday_request_does_not_start_the_architect(flags_off, route, phrase):
    send, calls = route
    taken, _ = await send(phrase)
    assert calls["proposal"] == [] and calls["implement"] == [], (
        f"{phrase!r} reached the architect with it off: proposal={calls['proposal']}, "
        f"implement={calls['implement']}")
    assert taken == "chat", f"{phrase!r} was taken by {taken!r}"


@pytest.mark.parametrize("command", [
    "architect propose new features",   # what the chat suggestion and button send
    "architect apply",
    "IRA, architect implement the dark mode",
])
async def test_naming_the_architect_says_it_is_off(flags_off, route, command):
    send, calls = route
    taken, events = await send(command)
    assert taken == "architect_off"
    assert calls["proposal"] == [] and calls["implement"] == []
    said = "".join(e.get("token", "") for e in events)
    assert "turned off" in said and "IRA_ARCHITECT_ENABLED=true" in said


async def test_a_sentence_that_mentions_an_architect_is_ordinary_chat(flags_off, route):
    send, _ = route
    taken, _ = await send("my sister is an architect in Hyderabad")
    assert taken == "chat", f"taken by {taken!r}"


# Positive controls: with the flags on, the same route still reaches both, so the
# tests above fail for the right reason and not because the harness is broken.

async def test_with_the_coding_agent_on_a_coding_request_reaches_it(monkeypatch, route):
    monkeypatch.setenv("IRA_CODING_AGENT_ENABLED", "true")
    monkeypatch.delenv("IRA_ARCHITECT_ENABLED", raising=False)
    send, calls = route
    taken, _ = await send("fix the bug in voice/tts.py")
    assert taken == "coding" and calls["coding"] == ["fix the bug in voice/tts.py"]


async def test_with_the_architect_on_a_proposal_request_reaches_it(monkeypatch, route):
    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", "true")
    monkeypatch.delenv("IRA_CODING_AGENT_ENABLED", raising=False)
    send, calls = route
    taken, _ = await send("architect propose new features")
    assert taken == "architect" and calls["proposal"] == ["architect propose new features"]


# ── The coding agent itself ──────────────────────────────────────────────────


async def test_the_coding_agent_refuses_any_caller_while_off(flags_off, monkeypatch):
    """Checked inside the agent too, so a caller other than chat cannot start one."""
    ran = []
    monkeypatch.setattr(ca, "_run_sync", lambda *a: ran.append(a) or {"ok": True})
    result = await ca.run_coding_task("fix the bug in x.py", "/tmp/repo", is_owner=True)
    assert result["reason"] == "disabled" and result["ok"] is False
    assert ran == [], "the agent touched the repository while off"
    assert ca.spoken_result(result) == "The coding agent is turned off on this computer."


# ── The /architect routes ────────────────────────────────────────────────────


@pytest.mark.parametrize("call", ["propose", "implement", "apply"])
async def test_the_routes_that_run_the_architect_answer_503_while_off(flags_off, monkeypatch, call):
    ran = []
    monkeypatch.setattr("agents.architect_agent.stream_architect_proposal",
                        lambda *a, **k: ran.append("proposal"))
    monkeypatch.setattr("agents.architect_agent.stream_auto_implement",
                        lambda *a, **k: ran.append("implement"))
    monkeypatch.setattr("utils.auto_implement.apply_implementation",
                        AsyncMock(side_effect=lambda *a, **k: ran.append("apply")))
    monkeypatch.setattr(arch_routes, "retrieve", AsyncMock(return_value=[]))
    monkeypatch.setattr(arch_routes, "_get_state", AsyncMock(return_value={"implementation": "DIFF"}))
    with pytest.raises(HTTPException) as caught:
        if call == "propose":
            await arch_routes.architect_propose(arch_routes.ProposeRequest(), _user="owner", _p=None)
        elif call == "implement":
            await arch_routes.architect_implement(
                arch_routes.ImplementRequest(feature_name="x"), _user="owner", _p=None)
        else:
            await arch_routes.architect_apply(arch_routes.ApplyRequest(), _user="owner")
    assert caught.value.status_code == 503
    assert ran == []


async def test_status_says_whether_the_architect_is_on(flags_off, monkeypatch):
    monkeypatch.setattr(arch_routes, "_get_state", AsyncMock(return_value=dict(arch_routes._DEFAULT_STATE)))
    assert (await arch_routes.architect_status(_user="owner"))["enabled"] is False
    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", "true")
    assert (await arch_routes.architect_status(_user="owner"))["enabled"] is True


async def test_with_the_architect_on_apply_runs_as_before(monkeypatch):
    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", "true")
    monkeypatch.setattr(arch_routes, "_get_state", AsyncMock(return_value={"implementation": None}))
    result = await arch_routes.architect_apply(arch_routes.ApplyRequest(), _user="owner")
    assert result["success"] is False and "No pending implementation" in result["message"]


# ── The 12-hour background cycle ─────────────────────────────────────────────


async def test_the_background_cycle_does_not_run_while_off(flags_off, monkeypatch):
    ran = []

    async def _proposal(*a, **k):
        ran.append(a)
        yield {"architect_done": True, "proposal": ""}

    monkeypatch.setattr(aa, "stream_architect_proposal", _proposal)
    await aa.run_background_architect_cycle()
    assert ran == [], "the architect ran a model cycle with the flag off"


async def test_with_the_architect_on_the_background_cycle_runs(monkeypatch):
    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", "true")
    ran = []

    async def _proposal(*a, **k):
        ran.append(a)
        yield {"architect_done": True, "proposal": ""}   # empty: returns before notifying

    monkeypatch.setattr(aa, "stream_architect_proposal", _proposal)
    await aa.run_background_architect_cycle()
    assert len(ran) == 1


# ── The documented install path ──────────────────────────────────────────────


def test_the_env_template_ships_both_off():
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    values = dict(
        line.split("=", 1) for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line)
    for key in FLAGS:
        assert values[key].split("#")[0].strip().lower() == "false", f"{key} must ship false"
