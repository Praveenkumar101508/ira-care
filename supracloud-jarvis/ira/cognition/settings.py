"""Environment-controlled NeuroKernel settings; every capability defaults OFF."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(value, high))


@dataclass(frozen=True)
class NeuroKernelSettings:
    enabled: bool
    persistence_enabled: bool
    plasticity_enabled: bool
    android_bridge_enabled: bool
    workspace_capacity: int
    event_capacity: int
    subscriber_capacity: int

    @classmethod
    def from_env(cls) -> "NeuroKernelSettings":
        return cls(
            enabled=_flag("IRA_NEUROKERNEL_ENABLED"),
            persistence_enabled=_flag("IRA_NEUROKERNEL_PERSISTENCE_ENABLED"),
            plasticity_enabled=_flag("IRA_NEUROKERNEL_PLASTICITY_ENABLED"),
            android_bridge_enabled=_flag("IRA_NEUROKERNEL_ANDROID_ENABLED"),
            workspace_capacity=_bounded_int("IRA_NEUROKERNEL_WORKSPACE_CAPACITY", 12, 4, 64),
            event_capacity=_bounded_int("IRA_NEUROKERNEL_EVENT_CAPACITY", 512, 32, 4096),
            subscriber_capacity=_bounded_int("IRA_NEUROKERNEL_SUBSCRIBER_CAPACITY", 64, 8, 256),
        )


__all__ = ["NeuroKernelSettings"]
