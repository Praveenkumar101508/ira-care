"""Cognitive homeostasis: transparent pressures, not fabricated emotions."""
from __future__ import annotations

from cognition.models import CognitiveEvent, DriveState, EventKind


class HomeostaticController:
    def __init__(self) -> None:
        self.state = DriveState()

    def respond(self, event: CognitiveEvent, workspace_load: float) -> DriveState:
        state = self.state
        state.attention_load = workspace_load
        state.energy_reserve -= 0.006 + 0.02 * event.urgency
        state.uncertainty_pressure = 0.75 * state.uncertainty_pressure + 0.25 * (1 - event.confidence)
        if event.kind == EventKind.SECURITY:
            state.safety_pressure += 0.35 * event.salience
        else:
            state.safety_pressure *= 0.96
        if event.payload.get("data_leaves_device"):
            state.privacy_pressure += 0.30
        else:
            state.privacy_pressure *= 0.97
        if event.kind == EventKind.GOAL or event.urgency > 0.7:
            state.commitment_pressure += 0.15
        elif event.kind == EventKind.OUTCOME and event.payload.get("success") is True:
            state.commitment_pressure -= 0.12
        state.normalize()
        return state

    def recover(self, amount: float = 0.25) -> DriveState:
        amount = max(0.0, min(float(amount), 1.0))
        self.state.energy_reserve += amount
        self.state.attention_load *= 0.5
        self.state.uncertainty_pressure *= 0.8
        self.state.normalize()
        return self.state


__all__ = ["HomeostaticController"]
