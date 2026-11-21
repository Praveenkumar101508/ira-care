"""P14-R1 — FINDING AB closed: lockdown refuses governed actions.

`test_p1401_lockdown_state.py` asserted the emptiness: `get_lockdown_state()`
was called by nothing in product code. `p15_acceptance.py` asserted it too,
every run, so that closing the finding would fail the documents still calling
it open.

These tests assert the opposite property, and they are written so that they
FAIL if the enforcement point is removed or bypassed — which is the only way a
closed finding stays closed.

The important one is `test_a_staged_action_is_refused_too`. Refusing only at
confirm would let a lockdown-time action stage, mint a token and produce a
Proof Card, leaving it ready to confirm the moment lockdown lifts. That is the
opposite of containment, and it is the shape a partial fix would take.
"""
from __future__ import annotations

import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

from trust import lockdown_enforcement
from trust.lockdown_enforcement import REFUSED_WHEN_UNKNOWN, lockdown_refusal
from utils.security_tools import LockdownState


@pytest.fixture
def state(monkeypatch):
    """Drive `get_lockdown_state` without a Redis or a database."""
    def _set(value):
        async def _get():
            return value
        monkeypatch.setattr("utils.security_tools.get_lockdown_state", _get)
    return _set


# ── The three states ────────────────────────────────────────────────────────

@pytest.mark.parametrize("risk", ["low", "medium", "high", "critical"])
async def test_active_refuses_every_risk_level(state, risk):
    """Lockdown means lockdown. No tier is exempt."""
    state(LockdownState.ACTIVE)

    refusal = await lockdown_refusal(
        risk=risk, action_type="file.write", is_owner=False)

    assert refusal is not None, (
        f"a {risk} action was permitted while lockdown was ACTIVE. That is "
        f"FINDING AB again: the state is set and nothing enforces it.")
    assert refusal["refused_by"] == "lockdown"
    assert refusal["lockdown_state"] == "active"
    assert refusal["retryable"] is False


async def test_active_refuses_the_owner_too(state):
    """No per-action owner bypass.

    An owner who wants to act LIFTS the lockdown, which is audited and writes
    to the ledger. A per-action exemption is not, and it would make the control
    depend on who is asking rather than on what state the system is in.
    """
    state(LockdownState.ACTIVE)

    refusal = await lockdown_refusal(
        risk="high", action_type="command.run", is_owner=True)

    assert refusal is not None
    assert "lift it" in refusal["reason"]


def test_the_refused_set_is_exactly_high_and_critical() -> None:
    """Pinned as a LITERAL, and that is the point of the test.

    The obvious way to write the test below is
    `@parametrize("risk", sorted(REFUSED_WHEN_UNKNOWN))`. It was written that
    way first, and the self-test's fail-open plant — emptying the frozenset —
    did not turn it red. It made pytest COLLECT NOTHING. The suite stayed
    green because the test had ceased to exist, which is the "a replacement
    matched nothing" failure this programme keeps finding in other people's
    checks.

    So the risk levels below are spelled out, and the constant is pinned here
    instead. Narrowing it fails; widening it fails and asks for the judgement
    in `test_unknown_permits_low_risk_actions` to be revisited with it.
    """
    assert set(REFUSED_WHEN_UNKNOWN) == {"high", "critical"}, (
        f"REFUSED_WHEN_UNKNOWN is {sorted(REFUSED_WHEN_UNKNOWN)}. Narrowing it "
        f"lets UNKNOWN read as 'not locked down' for actions the Phase 14 "
        f"instruction calls security-sensitive; widening it moves the outage "
        f"trade-off, which is a judgement that belongs in a diff.")


@pytest.mark.parametrize("risk", ["high", "critical"])
async def test_unknown_refuses_security_sensitive_actions(state, risk):
    """The owner's instruction, as an executable property.

    "Security-sensitive operations should not silently interpret UNKNOWN as
    'not locked down.'"
    """
    state(LockdownState.UNKNOWN)

    refusal = await lockdown_refusal(
        risk=risk, action_type="command.run", is_owner=False)

    assert refusal is not None, (
        f"a {risk} action proceeded while the lockdown state was UNKNOWN. "
        f"UNKNOWN is not 'not locked down'.")
    assert refusal["lockdown_state"] == "unknown"


@pytest.mark.parametrize("risk", ["low", "medium"])
async def test_unknown_permits_low_risk_actions(state, risk):
    """The other half of the judgement, asserted so it cannot drift silently.

    Failing closed on EVERYTHING would turn a datastore outage into a total
    product outage, and a control that takes the product down when its own
    dependencies wobble gets disabled. This is the deliberate limit of the
    tiering — if somebody widens it, this test says so.
    """
    state(LockdownState.UNKNOWN)

    assert await lockdown_refusal(
        risk=risk, action_type="file.read", is_owner=False) is None


@pytest.mark.parametrize("risk", ["low", "medium", "high", "critical"])
async def test_inactive_permits_everything(state, risk):
    """The negative control.

    Without this, a `lockdown_refusal` that returned a refusal unconditionally
    would pass every test above while blocking the entire product.
    """
    state(LockdownState.INACTIVE)

    assert await lockdown_refusal(
        risk=risk, action_type="file.write", is_owner=False) is None


# ── The check itself failing ───────────────────────────────────────────────

async def test_an_exception_is_treated_as_unknown_not_as_permission(monkeypatch):
    """A broken check is at least as uncertain as an unreachable store."""
    async def _boom():
        raise RuntimeError("simulated: the state store exploded")
    monkeypatch.setattr("utils.security_tools.get_lockdown_state", _boom)

    refusal = await lockdown_refusal(
        risk="high", action_type="command.run", is_owner=False)

    assert refusal is not None, (
        "the lockdown check raised and the action proceeded. A failure to "
        "evaluate the policy became a permit, which is Phase 13's whole "
        "subject.")
    assert refusal["lockdown_state"] == "unknown"

    # …and it still must not take the product down for low-risk work.
    assert await lockdown_refusal(
        risk="low", action_type="file.read", is_owner=False) is None


# ── The enforcement point is actually wired ────────────────────────────────

async def test_a_staged_action_is_refused_too(state, monkeypatch):
    """THE PLANT THIS FILE EXISTS FOR.

    A partial fix refuses at confirm and not at stage. The action then stages,
    mints a token and produces a Proof Card, and waits to be confirmed the
    moment lockdown lifts — containment that contains nothing.

    So this drives the real `guard_action` with NO confirm token, which is the
    staging branch, and requires the refusal there.

    It also drives the REAL `ActionDescriptor` rather than a stand-in, and that
    is not incidental. The first version of the wiring read
    `descriptor.action_type`; the field is `type`. Every guarded action would
    have raised `AttributeError` at the enforcement point — an enforcement
    consumer that exists in the diff and crashes in the product. The unit tests
    above all passed, because they call `lockdown_refusal` directly. Only
    driving the real pipeline found it.
    """
    state(LockdownState.ACTIVE)

    from trust.action_guard import ActionDescriptor, guard_action

    staged = []
    async def _never(*args, **kwargs):
        staged.append(kwargs)
        return {"status": "staged"}
    monkeypatch.setattr("actions.gated_execution.stage_action", _never)
    monkeypatch.setattr("actions.gated_execution.confirm_action", _never)

    outcome = await guard_action(
        ActionDescriptor(type="shell_command", target="ls -la",
                         requested_by="p14r1-test"),
        is_owner=True)

    assert outcome.get("refused_by") == "lockdown", (
        f"guard_action did not refuse while lockdown was ACTIVE: {outcome}")
    assert outcome["action_type"] == "shell_command", (
        f"the refusal did not name the action it refused: {outcome}")
    assert not staged, (
        "the action reached the staging pipeline despite the refusal. A "
        "lockdown that stages and then refuses has minted a token for later.")


async def test_the_confirm_branch_is_refused_too(state, monkeypatch):
    """The other half. A token minted before lockdown must not still spend.

    Staging and confirming are separate calls, and a token issued minutes
    before the lockdown is a live capability sitting in a client. If the guard
    refused only at stage, that token would still execute.
    """
    state(LockdownState.ACTIVE)

    from trust.action_guard import ActionDescriptor, guard_action

    executed = []
    async def _never(*args, **kwargs):
        executed.append(kwargs)
        return {"status": "executed"}
    monkeypatch.setattr("actions.gated_execution.stage_action", _never)
    monkeypatch.setattr("actions.gated_execution.confirm_action", _never)

    outcome = await guard_action(
        ActionDescriptor(type="shell_command", target="ls -la",
                         requested_by="p14r1-test"),
        is_owner=True, confirm_token="a-token-minted-before-the-lockdown")

    assert outcome.get("refused_by") == "lockdown", (
        f"a pre-lockdown token was still spendable during lockdown: {outcome}")
    assert not executed


def test_the_enforcement_point_is_reachable_from_the_guard() -> None:
    """The wiring itself, as a fact about the module rather than a mock.

    `test_p1401_lockdown_state.py` proved FINDING AB by finding NO caller. This
    proves the caller exists, in product code, at the pipeline every privileged
    operation passes through — so the two files disagree the moment somebody
    unwires it.

    It looks for the CALL and not merely the name. Deleting the enforcement
    block while leaving `from trust.lockdown_enforcement import lockdown_refusal`
    behind is exactly what a careless revert does, and an import is not an
    enforcement point. That was not hypothetical: the plant for this batch did
    precisely that, and the first version of this assertion stayed green while
    the two behavioural tests above went red.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1]
              / "trust" / "action_guard.py").read_text(encoding="utf-8")
    assert "await lockdown_refusal(" in source, (
        "trust/action_guard.py no longer AWAITS lockdown_refusal. FINDING AB "
        "is reopened: the state is set by initiate_lockdown() and consulted by "
        "nothing. (An import alone does not count — that is what a half-"
        "finished revert leaves behind.)")
    assert lockdown_enforcement.lockdown_refusal is not None
