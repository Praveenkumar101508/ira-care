"""NeuroKernel orchestration façade; planning is explicit and never execution."""
from __future__ import annotations

from cognition.action_selection import ActionSelector
from cognition.android_bridge import AndroidBridge
from cognition.calibration import PredictionLedger
from cognition.capabilities import CapabilityRegistry
from cognition.cognitive_loop import CognitiveCycle, CognitiveCycleHistory, choose_alternative
from cognition.consolidation import SleepConsolidator
from cognition.drives import HomeostaticController
from cognition.event_bus import CognitiveEventBus
from cognition.goals import GoalArbiter
from cognition.immune import CognitiveImmuneSystem
from cognition.models import ActionProposal, CognitiveEvent, EventKind, Goal, SelectedAction
from cognition.plasticity import PlasticityQuarantine
from cognition.settings import NeuroKernelSettings
from cognition.self_model import SelfModel
from cognition.store import CognitiveStore
from cognition.workspace import GlobalWorkspace
from cognition.world_model import PredictiveWorldModel


class NeuroKernel:
    def __init__(self, settings: NeuroKernelSettings | None = None) -> None:
        self.settings = settings or NeuroKernelSettings.from_env()
        self.events = CognitiveEventBus(self.settings.event_capacity, self.settings.subscriber_capacity)
        self.workspace = GlobalWorkspace(self.settings.workspace_capacity)
        self.goals = GoalArbiter()
        self.drives = HomeostaticController()
        self.self_model = SelfModel()
        self.world = PredictiveWorldModel()
        self.capabilities = CapabilityRegistry()
        self.selector = ActionSelector(self.goals, self.capabilities)
        self.predictions = PredictionLedger()
        self.cycles = CognitiveCycleHistory()
        # Keep at least one immune assessment for every buffered source event so
        # quarantined content cannot re-enter later sleep consolidation after
        # its assessment has aged out.
        self.immune = CognitiveImmuneSystem(self.settings.event_capacity)
        self.android = AndroidBridge(self.capabilities, self.settings.android_bridge_enabled)
        self.consolidator = SleepConsolidator()
        self.plasticity = PlasticityQuarantine(self.settings.plasticity_enabled)
        self.store = CognitiveStore(self.settings.persistence_enabled)

    def require_enabled(self) -> None:
        if not self.settings.enabled:
            raise RuntimeError("NeuroKernel is disabled; set IRA_NEUROKERNEL_ENABLED=true")

    def status(self, owner_id: str) -> dict:
        calibration = self.predictions.report(owner_id)
        return {
            "enabled": self.settings.enabled,
            "mode": "planning_only",
            "human_brain_equivalent": False,
            "neural_world_model": self.world.neural.version,
            "persistence_enabled": self.settings.persistence_enabled,
            "plasticity_enabled": self.settings.plasticity_enabled,
            "android_bridge_enabled": self.settings.android_bridge_enabled,
            "workspace": {"capacity": self.workspace.capacity, "active": len(self.workspace.snapshot(owner_id))},
            "events_buffered": len(self.events.recent(owner_id, self.settings.event_capacity)),
            "capabilities": len(self.capabilities.list()),
            "cognitive_cycles": len(self.cycles.recent(owner_id, 50)),
            "calibration": calibration.to_dict(),
            "immune": self.immune.report(owner_id),
            "guarantees": {
                "executes_actions": False,
                "owner_goals_only": True,
                "bounded_attention": True,
                "uncontrolled_self_modification": False,
                "outcomes_update_weights": False,
                "low_trust_adversarial_percepts_form_beliefs": False,
                "existing_shadow_world_required_for_execution": True,
            },
        }

    async def perceive(self, event: CognitiveEvent) -> dict:
        self.require_enabled()
        assessment = self.immune.inspect(event)
        relevance = self.goals.relevance(event.user_id, event.summary)
        await self.events.publish(event)
        if assessment.quarantined:
            security_event = CognitiveEvent(
                user_id=event.user_id,
                kind=EventKind.SECURITY,
                source="cognitive_immune_system",
                summary=f"Quarantined suspicious percept from {event.source}",
                payload={"quarantined_event_id": event.event_id, "flags": list(assessment.flags)},
                trust=1.0,
                confidence=1.0,
                salience=max(0.8, assessment.risk),
                urgency=assessment.risk,
                novelty=0.9,
            )
            await self.events.publish(security_event)
            admitted = self.workspace.admit(security_event, 0.0)
            if admitted is not None:
                self.self_model.for_owner(event.user_id).focus(security_event.summary, security_event.event_id)
            load = len(self.workspace.snapshot(event.user_id)) / self.workspace.capacity
            drives = self.drives.respond(security_event, load)
            original_persisted = await self.store.append_event(event)
            security_persisted = await self.store.append_event(security_event)
            return {
                "event": event.to_dict(),
                "workspace_admitted": False,
                "attention_priority": None,
                "goal_relevance": relevance,
                "drives": drives.to_dict(),
                "persisted": original_persisted and security_persisted,
                "immune": assessment.to_dict(),
                "security_event": security_event.to_dict(),
            }
        admitted = self.workspace.admit(event, relevance)
        if admitted is not None and self.workspace.snapshot(event.user_id)[0].event.event_id == event.event_id:
            self.self_model.for_owner(event.user_id).focus(event.summary, event.event_id)
        load = len(self.workspace.snapshot(event.user_id)) / self.workspace.capacity
        drives = self.drives.respond(event, load)
        self.world.observe(
            key=f"source:{event.source}:latest",
            value={"kind": event.kind.value, "summary": event.summary},
            confidence=event.confidence * event.trust,
            evidence_id=event.event_id,
        )
        persisted = await self.store.append_event(event)
        return {
            "event": event.to_dict(),
            "workspace_admitted": admitted is not None,
            "attention_priority": admitted.priority if admitted else None,
            "goal_relevance": relevance,
            "drives": drives.to_dict(),
            "persisted": persisted,
            "immune": assessment.to_dict(),
        }

    async def add_goal(self, goal: Goal) -> Goal:
        self.require_enabled()
        self.goals.add(goal)
        await self.store.save_goal(goal)
        await self.perceive(CognitiveEvent(
            user_id=goal.owner_id, kind=EventKind.GOAL, source="owner",
            summary=f"Goal added: {goal.title}", payload={"goal_id": goal.goal_id},
            trust=1.0, confidence=1.0, salience=goal.priority, urgency=goal.priority,
        ))
        return goal

    async def predict(self, owner_id: str, action: str, context: dict | None = None):
        self.require_enabled()
        prediction = self.world.predict(owner_id, action, context)
        self.predictions.register(prediction)
        await self.store.append_prediction(prediction)
        return prediction

    async def plan(self, proposal: ActionProposal) -> SelectedAction:
        self.require_enabled()
        prediction = await self.predict(proposal.owner_id, proposal.intent, proposal.arguments)
        selected = self.selector.select(proposal, prediction)
        await self.perceive(CognitiveEvent(
            user_id=proposal.owner_id, kind=EventKind.ACTION, source="executive_cortex",
            summary=f"Planned {proposal.capability_id}: {proposal.intent}",
            payload={
                "proposal_id": proposal.proposal_id,
                "prediction_id": prediction.prediction_id,
                "decision": selected.decision.value,
                "executed": False,
            },
            trust=1.0, confidence=1.0 - prediction.uncertainty,
            salience=max(prediction.risk, 0.4), urgency=prediction.risk,
        ))
        return selected

    async def run_cycle(
        self,
        owner_id: str,
        summary: str,
        proposals: list[ActionProposal],
        *,
        source: str = "owner",
    ) -> CognitiveCycle:
        self.require_enabled()
        if not summary.strip() or len(summary) > 1000:
            raise ValueError("cycle summary must contain 1-1000 characters")
        if not 2 <= len(proposals) <= 8:
            raise ValueError("a cognitive cycle requires 2-8 alternatives")
        if any(proposal.owner_id != owner_id for proposal in proposals):
            raise PermissionError("all cycle proposals must belong to the owner")

        percept = CognitiveEvent(
            user_id=owner_id,
            kind=EventKind.PERCEPT,
            source=source,
            summary=summary,
            payload={"alternatives": len(proposals), "planning_only": True},
            trust=1.0 if source == "owner" else 0.8,
            confidence=0.9,
            salience=0.75,
            novelty=0.7,
        )
        await self.perceive(percept)
        alternatives = [await self.plan(proposal) for proposal in proposals]
        selected = choose_alternative(alternatives)
        cycle = self.cycles.append(CognitiveCycle(
            owner_id=owner_id,
            summary=summary,
            percept_event_id=percept.event_id,
            alternatives=tuple(alternatives),
            selected_proposal_id=selected.proposal.proposal_id if selected else None,
        ))
        await self.store.append_cycle(cycle)
        await self.perceive(CognitiveEvent(
            user_id=owner_id,
            kind=EventKind.INTERNAL,
            source="global_workspace",
            summary="Cognitive cycle selected a plan" if selected else "Cognitive cycle found no safe plan",
            payload={
                "cycle_id": cycle.cycle_id,
                "selected_proposal_id": cycle.selected_proposal_id,
                "executed": False,
            },
            trust=1.0,
            confidence=1.0,
            salience=0.65,
            urgency=max((item.prediction.risk for item in alternatives), default=0.0),
        ))
        return cycle

    async def record_prediction_outcome(
        self,
        owner_id: str,
        prediction_id: str,
        *,
        succeeded: bool,
        observed_effects: list[str] | tuple[str, ...] = (),
        safety_incident: bool = False,
    ) -> dict:
        self.require_enabled()
        prediction = self.predictions.get(owner_id, prediction_id)
        event = CognitiveEvent(
            user_id=owner_id,
            kind=EventKind.OUTCOME,
            source="owner_outcome",
            summary=f"Observed {'success' if succeeded else 'failure'} for predicted action: {prediction.action[:700]}",
            payload={
                "prediction_id": prediction_id,
                "succeeded": bool(succeeded),
                "observed_effects": list(observed_effects),
                "safety_incident": bool(safety_incident),
            },
            trust=1.0,
            confidence=1.0,
            salience=0.9 if safety_incident else 0.65,
            urgency=1.0 if safety_incident else 0.2,
            novelty=0.8,
        )
        outcome = self.predictions.record(
            owner_id,
            prediction_id,
            succeeded=succeeded,
            observed_effects=observed_effects,
            safety_incident=safety_incident,
            evidence_event_id=event.event_id,
        )
        await self.perceive(event)
        await self.store.append_outcome(outcome)
        self.world.observe(
            key=f"owner:{owner_id}:prediction:{prediction_id}:outcome",
            value={"succeeded": bool(succeeded), "safety_incident": bool(safety_incident)},
            confidence=1.0,
            evidence_id=event.event_id,
        )
        report = self.predictions.report(owner_id)
        self.self_model.for_owner(owner_id).calibrated = report.calibrated
        return {"outcome": outcome.to_dict(), "calibration": report.to_dict(), "weights_updated": False}

    def sleep(self, owner_id: str):
        self.require_enabled()
        events = [
            event for event in reversed(self.events.recent(owner_id, 200))
            if not self.immune.is_quarantined(event.event_id)
        ]
        report = self.consolidator.consolidate(owner_id, events)
        self.drives.recover()
        return report

    def snapshot(self, owner_id: str) -> dict:
        return {
            "status": self.status(owner_id),
            "self": self.self_model.for_owner(owner_id).to_dict(),
            "workspace": [item.to_dict() for item in self.workspace.snapshot(owner_id)],
            "goals": [goal.to_dict() for goal in self.goals.list(owner_id, include_inactive=True)],
            "drives": self.drives.state.to_dict(),
            "beliefs": [belief.to_dict() for belief in self.world.beliefs()],
            "devices": [device.to_dict() for device in self.android.list(owner_id)],
            "calibration": self.predictions.report(owner_id).to_dict(),
            "recent_outcomes": [item.to_dict() for item in self.predictions.recent(owner_id)],
            "recent_cycles": [cycle.to_dict() for cycle in self.cycles.recent(owner_id)],
            "immune": self.immune.report(owner_id),
        }


_kernel: NeuroKernel | None = None


def get_kernel() -> NeuroKernel:
    global _kernel
    current = NeuroKernelSettings.from_env()
    if _kernel is None or _kernel.settings != current:
        _kernel = NeuroKernel(current)
    return _kernel


def reset_kernel() -> None:
    global _kernel
    _kernel = None


__all__ = ["NeuroKernel", "get_kernel", "reset_kernel"]
