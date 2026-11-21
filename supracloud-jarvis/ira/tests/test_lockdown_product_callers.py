"""Lockdown reaches the code that acts: the chokepoint, the HTTP answer, every caller.

F65 (docs/product/FEATURE_COMPLETION_MATRIX.md): the lockdown policy was
awaited by `guard_action` alone, which nothing in the product called, so an
engaged lockdown refused nothing a person could trigger. The fix puts the check
in `actions/gated_execution` (stage AND confirm) and in front of every
`owner_gated_action` draft.

This file runs in the main unit job, with no Redis or PostgreSQL, so the state
is driven by replacing `get_lockdown_state`. The same properties are proved
against real Redis and PostgreSQL, through the real app, in
`tests/tenancy/test_lockdown_refuses_action_routes.py`. What only this file
does is the ratchet at the bottom: a NEW route that drafts an owner-gated
action, or stages a gated one, without the lockdown check fails here.
"""
from __future__ import annotations

import ast
import os
import pathlib

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

import actions.gated_execution as ge
from actions.shadow_world import ActionSpec
from utils.approval import ApprovalGuardrail
from utils.security_tools import LockdownState

IRA = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def state(monkeypatch):
    def _set(value):
        async def _get():
            return value
        monkeypatch.setattr("utils.security_tools.get_lockdown_state", _get)
    return _set


@pytest.fixture(autouse=True)
def no_ledger(monkeypatch):
    """Record ledger writes instead of reaching PostgreSQL."""
    written: list[str] = []

    async def _append(kind, **_kw):
        written.append(kind)
        return {"seq": len(written)}

    async def _records(_action_id):
        return []

    monkeypatch.setattr("trust.ledger.append_record", _append)
    monkeypatch.setattr("trust.ledger.records_for", _records)
    return written


def _spec(risk: str = "medium") -> ActionSpec:
    return ActionSpec(kind="email_send", risk=risk, requested_by="owner",
                      intended_action="Send email to probe@example.test",
                      objects_affected=["email:probe@example.test"],
                      action_id=f"lockdown-probe-{risk}")


class _Probe:
    """An action and its simulator that count what ran."""

    def __init__(self):
        self.simulated = 0
        self.executed = 0

    async def simulate(self, _spec):
        self.simulated += 1
        return {"ok": True, "detail": "dry run"}

    def execute(self):
        self.executed += 1
        return {"status": "sent"}


# ── The chokepoint: actions/gated_execution ─────────────────────────────────

async def test_active_lockdown_refuses_at_stage_before_simulation_or_token(state):
    state(LockdownState.ACTIVE)
    probe, guard = _Probe(), ApprovalGuardrail()

    out = await ge.stage_action(_spec(), is_owner=True, execute=probe.execute,
                                simulator=probe.simulate, guard=guard)

    assert out["status"] == "refused" and out["refused_by"] == "lockdown", out
    assert out["lockdown_state"] == "active" and out["action_id"] == "lockdown-probe-medium"
    assert "token" not in out and "proof_card" not in out
    assert probe.simulated == 0, "the action was simulated during the lockdown"
    assert guard.pending_count() == 0, "a confirmation token was minted during the lockdown"


async def test_active_lockdown_refuses_a_confirm_staged_before_it(state, no_ledger):
    state(LockdownState.INACTIVE)
    probe, guard = _Probe(), ApprovalGuardrail()
    staged = await ge.stage_action(_spec(), is_owner=True, execute=probe.execute,
                                   simulator=probe.simulate, guard=guard)
    assert staged["status"] == "confirmation_required", staged

    state(LockdownState.ACTIVE)
    out = await ge.confirm_action(_spec(), token=staged["token"],
                                  approval_artifact="plan_approval",
                                  simulator=probe.simulate, guard=guard)

    assert out.get("refused_by") == "lockdown", f"a pre-lockdown token spent: {out}"
    assert probe.executed == 0, "the side effect ran during the lockdown"
    assert no_ledger == [], f"a refused action wrote to the ledger: {no_ledger}"


async def test_the_owner_gate_answers_a_non_owner_before_lockdown_does(state):
    """A person who may not act at all is not told the owner's security state."""
    state(LockdownState.ACTIVE)
    probe = _Probe()

    out = await ge.stage_action(_spec(), is_owner=False, execute=probe.execute,
                                simulator=probe.simulate, guard=ApprovalGuardrail())

    assert out["status"] == "forbidden", out


@pytest.mark.parametrize("risk, refused", [
    ("low", False), ("medium", False), ("high", True), ("critical", True)])
async def test_unknown_refuses_high_and_critical_at_stage(state, risk, refused):
    state(LockdownState.UNKNOWN)
    probe = _Probe()

    out = await ge.stage_action(_spec(risk), is_owner=True, execute=probe.execute,
                                simulator=probe.simulate, guard=ApprovalGuardrail())

    if refused:
        assert out.get("refused_by") == "lockdown" and out["lockdown_state"] == "unknown", out
    else:
        assert out.get("refused_by") is None, f"UNKNOWN refused a {risk} action: {out}"


async def test_inactive_stages_and_confirms_as_before(state, no_ledger):
    """The control: with lockdown off the chokepoint is unchanged."""
    state(LockdownState.INACTIVE)
    probe, guard = _Probe(), ApprovalGuardrail()

    staged = await ge.stage_action(_spec(), is_owner=True, execute=probe.execute,
                                   simulator=probe.simulate, guard=guard)
    done = await ge.confirm_action(_spec(), token=staged["token"],
                                   approval_artifact="plan_approval",
                                   simulator=probe.simulate, guard=guard)

    assert done["status"] == "executed" and probe.executed == 1, done


# ── The HTTP answer ─────────────────────────────────────────────────────────

async def test_the_http_answer_is_423_with_the_state_in_a_header(state):
    from fastapi import HTTPException

    from api.routes._lockdown import (LOCKDOWN_HEADER, raise_if_lockdown_refused,
                                      refuse_during_lockdown)

    state(LockdownState.ACTIVE)
    with pytest.raises(HTTPException) as caught:
        await refuse_during_lockdown(risk="low", action_type="probe", is_owner=True)
    assert caught.value.status_code == 423
    assert caught.value.headers == {LOCKDOWN_HEADER: "active"}
    assert "Lockdown is on" in caught.value.detail

    # A non-owner is refused by the owner gate that follows, not told the state.
    await refuse_during_lockdown(risk="low", action_type="probe", is_owner=False)

    refusal = await ge.stage_action(_spec(), is_owner=True, execute=_Probe().execute,
                                    simulator=_Probe().simulate, guard=ApprovalGuardrail())
    with pytest.raises(HTTPException) as mapped:
        raise_if_lockdown_refused(refusal)
    assert mapped.value.status_code == 423

    state(LockdownState.UNKNOWN)
    with pytest.raises(HTTPException) as unknown:
        await refuse_during_lockdown(risk="high", action_type="probe", is_owner=True)
    assert unknown.value.headers == {LOCKDOWN_HEADER: "unknown"}
    assert "cannot tell whether lockdown is on" in unknown.value.detail
    await refuse_during_lockdown(risk="medium", action_type="probe", is_owner=True)


# ── The ratchet: every product caller is covered ────────────────────────────

#: Owner-gated actions that are deliberately NOT refused during lockdown, with
#: the reason. Adding one is a decision about the lockdown, made in this diff.
NOT_REFUSED = {
    ("api/routes/access.py", "revoke_access"):
        "revoking a delegated account is how the owner contains a compromised one",
}

#: What counts as a lockdown check in a function that drafts or acts.
CHECKS = {"refuse_during_lockdown", "lockdown_refusal", "raise_if_lockdown_refused"}


def _product_functions():
    for path in IRA.rglob("*.py"):
        rel = str(path.relative_to(IRA))
        if "tests" in path.parts or ".venv" in path.parts or rel.startswith("scripts/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield rel, fn


def _calls(fn) -> list[tuple[str, int]]:
    """(name, line) of every call made by `fn` itself, not by a function
    defined inside it: the check has to sit in the function that acts."""
    out = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                out.append((name, child.lineno))
            visit(child)

    visit(fn)
    return out


def test_every_owner_gated_draft_checks_lockdown_first():
    """A route that drafts through `owner_gated_action` must check lockdown
    before it, in the same function, or be listed in NOT_REFUSED with a reason."""
    missing, seen = [], set()
    candidates = [(rel, fn) for rel, fn in _product_functions()
                  if rel != "utils/approval.py"
                  and any(name == "owner_gated_action" for name, _ in _calls(fn))]
    checked_but_exempt = []
    for rel, fn in candidates:
        seen.add((rel, fn.name))
        calls = _calls(fn)
        if (rel, fn.name) in NOT_REFUSED:
            # An exemption is a decision that the path stays open during a
            # lockdown; a check added to it closes the path without anyone
            # revisiting that decision.
            if any(name in CHECKS for name, _ in calls):
                checked_but_exempt.append(f"{rel}:{fn.name}")
            continue
        gate = min(line for name, line in calls if name == "owner_gated_action")
        if not any(name in CHECKS and line < gate for name, line in calls):
            missing.append(f"{rel}:{fn.name}")
    assert not missing, (
        "these draft an owner-gated action without checking lockdown first: "
        f"{missing}. Add `await refuse_during_lockdown(...)` (api/routes/_lockdown.py) "
        "before the draft, or list the function in NOT_REFUSED with the reason.")
    assert not checked_but_exempt, (
        f"these are listed in NOT_REFUSED but now check lockdown: {checked_but_exempt}. "
        f"Either the reason given there no longer holds (remove the entry) or the "
        f"check closes a path that must stay open during a lockdown.")
    stale = set(NOT_REFUSED) - seen
    assert not stale, f"NOT_REFUSED names functions that no longer draft anything: {stale}"
    # IRA CARE keeps 8 owner-gated drafts (access x3, backup, memory, people...).
    # The floor is the measured count, so a scanner that silently finds fewer fails.
    assert len(seen) >= 8, f"the scan found too few owner-gated drafts to trust: {seen}"


def test_every_route_that_stages_a_gated_action_answers_its_lockdown_refusal():
    """`stage_action` and `confirm_action` return a lockdown refusal as a dict.
    A route that does not map it would answer 200 to an action that did not run."""
    staging = {"stage_action", "confirm_action", "stage_schedule", "confirm_schedule"}
    missing, seen = [], []
    candidates = [(rel, fn) for rel, fn in _product_functions()
                  if rel.startswith("api/routes/")
                  and any(name in staging for name, _ in _calls(fn))]
    for rel, fn in candidates:
        seen.append(f"{rel}:{fn.name}")
        calls = _calls(fn)
        stages = sorted(line for name, line in calls if name in staging)
        mapped = [line for name, line in calls if name == "raise_if_lockdown_refused"]
        # Each stage or confirm needs its OWN mapping, before the next one: a
        # route that maps the confirm and not the stage answers the stage's
        # refusal with 200.
        for line, following in zip(stages, stages[1:] + [float("inf")]):
            if not any(line < m < following for m in mapped):
                missing.append(f"{rel}:{fn.name} (line {line})")
    assert not missing, (
        f"these routes stage or confirm a gated action and do not answer a lockdown "
        f"refusal with 423: {missing}. Call `raise_if_lockdown_refused(outcome)` after it.")
    # IRA CARE currently has no route that stages or confirms a gated action (the
    # routes that did were not carried over). Raise this floor when one is added.
    assert len(seen) >= 0, f"the scan found too few gated routes to trust: {seen}"


def test_the_architect_apply_checks_lockdown_wherever_it_is_called():
    """`apply_implementation` rewrites IRA's own source, commits and restarts
    services; both of its callers must check lockdown before calling it."""
    callers = [(rel, fn) for rel, fn in _product_functions()
               if rel != "utils/auto_implement.py"
               and any(n == "apply_implementation" for n, _ in _calls(fn))]
    assert callers, "nothing calls apply_implementation any more; update this test"
    for rel, fn in callers:
        calls = _calls(fn)
        first = min(line for name, line in calls if name == "apply_implementation")
        assert any(name in CHECKS and line < first for name, line in calls), (
            f"{rel}:{fn.name} applies an implementation without checking lockdown")
