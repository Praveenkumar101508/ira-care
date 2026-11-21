"""The executor node refuses a non-owner by itself, before the model is asked.

Found in the S-212 review (B1): `agents/executor.py` read `is_owner` only to
answer the lockdown question, after the model had extracted a command, the
metacharacter check, the blocklist, the allowlist and the path check had all
run, and an attempt had been logged. The owner gate (`security/owner_gate.py`)
refuses "run ... command" to a non-owner, but the router sends "run ls -la" to
the executor on the keyword alone, and chat is granted to every workspace role,
so a delegated viewer reached the node on `POST /api/v1/chat`. The digital
agent (`agents/digital.py`) and the security agent (`agents/security.py`) each
ask who is asking; now the executor does too, in the digital agent's words.

Negative control, run and seen on 10 November 2026 with the refusal block at the
top of `executor()` deleted (9 lines): 5 failed, 2 passed. Every refusal case
failed on its first assertion with `'Here is the listing.' == 'Only the owner
...'`, the spied model's summary in place of the refusal, so the model had been
asked and the command run; the route case failed the same way with
`agent_used: 'executor'` in the body, so the graph had reached the node. The
owner control and the wording check passed, as they should. Restored, 7 passed.

Nothing here executes a command: the process starter raises if reached.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from agents import executor as ex  # noqa: E402
from tests.test_chat_reports_served_model import _chat_app, stand_in  # noqa: E402, F401

QUERY = "run ls -la"


@pytest.fixture
def spies(monkeypatch):
    """The model, the attempt log and the process starter, each recording its
    calls. The model would answer `ls -la`, an allowlisted command, so only the
    owner check stands between the query and a process."""
    calls = {"model": [], "log": []}
    replies = iter(["ls -la", "Here is the listing."])

    async def fake_chat(messages, *_a, **_k):
        calls["model"].append(messages)
        return next(replies)

    async def fake_log(*args, **_k):
        calls["log"].append(args)

    async def no_process(*_a, **_k):
        raise AssertionError("a process was started")

    def no_run(*_a, **_k):
        raise AssertionError("a process was started")

    monkeypatch.setattr(ex, "chat_complete", fake_chat)
    monkeypatch.setattr(ex, "_log_exec_attempt", fake_log)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)
    monkeypatch.setattr(ex.subprocess, "run", no_run)
    return calls


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_before_the_model_is_asked(spies):
    out = await ex.executor({"user_query": QUERY, "is_owner": False, "session_id": "t"})
    assert out["final_response"] == ex.OWNER_ONLY
    assert [m.content for m in out["messages"]] == [ex.OWNER_ONLY]
    assert out["model_used"] == "executor_gate"
    assert out["user_query"] == QUERY and out["session_id"] == "t", "the state was not carried"
    assert spies["model"] == [], "the model was asked to extract a command for a non-owner"
    assert spies["log"] == [], "an attempt was logged for a non-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    {"user_query": QUERY, "session_id": "t"},                      # says nothing
    {"user_query": QUERY, "session_id": "t", "is_owner": None},
    {"user_query": QUERY, "session_id": "t", "is_owner": "true"},  # not the boolean
])
async def test_a_state_that_does_not_say_is_not_the_owner(spies, state):
    out = await ex.executor(state)
    assert out["final_response"] == ex.OWNER_ONLY
    assert spies["model"] == [] and spies["log"] == []


@pytest.mark.asyncio
async def test_the_owner_reaches_the_extraction(spies, monkeypatch):
    """The control: for the owner the model is asked once to extract the
    command and the attempt is logged. The run itself is recorded, not made."""
    ran: list[str] = []

    async def fake_run(cmd, timeout=30):
        ran.append(cmd)
        return ("(output)", 0)

    monkeypatch.setattr(ex, "_run_command", fake_run)
    out = await ex.executor({"user_query": QUERY, "is_owner": True, "session_id": "t"})
    assert len(spies["model"]) == 2, "one extraction call, then one summary call"
    assert spies["model"][0][-1] == {"role": "user", "content": QUERY}
    assert len(spies["log"]) == 1 and spies["log"][0][0] == "ls -la"
    assert out["final_response"] != ex.OWNER_ONLY
    assert ran == ["ls -la"]


def test_the_wording_is_the_digital_agents():
    """One refusal text for commands on this computer, wherever it is refused."""
    import inspect

    import agents.digital as digital

    source = inspect.getsource(digital)
    head, tail = ex.OWNER_ONLY.split("run commands ")
    assert f'"{head}run commands "' in source and f'"{tail}"' in source


# ── Through the chat route, as a delegated (non-owner) user ──────────────────


@pytest.mark.asyncio
async def test_a_delegated_user_gets_the_refusal_on_post_chat(stand_in, monkeypatch, spies):  # noqa: F811
    """`POST /api/v1/chat` runs the full graph against the stand-in model; the
    harness signs in as "synthetic-user", a workspace member who is not the
    admin, so `_is_owner` is false and the graph carries `is_owner=False` to
    the executor. The stand-in is never asked: the keyword router picks the
    executor and the executor refuses before its extraction call."""
    from api.middleware.workspace import require_workspace_principal
    from tests.conftest import make_workspace_principal

    app = _chat_app(monkeypatch)
    app.dependency_overrides[require_workspace_principal] = (
        lambda: make_workspace_principal(role="workspace_member", is_owner=False))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ira.test", timeout=60) as client:
        r = await client.post("/api/v1/chat", json={"message": QUERY, "session_id": f"s-{uuid.uuid4()}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["response"] == ex.OWNER_ONLY, body
    assert spies["model"] == [], "the executor asked the model on behalf of a non-owner"
    assert spies["log"] == []
    assert stand_in.requests == [], "a model was asked something on behalf of a non-owner"
