"""Transparent self-model for limitations, focus and calibration state."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class CognitiveSelf:
    owner_id: str
    identity: str = "IRA NeuroKernel"
    role: str = "owner-controlled cognitive orchestrator"
    current_focus: str | None = None
    focus_event_id: str | None = None
    calibrated: bool = False
    limitations: list[str] = field(default_factory=lambda: [
        "not equivalent to a biological human brain",
        "neural transition model starts untrained",
        "cannot execute actions from cognition output",
        "cannot create goals without explicit owner input",
        "predictions are estimates and report uncertainty",
    ])
    updated_at: float = field(default_factory=time.time)

    def focus(self, summary: str, event_id: str) -> None:
        self.current_focus = summary
        self.focus_event_id = event_id
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return asdict(self)


class SelfModel:
    def __init__(self) -> None:
        self._owners: dict[str, CognitiveSelf] = {}

    def for_owner(self, owner_id: str) -> CognitiveSelf:
        if owner_id not in self._owners:
            self._owners[owner_id] = CognitiveSelf(owner_id=owner_id)
        return self._owners[owner_id]


__all__ = ["CognitiveSelf", "SelfModel"]
