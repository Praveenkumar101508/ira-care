"""Typed cognitive records shared by the kernel, persistence and API layers."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


def clamp(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


class EventKind(StrEnum):
    PERCEPT = "percept"
    MEMORY = "memory"
    GOAL = "goal"
    PREDICTION = "prediction"
    ACTION = "action"
    OUTCOME = "outcome"
    SECURITY = "security"
    INTERNAL = "internal"


class GoalState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class Decision(StrEnum):
    ALLOW_PLAN = "allow_plan"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass(frozen=True)
class CognitiveEvent:
    user_id: str
    kind: EventKind
    source: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    trust: float = 0.5
    confidence: float = 0.5
    salience: float = 0.5
    urgency: float = 0.0
    novelty: float = 0.5
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.user_id or not self.summary.strip():
            raise ValueError("user_id and summary are required")
        if len(self.summary) > 1000:
            raise ValueError("summary exceeds 1000 characters")
        for name in ("trust", "confidence", "salience", "urgency", "novelty"):
            object.__setattr__(self, name, clamp(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["kind"] = self.kind.value
        return out


@dataclass(frozen=True)
class WorkspaceItem:
    event: CognitiveEvent
    priority: float
    admitted_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "priority": self.priority,
            "admitted_at": self.admitted_at,
            "expires_at": self.expires_at,
        }


@dataclass
class Goal:
    owner_id: str
    title: str
    desired_outcome: str
    priority: float = 0.5
    state: GoalState = GoalState.ACTIVE
    deadline: str | None = None
    source: str = "owner"
    goal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.owner_id or not self.title.strip() or not self.desired_outcome.strip():
            raise ValueError("owner_id, title and desired_outcome are required")
        self.priority = clamp(self.priority)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["state"] = self.state.value
        return out


@dataclass
class DriveState:
    safety_pressure: float = 0.05
    privacy_pressure: float = 0.05
    commitment_pressure: float = 0.15
    uncertainty_pressure: float = 0.25
    attention_load: float = 0.0
    energy_reserve: float = 1.0

    def normalize(self) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, clamp(getattr(self, name)))

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class Belief:
    key: str
    value: Any
    confidence: float
    evidence_ids: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Prediction:
    owner_id: str
    action: str
    expected_effects: list[str]
    success_probability: float
    risk: float
    uncertainty: float
    reversible: bool
    model_version: str
    prediction_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Capability:
    capability_id: str
    description: str
    risk: str
    side_effects: bool
    reversible: bool
    approval: str
    input_schema: dict[str, Any]
    platforms: tuple[str, ...] = ("ira",)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["platforms"] = list(self.platforms)
        return out


@dataclass(frozen=True)
class ActionProposal:
    owner_id: str
    capability_id: str
    intent: str
    arguments: dict[str, Any]
    risk: str
    reversible: bool
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectedAction:
    proposal: ActionProposal
    prediction: Prediction
    decision: Decision
    reason: str
    score: float
    approval_required: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal.to_dict(),
            "prediction": self.prediction.to_dict(),
            "decision": self.decision.value,
            "reason": self.reason,
            "score": self.score,
            "approval_required": self.approval_required,
            "executed": False,
        }


__all__ = [
    "ActionProposal", "Belief", "Capability", "CognitiveEvent", "Decision",
    "DriveState", "EventKind", "Goal", "GoalState", "Prediction",
    "SelectedAction", "WorkspaceItem", "clamp",
]
