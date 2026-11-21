"""Executive action arbitration with a deterministic safety brainstem."""
from __future__ import annotations

from cognition.capabilities import CapabilityRegistry
from cognition.goals import GoalArbiter
from cognition.models import ActionProposal, Decision, Prediction, SelectedAction, clamp


class SafetyBrainstem:
    """Non-neural invariant layer. It can only allow planning, require approval or block."""

    def decide(self, proposal: ActionProposal, prediction: Prediction, registry: CapabilityRegistry) -> tuple[Decision, str, str | None]:
        capability = registry.get(proposal.capability_id)
        if capability is None:
            return Decision.BLOCK, "capability is not declared in the ABI", None
        if proposal.risk == "critical":
            return Decision.BLOCK, "critical-risk proposals are blocked", None
        if proposal.risk != capability.risk:
            return Decision.BLOCK, "proposal risk does not match the capability contract", None
        if capability.side_effects or prediction.risk >= 0.45:
            return Decision.REQUIRE_APPROVAL, "side effects or predicted risk require the existing approval flow", capability.approval
        return Decision.ALLOW_PLAN, "read-only or side-effect-free planning is allowed", None


class ActionSelector:
    def __init__(self, goals: GoalArbiter, capabilities: CapabilityRegistry) -> None:
        self.goals = goals
        self.capabilities = capabilities
        self.brainstem = SafetyBrainstem()

    def select(self, proposal: ActionProposal, prediction: Prediction) -> SelectedAction:
        decision, reason, approval = self.brainstem.decide(proposal, prediction, self.capabilities)
        goal_value = self.goals.relevance(proposal.owner_id, proposal.intent)
        score = (
            0.38 * goal_value
            + 0.30 * prediction.success_probability
            + (0.08 if proposal.reversible else 0.0)
            - 0.16 * prediction.risk
            - 0.08 * prediction.uncertainty
        )
        if decision == Decision.BLOCK:
            score = 0.0
        return SelectedAction(
            proposal=proposal,
            prediction=prediction,
            decision=decision,
            reason=reason,
            score=round(clamp(score), 6),
            approval_required=approval,
        )


__all__ = ["ActionSelector", "SafetyBrainstem"]
