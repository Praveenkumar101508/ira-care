"""Bounded local cognitive event bus with slow-consumer protection."""
from __future__ import annotations

import asyncio
from collections import deque

from cognition.models import CognitiveEvent


class CognitiveEventBus:
    def __init__(self, capacity: int = 512, subscriber_capacity: int = 64) -> None:
        self._events: deque[CognitiveEvent] = deque(maxlen=capacity)
        self._subscriber_capacity = subscriber_capacity
        self._subscribers: set[asyncio.Queue[CognitiveEvent]] = set()
        self._lock = asyncio.Lock()
        self.dropped_deliveries = 0

    async def publish(self, event: CognitiveEvent) -> None:
        async with self._lock:
            self._events.append(event)
            for queue in tuple(self._subscribers):
                if queue.full():
                    try:
                        queue.get_nowait()
                        self.dropped_deliveries += 1
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[CognitiveEvent]:
        queue: asyncio.Queue[CognitiveEvent] = asyncio.Queue(self._subscriber_capacity)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[CognitiveEvent]) -> None:
        self._subscribers.discard(queue)

    def recent(self, owner_id: str, limit: int = 100) -> list[CognitiveEvent]:
        safe_limit = max(1, min(int(limit), 500))
        return [event for event in reversed(self._events) if event.user_id == owner_id][:safe_limit]

    @property
    def size(self) -> int:
        return len(self._events)


__all__ = ["CognitiveEventBus"]
