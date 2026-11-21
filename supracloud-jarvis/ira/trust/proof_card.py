"""
trust/proof_card.py — the owner-facing explanation of one action (brief §9).

A Proof Card is a plain dict (UI renders it in PR 76) answering, in order:
what IRA intends to do / why it is allowed / what it will access / whether
anything leaves the device / what can go wrong / how it can be reversed /
what verification passed / who approved it. Built from an ActionSpec plus
its ledger records — nothing on the card is free text invented at render
time; every field traces to a spec or ledger entry.
"""
from __future__ import annotations

from typing import Any, Optional

from actions.shadow_world import ActionSpec


def build_proof_card(spec: ActionSpec,
                     ledger_records: Optional[list[dict[str, Any]]] = None,
                     ) -> dict[str, Any]:
    records = ledger_records or []
    approvals = [r for r in records if r.get("kind") == "approval"]
    verifications = [r for r in records if r.get("kind") == "verification"]
    executions = [r for r in records if r.get("kind") == "execution"]
    rollbacks = [r for r in records if r.get("kind") == "rollback"]

    local_only = not spec.data_leaving_device
    sim = spec.simulation or {}

    return {
        "action_id": spec.action_id,
        "what": spec.intended_action,
        "kind": spec.kind,
        "risk": spec.risk,
        "why_allowed": {
            "approval_level_required": spec.approval_level,
            "approvals": [
                {"by": r.get("record", {}).get("approved_by"),
                 "at": str(r.get("at", ""))} for r in approvals
            ],
            "blocked_reasons": spec.problems(),   # [] means clear to run
        },
        "will_access": spec.objects_affected,
        "data_leaves_device": {
            "local_only": local_only,
            "transmissions": spec.data_leaving_device,
            "models_involved": spec.models_involved,
            "tools_involved": spec.tools_involved,
        },
        "what_can_go_wrong": spec.failure_modes + spec.side_effects,
        "reversal": {
            "procedure": spec.rollback_procedure or "none documented",
            "validated": spec.rollback_validated,
            "rollbacks_recorded": len(rollbacks),
        },
        "verification": {
            "simulation_ok": bool(sim.get("ok")),
            "simulation_detail": sim.get("detail", ""),
            "checks_passed": [r.get("record", {}) for r in verifications],
        },
        "execution": {
            "executed": bool(executions),
            "results": [r.get("record", {}) for r in executions],
        },
        "requested_by": spec.requested_by,
        "ledger_seqs": [r.get("seq") for r in records],
    }


def summarise_proof_card(card: dict[str, Any]) -> dict[str, Any]:
    """A compact, screen-readable view of a Proof Card.

    Same data, fewer fields: what was attempted, what it touched, whether it was
    simulated, who approved it, whether it ran, whether it can be undone, and
    whether anything left the device. Every value is copied from ``card`` — this
    renderer computes no new claims and invents no text.
    """
    approvals = card.get("why_allowed", {}).get("approvals", [])
    execution = card.get("execution", {})
    verification = card.get("verification", {})
    reversal = card.get("reversal", {})
    leaves = card.get("data_leaves_device", {})
    blocked = card.get("why_allowed", {}).get("blocked_reasons", [])

    if blocked:
        status = "blocked"
    elif execution.get("executed"):
        status = "executed"
    elif approvals:
        status = "approved"
    else:
        status = "awaiting_approval"

    return {
        "action_id": card.get("action_id"),
        "what": card.get("what"),
        "kind": card.get("kind"),
        "risk": card.get("risk"),
        "status": status,
        "touched": card.get("will_access", []),
        "simulated": bool(verification.get("simulation_ok")),
        "simulation_detail": verification.get("simulation_detail", ""),
        "approval_required": card.get("why_allowed", {}).get("approval_level_required"),
        "approved_by": [a.get("by") for a in approvals if a.get("by")],
        "approved_at": [a.get("at") for a in approvals if a.get("at")],
        "blocked_reasons": blocked,
        "undo": reversal.get("procedure", "none documented"),
        "undo_validated": bool(reversal.get("validated")),
        "local_only": bool(leaves.get("local_only")),
        "ledger_seqs": card.get("ledger_seqs", []),
    }


__all__ = ["build_proof_card", "summarise_proof_card"]
