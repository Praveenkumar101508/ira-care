"""Integrated action gate (PR 73 wiring) — actions/gated_execution.py.

DB-free: the trust ledger append is monkeypatched (or made to fail, to prove
that since P1-09 a ledger outage refuses the action). A fresh ApprovalGuardrail
per test isolates tokens.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio

import pytest

import actions.gated_execution as ge
from actions.shadow_world import ActionSpec, register_simulator
from utils.approval import ApprovalGuardrail

# Not a lockdown suite: the lockdown state is pinned INACTIVE here, because
# with no Redis or PostgreSQL it reads UNKNOWN and refuses high-risk actions.
# See `lockdown_inactive` in tests/conftest.py; the policy has its own suites.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")


@pytest.fixture(autouse=True)
def ledger_capture(monkeypatch):
    """Capture ledger appends instead of hitting Postgres."""
    recorded = []
    seq = {"n": 0}

    async def fake_append(kind, *, user_id, action_id, task_id=None, record=None):
        seq["n"] += 1
        entry = {"seq": seq["n"], "kind": kind, "user_id": user_id,
                 "action_id": action_id, "record": record or {}}
        recorded.append(entry)
        return entry

    monkeypatch.setattr("trust.ledger.append_record", fake_append)
    return recorded


@pytest.fixture(autouse=True)
def _sim(monkeypatch):
    async def ok_sim(spec):
        return {"ok": True, "detail": "dry-run ok"}
    # register for every kind used in tests
    for kind in ("file_change", "email_send", "db_operation"):
        register_simulator(kind, ok_sim)


def _spec(risk="medium", **kw):
    d = dict(kind="file_change", intended_action="rewrite notes",
             requested_by="owner", risk=risk)
    d.update(kw)
    return ActionSpec(**d)


def _guard():
    return ApprovalGuardrail(ttl_seconds=300, now=lambda: _guard.t)
_guard.t = 1000.0


async def _run_full(spec, ledger, *, approval=None, execute=None, is_owner=True):
    g = _guard()
    ran = {"count": 0}

    def do():
        ran["count"] += 1
        return {"sent": True}

    staged = await ge.stage_action(spec, is_owner=is_owner,
                                   execute=execute or do, guard=g)
    if staged["status"] != "confirmation_required":
        return staged, ran
    confirmed = await ge.confirm_action(spec, token=staged["token"],
                                        approval_artifact=approval, guard=g)
    return confirmed, ran


# ── Owner gate ────────────────────────────────────────────────────────────────

def test_non_owner_forbidden_before_simulation(ledger_capture):
    out, ran = asyncio.run(_run_full(_spec(), ledger_capture, is_owner=False))
    assert out["status"] == "forbidden"
    assert ran["count"] == 0 and ledger_capture == []


# ── Critical / failed sim blocked at stage ───────────────────────────────────

def test_critical_blocked_at_stage_no_token(ledger_capture):
    g = _guard()
    staged = asyncio.run(ge.stage_action(_spec(risk="critical"),
                                         is_owner=True, execute=lambda: 1, guard=g))
    assert staged["status"] == "blocked" and "critical" in staged["reason"]
    assert "token" not in staged


def test_failed_simulation_blocks_at_stage(monkeypatch, ledger_capture):
    async def bad_sim(spec):
        return {"ok": False, "detail": "would clobber prod"}
    monkeypatch.setattr("actions.shadow_world._SIMULATORS",
                        {"file_change": bad_sim})
    g = _guard()
    staged = asyncio.run(ge.stage_action(_spec(), is_owner=True,
                                         execute=lambda: 1, guard=g))
    assert staged["status"] == "blocked" and "would clobber prod" in staged["reason"]


# ── Happy path records the ledger ────────────────────────────────────────────

def test_low_risk_executes_and_records(ledger_capture):
    out, ran = asyncio.run(_run_full(_spec(risk="low"), ledger_capture))
    assert out["status"] == "executed" and ran["count"] == 1
    assert out["result"] == {"sent": True}
    kinds = [r["kind"] for r in ledger_capture]
    assert kinds == ["action", "approval", "execution"]
    assert out["ledger_recorded"] is True
    assert out["ledger_seqs"] == [1, 3]  # action + execution seqs


def test_high_risk_requires_approval_artifact(ledger_capture):
    # confirm without approval → blocked, side effect never runs
    out, ran = asyncio.run(_run_full(
        _spec(risk="high", rollback_procedure="restore", rollback_validated=True),
        ledger_capture))
    assert out["status"] == "blocked" and "owner_password" in out["reason"]
    assert ran["count"] == 0
    # a security_event was recorded for the blocked attempt
    assert any(r["kind"] == "security_event" for r in ledger_capture)


def test_high_risk_runs_with_approval(ledger_capture):
    out, ran = asyncio.run(_run_full(
        _spec(risk="high", rollback_procedure="restore", rollback_validated=True),
        ledger_capture, approval="owner_password"))
    assert out["status"] == "executed" and ran["count"] == 1


# ── Ledger down: refused, nothing runs (P1-09) ───────────────────────────────
#
# Until 1 November 2026 this asserted the opposite: a ledger outage did not
# stop the action. The owner approved failing closed (P1-09). The token-reuse,
# journal and replay behaviour is in `test_ledger_fail_closed.py`.

def test_ledger_failure_refuses_and_runs_nothing(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("postgres down")
    monkeypatch.setattr("trust.ledger.append_record", broken)
    out, ran = asyncio.run(_run_full(_spec(risk="low"), []))
    assert out["status"] == "ledger_unavailable" and ran["count"] == 0  # nothing ran
    assert out["ledger_recorded"] is False and out["confirmation_spent"] is False


# ── Intent recorded before the side effect ───────────────────────────────────

def test_intent_recorded_before_side_effect(ledger_capture):
    order = []

    def do():
        order.append("side_effect")
        return "done"

    async def tracking_append(kind, **kw):
        order.append(f"ledger:{kind}")
        return {"seq": len(order)}

    import trust.ledger
    orig = trust.ledger.append_record
    trust.ledger.append_record = tracking_append
    try:
        asyncio.run(_run_full(_spec(risk="low"), ledger_capture, execute=do))
    finally:
        trust.ledger.append_record = orig
    assert order.index("ledger:action") < order.index("side_effect")
    assert order.index("side_effect") < order.index("ledger:execution")
