"""Offline-style consolidation that extracts reviewable patterns without mutation."""
from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field

from cognition.models import CognitiveEvent

_TOKEN = re.compile(r"[a-z0-9]{4,}")
_STOP = {"that", "this", "with", "from", "have", "will", "your", "into", "about"}


@dataclass(frozen=True)
class ConsolidationReport:
    owner_id: str
    events_reviewed: int
    recurring_patterns: list[dict]
    memory_candidates: list[str]
    adapter_candidate_created: bool = False
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class SleepConsolidator:
    def consolidate(self, owner_id: str, events: list[CognitiveEvent]) -> ConsolidationReport:
        owned = [event for event in events if event.user_id == owner_id][-200:]
        counts = Counter(
            token
            for event in owned
            for token in _TOKEN.findall(event.summary.lower())
            if token not in _STOP
        )
        patterns = [{"token": token, "occurrences": count} for token, count in counts.most_common(8) if count >= 2]
        candidates = [
            f"Review recurring context: {item['token']} ({item['occurrences']} events)"
            for item in patterns[:5]
        ]
        return ConsolidationReport(
            owner_id=owner_id,
            events_reviewed=len(owned),
            recurring_patterns=patterns,
            memory_candidates=candidates,
        )


__all__ = ["ConsolidationReport", "SleepConsolidator"]
