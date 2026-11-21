"""Capability ABI shared by IRA and future Android/AOSP agent shells."""
from __future__ import annotations

from cognition.models import Capability


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Capability] = {}
        self._register_defaults()

    def register(self, capability: Capability) -> None:
        if capability.capability_id in self._items:
            raise ValueError(f"capability already registered: {capability.capability_id}")
        self._items[capability.capability_id] = capability

    def get(self, capability_id: str) -> Capability | None:
        return self._items.get(capability_id)

    def list(self) -> list[Capability]:
        return sorted(self._items.values(), key=lambda item: item.capability_id)

    def export_manifest(self) -> dict:
        return {
            "abi": "ira.capability/v1",
            "contract": {
                "simulation_before_side_effects": True,
                "explicit_risk": True,
                "explicit_reversibility": True,
                "owner_approval_preserved": True,
            },
            "capabilities": [item.to_dict() for item in self.list()],
        }

    def _register_defaults(self) -> None:
        defaults = (
            Capability("workspace.observe", "Read the current bounded cognitive workspace", "low", False, True, "auto", {}, ("ira", "android")),
            Capability("life_map.read", "Read the owner's Life Map", "low", False, True, "auto", {}, ("ira", "android")),
            Capability("memory.search", "Search owner-scoped local memory", "low", False, True, "auto", {"query": "string"}, ("ira", "android")),
            Capability("action.plan", "Create a simulated action plan without execution", "low", False, True, "auto", {"intent": "string"}, ("ira", "android")),
            Capability("android.app_function", "Plan invocation of a declared Android app function", "medium", True, False, "plan_approval", {"package": "string", "function": "string", "arguments": "object"}, ("android",)),
            Capability("external.communicate", "Plan an external communication", "medium", True, False, "plan_approval", {"channel": "string", "recipient": "string"}, ("ira", "android")),
        )
        for item in defaults:
            self.register(item)


__all__ = ["CapabilityRegistry"]
