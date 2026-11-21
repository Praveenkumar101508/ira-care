"""Quarantined neural plasticity: candidates require evidence and owner approval."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class AdapterCandidate:
    description: str
    baseline_score: float
    candidate_score: float
    safety_regressions: int
    candidate_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "quarantined"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class PlasticityQuarantine:
    def __init__(self, enabled: bool = False, minimum_gain: float = 0.02) -> None:
        self.enabled = enabled
        self.minimum_gain = minimum_gain
        self._candidates: dict[str, AdapterCandidate] = {}

    def propose(self, description: str, baseline_score: float, candidate_score: float, safety_regressions: int) -> AdapterCandidate:
        candidate = AdapterCandidate(description, baseline_score, candidate_score, safety_regressions)
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def activate(self, candidate_id: str, *, owner_approved: bool) -> AdapterCandidate:
        candidate = self._candidates[candidate_id]
        if not self.enabled:
            raise RuntimeError("neural plasticity is disabled")
        if not owner_approved:
            raise PermissionError("explicit owner approval is required")
        if candidate.safety_regressions:
            raise RuntimeError("candidate has safety regressions")
        if candidate.candidate_score - candidate.baseline_score < self.minimum_gain:
            raise RuntimeError("candidate does not meet the minimum measured gain")
        candidate.status = "approved_for_installation"
        return candidate

    def list(self) -> list[AdapterCandidate]:
        return list(self._candidates.values())


__all__ = ["AdapterCandidate", "PlasticityQuarantine"]
