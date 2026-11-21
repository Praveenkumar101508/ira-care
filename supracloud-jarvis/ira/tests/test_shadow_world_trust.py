"""Shadow World + Trust Ledger + Proof Cards (PR 73).

Pins the load-bearing guarantees:
  - approval level derives from risk and cannot be weakened
  - unsimulated medium/high actions cannot pass the execute gate
  - critical actions are blocked outright
  - file snapshots restore byte-identically and refuse corrupted snapshots
  - the ledger chain detects edits, deletions, and anchor mismatches
  - proof cards trace every field to spec/ledger data
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
from pathlib import Path

import pytest

import actions.shadow_world as sw
from actions.shadow_world import ActionSpec, FileSnapshot, execute_gate, simulate
from trust.ledger import GENESIS, compute_hash, verify_chain
from trust.proof_card import build_proof_card


def _spec(risk="medium", **kw):
    defaults = dict(kind="file_change", intended_action="rewrite config",
                    requested_by="owner", risk=risk)
    defaults.update(kw)
    return ActionSpec(**defaults)


# ── ActionSpec / approval derivation ─────────────────────────────────────────

def test_spec_validates_inputs():
    with pytest.raises(ValueError):
        ActionSpec(kind="tickle", intended_action="x", requested_by="owner")
    with pytest.raises(ValueError):
        _spec(risk="apocalyptic")
    with pytest.raises(ValueError):
        ActionSpec(kind="file_change", intended_action="  ", requested_by="owner")


def test_approval_level_is_derived_not_chosen():
    assert _spec(risk="low").approval_level == "auto"
    assert _spec(risk="medium").approval_level == "plan_approval"
    assert _spec(risk="high").approval_level == "owner_password"
    assert _spec(risk="critical").approval_level == "blocked"
    # there is no constructor parameter to weaken it
    assert "approval_level" not in ActionSpec.__dataclass_fields__


def test_execute_gate_blocks_unsimulated_medium(monkeypatch):
    ok, reason = execute_gate(_spec(risk="medium"), approval="plan_approval")
    assert not ok and "simulation" in reason


def test_execute_gate_blocks_critical_always():
    spec = _spec(risk="critical", simulation={"ok": True},
                 rollback_procedure="r", rollback_validated=True)
    ok, reason = execute_gate(spec, approval="owner_password")
    assert not ok and "blocked" in reason


def test_execute_gate_requires_validated_rollback_for_high():
    spec = _spec(risk="high", simulation={"ok": True},
                 rollback_procedure="restore snapshot")
    ok, reason = execute_gate(spec, approval="owner_password")
    assert not ok and "VALIDATED" in reason
    spec.rollback_validated = True
    ok, reason = execute_gate(spec, approval="owner_password")
    assert ok


def test_execute_gate_low_risk_auto():
    ok, reason = execute_gate(_spec(risk="low"))
    assert ok and "auto" in reason


def test_failed_simulation_blocks():
    spec = _spec(risk="medium", simulation={"ok": False, "detail": "would clobber prod"})
    ok, reason = execute_gate(spec, approval="plan_approval")
    assert not ok and "would clobber prod" in reason


# ── simulate() ────────────────────────────────────────────────────────────────

def test_simulate_without_registered_simulator_is_honest_failure():
    spec = _spec(kind="cloud_action", risk="medium")
    result = asyncio.run(simulate(spec))
    assert result["ok"] is False and "no simulator" in result["detail"]
    assert not execute_gate(spec, approval="plan_approval")[0]


def test_simulate_uses_registered_simulator_and_crash_is_failure():
    async def good(spec):
        return {"ok": True, "detail": "dry-run fine"}
    async def crashy(spec):
        raise RuntimeError("sim exploded")
    sw.register_simulator("db_operation", good)
    spec = _spec(kind="db_operation")
    assert asyncio.run(simulate(spec))["ok"] is True
    sw.register_simulator("db_operation", crashy)
    result = asyncio.run(simulate(_spec(kind="db_operation")))
    assert result["ok"] is False and "sim exploded" in result["detail"]
    _SIM = sw._SIMULATORS.pop("db_operation", None)  # cleanup registry


# ── FileSnapshot ─────────────────────────────────────────────────────────────

def test_snapshot_restore_roundtrip(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("original: true\n", encoding="utf-8")
    snap = FileSnapshot.create(f, tmp_path / "snaps")
    f.write_text("clobbered: true\n", encoding="utf-8")
    snap.restore()
    assert f.read_text(encoding="utf-8") == "original: true\n"


def test_snapshot_refuses_corrupted_source(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data", encoding="utf-8")
    snap = FileSnapshot.create(f, tmp_path / "snaps")
    snap.snapshot.write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupted"):
        snap.restore()


def test_snapshot_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        FileSnapshot.create(tmp_path / "ghost.txt", tmp_path / "snaps")


# ── Ledger chain (pure) ──────────────────────────────────────────────────────

def _chain(n=3):
    records, prev = [], GENESIS
    for i in range(1, n + 1):
        body = {"seq": i, "user_id": "owner", "action_id": "a1",
                "task_id": None, "kind": "action", "record": {"n": i},
                "prev_hash": prev}
        h = compute_hash(prev, body)
        records.append({**body, "hash": h})
        prev = h
    return records


def test_verify_chain_roundtrip_and_anchor():
    records = _chain(4)
    assert verify_chain(records) == 4
    assert verify_chain(records, expected_head=records[-1]["hash"]) == 4
    with pytest.raises(ValueError, match="anchor"):
        verify_chain(records, expected_head="f" * 64)


def test_verify_chain_detects_edit_and_deletion():
    records = _chain(4)
    records[1]["record"] = {"n": 999}
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_chain(records)
    records = _chain(4)
    del records[1]
    with pytest.raises(ValueError, match="broken prev_hash"):
        verify_chain(records)


# ── Proof card ───────────────────────────────────────────────────────────────

def test_proof_card_traces_spec_and_ledger():
    spec = _spec(risk="high",
                 objects_affected=["~/notes/plan.md"],
                 failure_modes=["partial write"],
                 rollback_procedure="restore snapshot",
                 rollback_validated=True,
                 simulation={"ok": True, "detail": "dry-run ok"})
    records = _chain(2)
    records[1]["kind"] = "approval"
    records[1]["record"] = {"approved_by": "owner"}
    card = build_proof_card(spec, records)
    assert card["what"] == "rewrite config"
    assert card["risk"] == "high"
    assert card["why_allowed"]["approval_level_required"] == "owner_password"
    assert card["why_allowed"]["approvals"][0]["by"] == "owner"
    assert card["will_access"] == ["~/notes/plan.md"]
    assert card["data_leaves_device"]["local_only"] is True
    assert "partial write" in card["what_can_go_wrong"]
    assert card["reversal"]["validated"] is True
    assert card["verification"]["simulation_ok"] is True
    assert card["ledger_seqs"] == [1, 2]


def test_proof_card_exposes_blockers_honestly():
    card = build_proof_card(_spec(risk="high"))  # no sim, no rollback
    blockers = card["why_allowed"]["blocked_reasons"]
    assert any("simulation" in b for b in blockers)
    assert any("rollback" in b for b in blockers)
    assert card["execution"]["executed"] is False
