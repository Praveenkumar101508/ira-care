"""A bounded global workspace implementing deterministic attention competition."""
from __future__ import annotations

import time

from cognition.models import CognitiveEvent, EventKind, WorkspaceItem, clamp

_KIND_BONUS = {
    EventKind.SECURITY: 0.18,
    EventKind.GOAL: 0.10,
    EventKind.OUTCOME: 0.08,
    EventKind.ACTION: 0.05,
}


class GlobalWorkspace:
    def __init__(self, capacity: int = 12, ttl_seconds: float = 300.0) -> None:
        self.capacity = max(4, min(int(capacity), 64))
        self.ttl_seconds = max(5.0, float(ttl_seconds))
        self._items: dict[str, WorkspaceItem] = {}

    @staticmethod
    def attention_score(event: CognitiveEvent, goal_relevance: float = 0.0) -> float:
        score = (
            0.24 * event.salience
            + 0.19 * event.novelty
            + 0.21 * event.urgency
            + 0.12 * event.confidence
            + 0.09 * event.trust
            + 0.15 * clamp(goal_relevance)
            + _KIND_BONUS.get(event.kind, 0.0)
        )
        return round(clamp(score), 6)

    def admit(self, event: CognitiveEvent, goal_relevance: float = 0.0) -> WorkspaceItem | None:
        self.expire()
        priority = self.attention_score(event, goal_relevance)
        item = WorkspaceItem(
            event=event,
            priority=priority,
            expires_at=time.time() + self.ttl_seconds,
        )
        if len(self._items) < self.capacity:
            self._items[event.event_id] = item
            return item
        lowest = min(self._items.values(), key=lambda current: (current.priority, current.admitted_at))
        if priority <= lowest.priority:
            return None
        self._items.pop(lowest.event.event_id, None)
        self._items[event.event_id] = item
        return item

    def expire(self, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        expired = [key for key, item in self._items.items() if item.expires_at <= moment]
        for key in expired:
            self._items.pop(key, None)
        return len(expired)

    def snapshot(self, owner_id: str) -> list[WorkspaceItem]:
        self.expire()
        return sorted(
            (item for item in self._items.values() if item.event.user_id == owner_id),
            key=lambda item: (-item.priority, -item.admitted_at),
        )


__all__ = ["GlobalWorkspace"]
