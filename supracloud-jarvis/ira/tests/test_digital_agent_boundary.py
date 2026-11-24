"""The digital agent's host tools are the owner's, and its browsing obeys the egress switch.

`agents/digital.py` runs three tools straight from the chat message:
`run_terminal_command` (an allowlist of read-only commands: `git log`,
`docker logs`, `netstat`, `cat` inside the IRA folder and more),
`open_application`, and `browse_and_summarize_website`. The attack-style pass of
21 October 2026 found two ways past their limits:

  1. **A household account runs host commands.** The owner gate
     (`security/owner_gate.py`) refuses "run/execute ... command|git|docker" to
     anyone but the owner. The digital agent also runs a command for
     "check ..." and "show me ...", which the gate lets through, and any message
     with a URL in it is routed to the digital agent. So
     "https://a check docker logs ira-api" from a household account reaches
     `run_terminal_command`. The agent never asks who is asking.
  2. **Browsing ignores the master switch.** `utils/external_gate.require_browsing`
     guards the browser agent's navigation, and its docstring calls that "the
     single navigation chokepoint". `browse_and_summarize_website` launches its
     own Chromium and never passes through it, so a chat message with a URL
     fetches the page with `IRA_ALLOW_EXTERNAL_API=false`.

Each attack test fails on `main` at 310e8c6; that run is its negative control.
The tests marked "the control" pass on both.

No tool runs for real: each is replaced by a recorder, as is the LLM.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

#: Passes the owner gate for anyone, is routed to the digital agent because it
#: holds a URL, and names an allowlisted command.
HOUSEHOLD_ASKS = "https://a check docker logs ira-api"


@pytest.fixture
def tools(monkeypatch):
    """Recorders in place of the three host tools and the LLM."""
    calls: list[tuple] = []

    async def _cmd(command):
        calls.append(("run_terminal_command", command))
        return {"stdout": "recorded"}

    async def _app(name):
        calls.append(("open_application", name))
        return {"launched": name}

    async def _browse(url, query):
        calls.append(("browse_and_summarize_website", url))
        return {"error": "recorded"}

    async def _llm(messages, **_kw):
        return "ok"

    monkeypatch.setattr("utils.os_tools.run_terminal_command", _cmd)
    monkeypatch.setattr("utils.os_tools.open_application", _app)
    monkeypatch.setattr("utils.browser_tools.browse_and_summarize_website", _browse)
    monkeypatch.setattr("agents.digital.chat_complete", _llm)
    return calls


def _state(query: str, *, owner: bool) -> dict:
    return {"user_query": query, "messages": [], "mode": None, "is_owner": owner,
            "memory_context": ""}


# ── 1. Host commands are the owner's ─────────────────────────────────────────


async def test_the_message_reaches_the_digital_agent_for_anyone():
    """The path exists: the owner gate lets the message through for a
    non-owner, and the router sends it to the digital agent."""
    from agents.supervisor import classify
    from security.owner_gate import evaluate

    assert evaluate(HOUSEHOLD_ASKS, False).allowed
    routed = await classify(_state(HOUSEHOLD_ASKS, owner=False))
    assert routed["active_agent"] == "digital"


async def test_the_owner_can_run_an_allowlisted_command(tools):
    """The control: the owner's request reaches the tool."""
    from agents.digital import digital_agent

    await digital_agent(_state(HOUSEHOLD_ASKS, owner=True))
    assert ("run_terminal_command", "docker logs ira-api") in tools


async def test_a_household_account_cannot_run_a_command(tools):
    from agents.digital import digital_agent

    await digital_agent(_state(HOUSEHOLD_ASKS, owner=False))
    ran = [c for c in tools if c[0] == "run_terminal_command"]
    assert not ran, f"a non-owner ran a host command: {ran}"


async def test_a_household_account_cannot_open_an_application(tools):
    from agents.digital import digital_agent

    await digital_agent(_state("https://a open the terminal", owner=False))
    opened = [c for c in tools if c[0] == "open_application"]
    assert not opened, f"a non-owner opened an application: {opened}"


async def test_a_state_without_the_flag_is_not_the_owner(tools):
    """Fail closed: a state that does not say who is asking runs nothing."""
    from agents.digital import digital_agent

    state = _state(HOUSEHOLD_ASKS, owner=True)
    del state["is_owner"]
    await digital_agent(state)
    assert not [c for c in tools if c[0] == "run_terminal_command"]


# ── 2. Browsing obeys the master switch ──────────────────────────────────────


@pytest.fixture
def chromium(monkeypatch):
    """A stand-in for Playwright that records a launch, and a URL check that
    passes without a DNS lookup."""
    launched: list[str] = []

    class _Refused(RuntimeError):
        pass

    class _Ctx:
        async def __aenter__(self):
            launched.append("chromium")
            raise _Refused("stand-in: no real browser")

        async def __aexit__(self, *_a):
            return False

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: _Ctx()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr("utils.browser_tools.check_url", lambda _u: (True, ""))
    return launched


async def test_browsing_launches_nothing_even_when_both_switches_are_on(chromium, monkeypatch):
    """D-056: the product never browses for the person. The old master switch and the
    browsing capability together no longer launch a browser; the answer says why."""
    from config import get_settings
    from utils.browser_tools import browse_and_summarize_website

    monkeypatch.setenv("IRA_ALLOW_EXTERNAL_API", "true")
    monkeypatch.setattr(get_settings(), "browser_agent_enabled", True, raising=False)
    result = await browse_and_summarize_website("https://example.com", "what is here")
    assert chromium == []
    assert result.get("error")


@pytest.mark.parametrize("egress, capability", [("false", True), ("true", False)],
                         ids=["master-switch-off", "browsing-off"])
async def test_browsing_launches_nothing_when_a_switch_is_off(chromium, monkeypatch, egress, capability):
    from config import get_settings
    from utils.browser_tools import browse_and_summarize_website

    monkeypatch.setenv("IRA_ALLOW_EXTERNAL_API", egress)
    monkeypatch.setattr(get_settings(), "browser_agent_enabled", capability, raising=False)
    result = await browse_and_summarize_website("https://example.com", "what is here")
    assert not chromium, "a page was fetched with browsing switched off"
    assert "IRA_ALLOW_EXTERNAL_API" in result.get("error", ""), result
