"""Typed lockdown commands are acted on before any agent is chosen.

Found by a clean install of main at c1d47fbf on 1 November 2026
(supracloud-jarvis/docs/testing/evidence/clean-install-final-2026-11-01, PR #201):

1. **The documented lift did not lift.** The lockdown alert tells the owner to type
   "IRA, lift lockdown". The chat page sends /api/v1/chat/stream, which ran the
   agent graph, and so the security agent's tools, only for the career, digital and
   browser agents; POST /api/v1/chat routes by keyword, and "lockdown" is not a
   security keyword. Both answered from the conversational model, and the
   lockdown stayed on.
2. **An engage answered 504 with the lockdown on.** The security agent ran the
   tool, then waited for a report from the model, which passed the time limit.

The real-route tests are in `tests/tenancy/test_lockdown_refuses_action_routes.py`
(real Redis and PostgreSQL). These are the unit job's share: the gate that decides
what counts as a typed command, the function both routes call, the security agent's
answer after a tool, and a ratchet that both routes ask before choosing an agent.
No database, no Redis: the tools and the state are stand-ins.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import agents.security as sec
import utils.security_tools as st
from config import get_settings

IRA = Path(__file__).resolve().parents[1]

LIFT = [
    "IRA, lift lockdown",
    "lift lockdown",
    "Ira lift lockdown.",
    "Lift the lockdown please",
    "please remove the lockdown",
    "end the lockdown",
    "disengage lockdown",
    "stop lockdown",
    "SYNTHETIC drill over, the suspicious intrusion was a test: lift lockdown. /no_think",
]
ENGAGE = [
    "IRA, lockdown",
    "lockdown now",
    "initiate lockdown",
    "engage the lockdown",
    "Stop! Lockdown now",
    "stop, lockdown now!",
    "lockdown, someone is at the door",
    "SYNTHETIC drill: suspicious intrusion detected on this machine, engage lockdown now. /no_think",
]
STATUS = [
    "lockdown status",
    "IRA, is lockdown on?",
    "is the lockdown still on?",
    "are we in lockdown?",
    "what's the lockdown status?",
    "Hey IRA, lockdown status",
]
#: Prose that names a lockdown, or a lock word, and is not a command.
NOT_COMMANDS = [
    "panic",
    "Don't panic, what was the 2020 lockdown like?",
    "lock down the budget for next month",
    "remove the lock on my bike",
    "cancel emergency lockdown",
    "I am in lockdown mode for exams this week",
    "Suspicious intrusion on this machine: I want a lockdown right away",
    "IRA, lockdown?",           # asked, not ordered
    "lift lockdown?",
    "lift lockdown. lockdown status",   # two different commands: neither
    "what is the weather",
    " ".join(["word"] * 20) + ", lift lockdown",   # pasted text
]


@pytest.mark.parametrize("words, intent", [*((w, "lift") for w in LIFT),
                                           *((w, "lockdown") for w in ENGAGE),
                                           *((w, "status") for w in STATUS)])
def test_a_typed_command_is_recognised(words, intent):
    assert sec.typed_lockdown_command(words) == intent


@pytest.mark.parametrize("words", NOT_COMMANDS)
def test_prose_is_not_a_command(words):
    """Running `_security_intent` on every chat message would engage a lockdown
    for "don't panic" or "lock down the budget"; the gate must not."""
    assert sec.typed_lockdown_command(words) is None


@pytest.mark.parametrize("words", ["is lockdown on", "lockdown status",
                                   "tell me if lockdown is on", "are we under lockdown"])
def test_a_status_question_is_not_read_as_an_engage(words):
    """The lock pattern matches any mention of lockdown, so a question asked of the
    security agent used to engage it."""
    assert sec._security_intent(words) == "status"


# ── answer_lockdown_command, with the tools and the state as stand-ins ────────


class _Tools:
    """Records each tool call; the state is whatever the test sets."""

    def __init__(self, monkeypatch, state: str = "inactive", fail: bool = False):
        self.calls: list[tuple] = []
        self.state = state
        self.fail = fail
        monkeypatch.setattr(st, "initiate_lockdown", self._engage)
        monkeypatch.setattr(st, "lift_lockdown", self._lift)
        monkeypatch.setattr(st, "get_lockdown_state", self._read)

    async def _engage(self, reason="", *, is_owner=False):
        self.calls.append(("engage", reason, is_owner))
        if self.fail:
            raise RuntimeError("store unreachable")
        return {"status": "lockdown_active", "message": "Lockdown engaged. TOOL"}

    async def _lift(self, *, is_owner=False):
        self.calls.append(("lift", is_owner))
        if self.fail:
            raise RuntimeError("store unreachable")
        return {"status": "normal", "message": "Lockdown lifted. TOOL"}

    async def _read(self):
        self.calls.append(("read",))
        return st.LockdownState(self.state)


def _answer(words, *, owner=True, voice=False, user="owner"):
    return asyncio.run(sec.answer_lockdown_command(
        words, is_owner=owner, is_voice=voice, user_id=user))


def test_the_owner_s_typed_lift_runs_the_tool_and_answers_with_its_message(monkeypatch):
    tools = _Tools(monkeypatch)
    assert _answer("IRA, lift lockdown") == "Lockdown lifted. TOOL"
    assert tools.calls == [("lift", True)]


def test_the_owner_s_typed_engage_runs_the_tool_with_the_usual_reason(monkeypatch):
    tools = _Tools(monkeypatch)
    assert _answer("IRA, lockdown") == "Lockdown engaged. TOOL"
    assert tools.calls == [("engage", "voice/chat command: IRA, lockdown", True)]


@pytest.mark.parametrize("state", ["active", "inactive", "unknown"])
def test_a_status_question_answers_the_state_it_reads(monkeypatch, state):
    tools = _Tools(monkeypatch, state=state)
    assert _answer("lockdown status") == sec.LOCKDOWN_STATUS_REPLIES[state]
    assert tools.calls == [("read",)], "a status question changed the lockdown"


@pytest.mark.parametrize("words", ["IRA, lift lockdown", "IRA, lockdown", "lockdown status"])
@pytest.mark.parametrize("state", ["active", "inactive"])
def test_a_non_owner_is_refused_and_the_state_is_not_read(monkeypatch, words, state):
    tools = _Tools(monkeypatch, state=state)
    assert _answer(words, owner=False) == sec.OWNER_ONLY
    assert tools.calls == [], "a non-owner's command reached a tool or the state"


@pytest.mark.parametrize("signal", ["is_voice", "voice_service"])
def test_a_spoken_lift_is_refused_without_a_tool(monkeypatch, signal):
    tools = _Tools(monkeypatch, state="active")
    if signal == "is_voice":
        reply = _answer("IRA, lift lockdown", voice=True)
    else:
        reply = _answer("IRA, lift lockdown", user=get_settings().ira_voice_service_username)
    assert reply == sec.LIFT_BY_VOICE_REFUSED
    assert tools.calls == []


@pytest.mark.parametrize("words", ["IRA, lockdown", "lockdown status"])
def test_a_spoken_engage_or_status_question_is_left_to_the_agents(monkeypatch, words):
    """This path is for typed commands. A voice turn's engage or question goes on
    to the agents, as before."""
    tools = _Tools(monkeypatch)
    assert _answer(words, voice=True) is None
    assert tools.calls == []


def test_anything_else_is_left_to_the_agents(monkeypatch):
    tools = _Tools(monkeypatch)
    assert _answer("Don't panic, what was the 2020 lockdown like?") is None
    assert tools.calls == []


@pytest.mark.parametrize("words, verb", [("IRA, lift lockdown", "lift"), ("IRA, lockdown", "engage")])
def test_a_tool_that_fails_is_reported_as_a_failure(monkeypatch, words, verb):
    _Tools(monkeypatch, fail=True)
    reply = _answer(words)
    assert reply.startswith(f"I could not {verb} the lockdown"), reply
    assert "RuntimeError" in reply and "lockdown status" in reply


# ── The security agent answers a lift or an engage without the model ──────────


@pytest.fixture
def no_model(monkeypatch):
    """A model that is never asked, and an events read that is never needed."""
    asked: list[list[dict]] = []

    async def _model(messages, **_k):
        asked.append(messages)
        return "a model's report"

    async def _events(limit=20):
        return []

    monkeypatch.setattr(sec, "chat_complete", _model)
    monkeypatch.setattr(sec, "_fetch_recent_events", _events)
    return asked


def _guardian(words, *, voice=False):
    return asyncio.run(sec.security_guardian({
        "is_owner": True, "user_query": words, "memory_context": "",
        "is_voice": voice, "user_id": "owner",
    }))


@pytest.mark.parametrize("words, reply", [
    ("suspicious intrusion, I want a lockdown right away", "Lockdown engaged. TOOL"),
    ("the intrusion was a test, lift lockdown", "Lockdown lifted. TOOL"),
])
def test_the_agent_answers_with_the_tool_s_message_and_does_not_ask_the_model(
        monkeypatch, no_model, words, reply):
    _Tools(monkeypatch)
    out = _guardian(words)
    assert out["final_response"] == reply
    assert out["model_used"] == "security_gate"
    assert no_model == [], "the agent waited for the model after the tool had acted"


def test_the_agent_still_refuses_a_spoken_lift(monkeypatch, no_model):
    tools = _Tools(monkeypatch, state="active")
    out = _guardian("IRA, lift lockdown", voice=True)
    assert out["final_response"] == sec.LIFT_BY_VOICE_REFUSED
    assert tools.calls == [] and no_model == []


def test_the_agent_gives_the_model_the_state_for_a_status_question(monkeypatch, no_model):
    tools = _Tools(monkeypatch, state="active")
    out = _guardian("any threats? is lockdown on?")
    assert out["final_response"] == "a model's report"
    assert tools.calls == [("read",)], "a status question changed the lockdown"
    context = "\n".join(m["content"] for m in no_model[0])
    assert sec.LOCKDOWN_STATUS_REPLIES["active"] in context


# ── Ratchet: both chat routes ask before choosing an agent ───────────────────

#: Calls that mean the route has gone on to an agent, a cache or a guard.
_LATER = {"cache_get", "guard_chat_memory", "_cortex_route", "classify", "run_graph",
          "is_coding_request", "is_strategy_request", "feed_cognitive_percept"}


def _first_lines(fn: ast.AsyncFunctionDef) -> dict[str, int]:
    first: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name and (name not in first or node.lineno < first[name]):
                first[name] = node.lineno
    return first


@pytest.mark.parametrize("route", ["chat", "chat_stream"])
def test_the_route_asks_about_a_lockdown_command_before_anything_else_answers(route):
    tree = ast.parse((IRA / "api" / "routes" / "chat.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == route)
    first = _first_lines(fn)
    assert "answer_lockdown_command" in first, f"{route} no longer asks answer_lockdown_command"
    asked = first["answer_lockdown_command"]
    later = {name: line for name, line in first.items() if name in _LATER and line < asked}
    assert not later, f"{route} reaches {later} before it acts on a lockdown command (line {asked})"

    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "answer_lockdown_command")
    kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kwargs == {"is_owner": "owner", "is_voice": "req.is_voice", "user_id": "_user"}, kwargs
