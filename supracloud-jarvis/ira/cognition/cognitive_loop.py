"""Bounded counterfactual cycles that select plans but never execute them."""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from cognition.models import Decision, SelectedAction


@dataclass(frozen=True)
class CognitiveCycle:
    owner_id: str
    summary: str
    percept_event_id: str
    alternatives: tuple[SelectedAction, ...]
    selected_proposal_id: str | None
    cycle_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.owner_id or not self.summary.strip() or not self.percept_event_id:
            raise ValueError("owner_id, summary and percept_event_id are required")
        if not 2 <= len(self.alternatives) <= 8:
            raise ValueError("a cognitive cycle requires 2-8 alternatives")
        if self.selected_proposal_id and not any(
            item.proposal.proposal_id == self.selected_proposal_id for item in self.alternatives
        ):
            raise ValueError("selected_proposal_id must identify a cycle alternative")

    @property
    def selected(self) -> SelectedAction | None:
        return next(
            (item for item in self.alternatives if item.proposal.proposal_id == self.selected_proposal_id),
            None,
        )

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "owner_id": self.owner_id,
            "summary": self.summary,
            "percept_event_id": self.percept_event_id,
            "alternatives": [item.to_dict() for item in self.alternatives],
            "selected": self.selected.to_dict() if self.selected else None,
            "selected_proposal_id": self.selected_proposal_id,
            "status": "planned" if self.selected else "no_safe_plan",
            "executed": False,
            "created_at": self.created_at,
        }


def choose_alternative(alternatives: list[SelectedAction]) -> SelectedAction | None:
    eligible = [item for item in alternatives if item.decision != Decision.BLOCK]
    if not eligible:
        return None
    decision_rank = {Decision.ALLOW_PLAN: 2, Decision.REQUIRE_APPROVAL: 1, Decision.BLOCK: 0}
    return max(
        eligible,
        key=lambda item: (
            decision_rank[item.decision],
            item.score,
            -item.prediction.risk,
            -item.prediction.uncertainty,
        ),
    )


class CognitiveCycleHistory:
    def __init__(self, capacity: int = 128) -> None:
        self._cycles: deque[CognitiveCycle] = deque(maxlen=max(16, min(int(capacity), 1024)))

    def append(self, cycle: CognitiveCycle) -> CognitiveCycle:
        self._cycles.append(cycle)
        return cycle

    def recent(self, owner_id: str, limit: int = 10) -> list[CognitiveCycle]:
        bounded = max(1, min(int(limit), 50))
        return [cycle for cycle in reversed(self._cycles) if cycle.owner_id == owner_id][:bounded]


__all__ = ["CognitiveCycle", "CognitiveCycleHistory", "choose_alternative"]
