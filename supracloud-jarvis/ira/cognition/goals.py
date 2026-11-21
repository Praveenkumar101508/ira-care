"""Owner-authored goal memory and relevance arbitration."""
from __future__ import annotations

import re

from cognition.models import Goal, GoalState

_TOKEN = re.compile(r"[a-z0-9]{3,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class GoalArbiter:
    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}

    def add(self, goal: Goal) -> Goal:
        if goal.source != "owner":
            raise ValueError("new goals must be explicitly owner-authored")
        self._goals[goal.goal_id] = goal
        return goal

    def list(self, owner_id: str, include_inactive: bool = False) -> list[Goal]:
        goals = [g for g in self._goals.values() if g.owner_id == owner_id]
        if not include_inactive:
            goals = [g for g in goals if g.state == GoalState.ACTIVE]
        return sorted(goals, key=lambda goal: (-goal.priority, goal.created_at))

    def relevance(self, owner_id: str, text: str) -> float:
        target = _tokens(text)
        if not target:
            return 0.0
        best = 0.0
        for goal in self.list(owner_id):
            goal_tokens = _tokens(f"{goal.title} {goal.desired_outcome}")
            overlap = len(target & goal_tokens) / max(1, len(target | goal_tokens))
            best = max(best, overlap * goal.priority)
        return min(best, 1.0)

    def set_state(self, owner_id: str, goal_id: str, state: GoalState) -> Goal:
        goal = self._goals.get(goal_id)
        if goal is None or goal.owner_id != owner_id:
            raise KeyError(goal_id)
        goal.state = state
        return goal


__all__ = ["GoalArbiter"]
