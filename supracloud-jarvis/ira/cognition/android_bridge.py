"""State-only Android bridge contracts; no device command is executed here."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

from cognition.capabilities import CapabilityRegistry


@dataclass
class CognitiveDevice:
    owner_id: str
    device_name: str
    platform: str
    declared_capabilities: list[str]
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    registered_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class AndroidBridge:
    def __init__(self, registry: CapabilityRegistry, enabled: bool = False) -> None:
        self.registry = registry
        self.enabled = enabled
        self._devices: dict[str, CognitiveDevice] = {}

    def register(self, owner_id: str, device_name: str, platform: str, capabilities: list[str]) -> CognitiveDevice:
        if not self.enabled:
            raise RuntimeError("Android bridge is disabled")
        if platform not in {"android", "aosp"}:
            raise ValueError("platform must be android or aosp")
        unknown = [item for item in capabilities if self.registry.get(item) is None]
        if unknown:
            raise ValueError(f"undeclared capabilities: {', '.join(unknown)}")
        device = CognitiveDevice(owner_id, device_name.strip(), platform, sorted(set(capabilities)))
        if not device.device_name:
            raise ValueError("device_name is required")
        self._devices[device.device_id] = device
        return device

    def list(self, owner_id: str) -> list[CognitiveDevice]:
        return [device for device in self._devices.values() if device.owner_id == owner_id]


__all__ = ["AndroidBridge", "CognitiveDevice"]
