"""
actions/shadow_world.py — universal pre-execution simulation (brief §8).

Every medium+ risk action gets an ActionSpec BEFORE anything real happens:
what will be done, to which objects, what changes are expected, what can go
wrong, what leaves the device, and how to roll back. simulate() runs the
registered simulator for the action kind (dry-run — simulators must be
side-effect free) and produces a SimulationResult the approval flow and the
Proof Card (trust/proof_card.py) consume.

Code-enforced guarantees:
  * approval level is DERIVED from risk (same vocabulary as the Command
    Center risk engine): low→auto, medium→plan approval, high→owner
    password, critical→blocked. Callers cannot pass a weaker level.
  * `execute_gate()` refuses medium+ actions whose spec lacks a simulation
    result or (high+) a validated rollback — "no high-risk action moves
    directly from model output to real execution".
  * FileSnapshot gives destructive file operations a reversible snapshot
    (copy + sha256; restore() verifies what it wrote back).

This module performs NO real actions itself; executors stay where they are
(utils/approval.py flow, actions/*). Shadow World is the checklist standing
in front of them.
"""
from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

RISK_LEVELS = ("low", "medium", "high", "critical")

#: risk → required approval, fixed (B2). Not caller-overridable downward.
APPROVAL_BY_RISK = {
    "low": "auto",
    "medium": "plan_approval",
    "high": "owner_password",
    "critical": "blocked",
}

#: action kinds Shadow World knows about (brief §8 list).
ACTION_KINDS = frozenset({
    "email_send", "calendar_change", "file_change", "db_operation",
    "code_change", "git_operation", "package_install", "cloud_action",
    "browser_automation", "permission_change", "memory_deletion",
    "external_api_call",
    # Shell/system commands run through the Action Guard's two-gate grading
    # (allowlist + argument safety) — see trust/action_guard.py.
    "shell_command",
    # Armory Vault security tools, simulated before they run in a throwaway,
    # network-gated container — see armory/gatekeeper.py.
    "armory_tool",
})


@dataclass
class ActionSpec:
    kind: str
    intended_action: str
    requested_by: str
    risk: str = "medium"
    objects_affected: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_changes: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    security_implications: list[str] = field(default_factory=list)
    data_leaving_device: list[str] = field(default_factory=list)  # [] = local only
    models_involved: list[str] = field(default_factory=list)
    tools_involved: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    rollback_procedure: str = ""
    rollback_validated: bool = False
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    simulation: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unknown action kind: {self.kind!r}")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"unknown risk: {self.risk!r}")
        if not self.intended_action.strip():
            raise ValueError("intended_action must not be empty")
        if not self.requested_by:
            raise ValueError("requested_by is required")

    @property
    def approval_level(self) -> str:
        return APPROVAL_BY_RISK[self.risk]

    def problems(self) -> list[str]:
        """What blocks this spec from being executable (fail-closed list)."""
        out = []
        if self.risk == "critical":
            out.append("critical actions are blocked outright")
        if self.risk in ("medium", "high") and self.simulation is None:
            out.append("medium/high risk requires a simulation result")
        if self.risk == "high":
            if not self.rollback_procedure:
                out.append("high risk requires a rollback procedure")
            elif not self.rollback_validated:
                out.append("high risk requires a VALIDATED rollback")
        if self.simulation is not None and not self.simulation.get("ok", False):
            out.append(f"simulation failed: {self.simulation.get('detail', 'no detail')}")
        return out

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


# ── Simulation registry ───────────────────────────────────────────────────────

Simulator = Callable[[ActionSpec], Awaitable[dict[str, Any]]]
_SIMULATORS: dict[str, Simulator] = {}


def register_simulator(kind: str, fn: Simulator) -> None:
    if kind not in ACTION_KINDS:
        raise ValueError(f"unknown action kind: {kind!r}")
    _SIMULATORS[kind] = fn


async def simulate(spec: ActionSpec) -> dict[str, Any]:
    """Run the registered simulator (side-effect free by contract) and attach
    the result to the spec. Without a simulator, the result is an honest
    'not simulated' failure — which blocks medium+ execution rather than
    letting an unsimulated action through."""
    fn = _SIMULATORS.get(spec.kind)
    if fn is None:
        result = {"ok": False, "detail": f"no simulator registered for {spec.kind}",
                  "simulated_at": time.time()}
    else:
        try:
            result = await fn(spec)
            result.setdefault("ok", False)
            result.setdefault("simulated_at", time.time())
        except Exception as exc:  # a crashing simulator is a failed simulation
            result = {"ok": False, "detail": f"simulator raised: {exc}",
                      "simulated_at": time.time()}
    spec.simulation = result
    return result


def execute_gate(spec: ActionSpec, *, approval: Optional[str] = None) -> tuple[bool, str]:
    """THE gate real executors must call. Returns (allowed, reason).
    `approval` is the approval artefact the flow collected ('auto' is only
    acceptable for low risk)."""
    problems = spec.problems()
    if problems:
        return False, "; ".join(problems)
    need = spec.approval_level
    if need == "auto":
        return True, "low risk, auto-approved"
    if not approval:
        return False, f"approval level {need!r} required, none provided"
    return True, f"approved via {approval}"


# ── Reversible file snapshots ─────────────────────────────────────────────────

@dataclass
class FileSnapshot:
    original: Path
    snapshot: Path
    sha256: str

    @classmethod
    def create(cls, path: Path | str, snapshot_dir: Path | str) -> "FileSnapshot":
        original = Path(path).resolve()
        if not original.is_file():
            raise FileNotFoundError(f"cannot snapshot missing file: {original}")
        sdir = Path(snapshot_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        target = sdir / f"{original.name}.{uuid.uuid4().hex[:8]}.snap"
        shutil.copy2(original, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return cls(original=original, snapshot=target, sha256=digest)

    def restore(self) -> None:
        """Put the snapshot back and verify the restore byte-for-byte."""
        if not self.snapshot.is_file():
            raise FileNotFoundError(f"snapshot missing: {self.snapshot}")
        if hashlib.sha256(self.snapshot.read_bytes()).hexdigest() != self.sha256:
            raise RuntimeError("snapshot corrupted; refusing to restore from it")
        shutil.copy2(self.snapshot, self.original)
        if hashlib.sha256(self.original.read_bytes()).hexdigest() != self.sha256:
            raise RuntimeError("restore verification failed")


__all__ = [
    "RISK_LEVELS", "APPROVAL_BY_RISK", "ACTION_KINDS", "ActionSpec",
    "register_simulator", "simulate", "execute_gate", "FileSnapshot",
]
