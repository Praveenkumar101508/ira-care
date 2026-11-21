"""Cognitive immune screening for low-trust percepts before belief formation.

This composes IRA's existing prompt-injection detector with an owner-scoped,
bounded audit history.  It quarantines suspicious external data; it does not
claim that heuristic screening alone is a complete security boundary.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass

from cognition.models import CognitiveEvent, clamp
from utils.prompt_safety import check_adversarial_content


@dataclass(frozen=True)
class ImmuneAssessment:
    owner_id: str
    event_id: str
    source: str
    flags: tuple[str, ...]
    risk: float
    quarantined: bool
    reason: str

    def to_dict(self) -> dict:
        out = asdict(self)
        out["flags"] = list(self.flags)
        return out


class CognitiveImmuneSystem:
    def __init__(self, capacity: int = 256) -> None:
        self._history: deque[ImmuneAssessment] = deque(maxlen=max(32, min(int(capacity), 8192)))

    def inspect(self, event: CognitiveEvent) -> ImmuneAssessment:
        payload_text = json.dumps(event.payload, ensure_ascii=False, default=str)[:4000]
        flags = tuple(check_adversarial_content(f"{event.summary}\n{payload_text}"))
        low_trust = event.trust < 0.8
        quarantined = low_trust and bool(flags)
        risk = clamp((0.18 * len(flags)) + (0.35 if low_trust else 0.0))
        if quarantined:
            reason = "low-trust content contains adversarial instruction patterns"
        elif flags:
            reason = "patterns recorded for audit; trusted owner/subsystem input remains visible"
        else:
            reason = "no known adversarial instruction pattern detected"
        assessment = ImmuneAssessment(
            owner_id=event.user_id,
            event_id=event.event_id,
            source=event.source,
            flags=flags,
            risk=risk,
            quarantined=quarantined,
            reason=reason,
        )
        self._history.append(assessment)
        return assessment

    def report(self, owner_id: str) -> dict:
        items = [item for item in self._history if item.owner_id == owner_id]
        quarantined = [item for item in items if item.quarantined]
        return {
            "inspected": len(items),
            "quarantined": len(quarantined),
            "recent_quarantines": [item.to_dict() for item in reversed(quarantined[-10:])],
            "heuristic_only": True,
        }

    def is_quarantined(self, event_id: str) -> bool:
        return any(item.event_id == event_id and item.quarantined for item in self._history)


__all__ = ["CognitiveImmuneSystem", "ImmuneAssessment"]
