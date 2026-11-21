"""
trust/action_guard.py — one generic guard in front of ANY agent action.

IRA already had the machinery: Shadow World simulation, risk-derived approval,
the hash-chained Trust Ledger, Proof Cards, the protected-path denylist and the
two-gate command safety. What it did not have was a single seam a *new* action
type could be dropped into. This module is that seam — extraction and a generic
interface, NOT a second safety implementation. Every decision below is still
made by the existing components.

    guard_action(descriptor)
        → adapter builds an ActionSpec (blast radius + derived risk)
        → Shadow World simulation (side-effect free)
        → block | require approval | allow          (risk-derived, not caller-set)
        → on confirm: execute, append intent + result to the Trust Ledger
        → return a Proof Card

Three action types are wired to prove the interface is generic:

  * ``file_change``    — delete/modify, with a rollback snapshot taken BEFORE
                         approval so a high-risk action has a *validated* undo
  * ``calendar_change``— reuses actions/calendar_dav.py
  * ``shell_command``  — graded by the same two gates the executor uses
                         (allowlist + argument/metacharacter safety)

Guarantees that are structural, not stylistic:
  * risk is DERIVED here; a caller may raise it, never lower it
  * critical actions are blocked outright — no approval can release them
  * preview() never executes and never mutates anything
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from actions.shadow_world import (
    ACTION_KINDS,
    APPROVAL_BY_RISK,
    RISK_LEVELS,
    ActionSpec,
    FileSnapshot,
)

logger = logging.getLogger("ira.trust.action_guard")

#: Where rollback snapshots are written before a destructive file operation.
SNAPSHOT_DIRNAME = ".ira_snapshots"

_RISK_ORDER = {level: i for i, level in enumerate(RISK_LEVELS)}


@dataclass
class ActionDescriptor:
    """A generic description of an action to guard.

    ``type`` is a Shadow World action kind; ``target`` is the thing acted on
    (a path, a calendar summary, a command line); ``params`` carries whatever
    the adapter needs. Deliberately not tied to email or calendar.
    """

    type: str
    target: str
    requested_by: str
    params: dict[str, Any] = field(default_factory=dict)
    #: Optional floor. The guard takes max(derived, requested) — never less.
    min_risk: Optional[str] = None
    description: str = ""
    #: The `(domain, action)` pair the tenant's Constitution is asked about
    #: (P9-07). Optional, and deliberately NOT derived from `type`: a shadow
    #: kind maps to more than one policy — `calendar_change` is both
    #: `calendar.create` and `calendar.delete`, which differ in risk and in
    #: maximum effect — so deriving it would grade the action nobody asked
    #: for. Absent in the cloud edition, the guard refuses: a generic guard
    #: that cannot say WHICH governed action this is must not run one.
    governed: Optional[tuple[str, str]] = None

    def __post_init__(self) -> None:
        if self.type not in ACTION_KINDS:
            raise ValueError(f"unknown action type: {self.type!r}")
        if self.min_risk is not None and self.min_risk not in RISK_LEVELS:
            raise ValueError(f"unknown risk: {self.min_risk!r}")
        if not self.requested_by:
            raise ValueError("requested_by is required")


#: An adapter turns a descriptor into (spec, simulator, executor).
Adapter = Callable[
    [ActionDescriptor],
    tuple[ActionSpec, Callable[[ActionSpec], Awaitable[dict]], Callable[[], Any]],
]

_ADAPTERS: dict[str, Adapter] = {}


def register_adapter(action_type: str, adapter: Adapter) -> None:
    """Register the adapter for an action type (the extension point)."""
    if action_type not in ACTION_KINDS:
        raise ValueError(f"unknown action type: {action_type!r}")
    _ADAPTERS[action_type] = adapter


def _max_risk(derived: str, floor: Optional[str]) -> str:
    if floor is None:
        return derived
    return derived if _RISK_ORDER[derived] >= _RISK_ORDER[floor] else floor


# ── Risk derivation (reuses the existing denylists, adds no new opinion) ──────

def _protected_paths() -> tuple[str, ...]:
    try:
        from coding.capability_policy import load_protected_paths

        return load_protected_paths()
    except Exception:  # noqa: BLE001 — a missing policy file must fail CLOSED-ish
        logger.warning("protected-path policy unavailable; treating none as protected")
        return ()


def _is_protected(path: Path) -> bool:
    text = str(path).replace("\\", "/")
    for pattern in _protected_paths():
        stem = pattern.replace("**", "").replace("*", "").strip("/")
        if stem and stem in text:
            return True
    # Secrets and credentials are protected regardless of the policy file.
    return any(
        marker in text.lower()
        for marker in (".env", ".ssh", "/etc/shadow", "/etc/passwd", "id_rsa", "secrets")
    )


def classify_file_risk(path: Path, operation: str) -> str:
    """delete/modify → high/medium; anything touching a protected path → critical."""
    if _is_protected(path):
        return "critical"
    return "high" if operation == "delete" else "medium"


def classify_command_risk(command: str) -> tuple[str, str]:
    """Grade a shell command with the SAME two gates the executor enforces.

    Returns ``(risk, detail)``. Blocked commands and unsafe arguments are
    critical (blocked outright); allowlisted read-only commands are low;
    anything else is high.
    """
    from agents.executor import (BLOCKED_COMMANDS, confined_to_run_dir, is_allowed,
                                 is_safe_path, runs_code)
    from utils.cmd_safety import safe_check

    stripped = (command or "").strip()
    if not stripped:
        return "critical", "empty command"

    try:
        first = shlex.split(stripped)[0].lower()
    except ValueError as exc:
        return "critical", f"unparseable command: {exc}"

    if Path(first).name in BLOCKED_COMMANDS:
        return "critical", f"{first!r} is on the hard-blocked list"

    ok, reason = safe_check(stripped)
    if not ok:
        return "critical", reason
    # The executor's own confinement, so a command it would refuse is never
    # graded as a safe read (`cat /tmp/id_rsa`, `git diff --no-index …`).
    parts = shlex.split(stripped)
    unsafe = next((a for a in parts[1:] if not is_safe_path(a)), None)
    if unsafe is not None:
        return "critical", f"restricted path in argument: {unsafe!r}"
    ok, reason = confined_to_run_dir(parts)
    if not ok:
        return "critical", reason

    if is_allowed(stripped) and runs_code(stripped):
        return "high", "allowlisted, but it runs code"
    if is_allowed(stripped):
        return "low", "allowlisted read-only command"
    return "high", "command is not on the read-only allowlist"


# ── Adapters ─────────────────────────────────────────────────────────────────

def _file_adapter(desc: ActionDescriptor):
    path = Path(desc.target).expanduser().resolve()
    operation = str(desc.params.get("operation", "modify")).lower()
    if operation not in ("delete", "modify"):
        raise ValueError(f"unsupported file operation: {operation!r}")

    risk = _max_risk(classify_file_risk(path, operation), desc.min_risk)

    snapshot: Optional[FileSnapshot] = None
    rollback = ""
    validated = False
    # A rollback snapshot is taken BEFORE approval — non-destructive, and the only
    # way a high-risk file operation can present a *validated* undo at gate time.
    if risk in ("high", "critical") and path.is_file():
        try:
            snapshot = FileSnapshot.create(path, path.parent / SNAPSHOT_DIRNAME)
            rollback = f"restore {path} from {snapshot.snapshot} (sha256 {snapshot.sha256[:12]}…)"
            validated = True
        except Exception as exc:  # noqa: BLE001 — no snapshot means no validated undo
            logger.warning("snapshot failed for %s: %s", path, exc)
            rollback = f"snapshot failed: {exc}"

    spec = ActionSpec(
        kind="file_change",
        risk=risk,
        requested_by=desc.requested_by,
        intended_action=desc.description or f"{operation} {path}",
        objects_affected=[f"file:{path}"],
        expected_changes=[f"{path} is {'removed' if operation == 'delete' else 'rewritten'}"],
        side_effects=["anything reading this path sees the change immediately"],
        failure_modes=["path missing at execution time", "permission denied"],
        rollback_procedure=rollback,
        rollback_validated=validated,
        tools_involved=["filesystem"],
    )

    async def _simulate(_spec: ActionSpec) -> dict:
        """Side-effect free: report what the operation would touch, right now."""
        if not path.exists():
            return {"ok": False, "detail": f"target does not exist: {path}"}
        size = path.stat().st_size
        return {
            "ok": True,
            "detail": f"would {operation} {path} ({size} bytes)",
            "blast_radius": {
                "paths": [str(path)],
                "bytes": size,
                "protected": _is_protected(path),
                "snapshot": str(snapshot.snapshot) if snapshot else None,
            },
        }

    def _execute():
        if operation == "delete":
            path.unlink()
            return {"deleted": str(path), "snapshot": str(snapshot.snapshot) if snapshot else None}
        content = desc.params.get("content", "")
        path.write_text(content, encoding="utf-8")
        return {"written": str(path), "bytes": len(content),
                "snapshot": str(snapshot.snapshot) if snapshot else None}

    return spec, _simulate, _execute


async def _simulate_calendar(spec: ActionSpec) -> dict:
    return {
        "ok": True,
        "detail": f"validated; would apply to {spec.objects_affected}",
        "blast_radius": {"objects": spec.objects_affected},
    }


def _calendar_adapter(desc: ActionDescriptor):
    from actions import calendar_dav

    start = desc.params.get("start", "")
    risk = _max_risk("medium", desc.min_risk)

    spec = ActionSpec(
        kind="calendar_change",
        risk=risk,
        requested_by=desc.requested_by,
        intended_action=desc.description or f"Create calendar event '{desc.target}' at {start}",
        objects_affected=[f"calendar_event:{desc.target}@{start}"],
        expected_changes=["one event added to the local CalDAV calendar"],
        side_effects=["the event appears on every device syncing this calendar"],
        failure_modes=["CalDAV server unreachable"],
        rollback_procedure="delete the created event via DELETE /api/v1/calendar/dav/event/{uid}",
        tools_involved=["caldav"],
    )

    async def _execute():
        return await calendar_dav.create_event(
            summary=desc.target,
            start=start,
            end=desc.params.get("end"),
            description=desc.params.get("description", ""),
            location=desc.params.get("location", ""),
            is_owner=bool(desc.params.get("is_owner", False)),
        )

    return spec, _simulate_calendar, _execute


def _command_adapter(desc: ActionDescriptor):
    command = desc.target
    derived, detail = classify_command_risk(command)
    risk = _max_risk(derived, desc.min_risk)

    async def _simulate(spec: ActionSpec) -> dict:
        # Re-grade at simulation time: the gates, not a cached verdict, decide.
        level, why = classify_command_risk(command)
        if level == "critical":
            return {"ok": False, "detail": why,
                    "blast_radius": {"command": command, "risk": level}}
        from agents.executor import RUN_DIR
        return {"ok": True, "detail": f"would run: {command} ({why})",
                "blast_radius": {"command": command, "risk": level,
                                 "cwd": RUN_DIR, "network": False}}

    spec = ActionSpec(
        kind="shell_command",
        risk=risk,
        requested_by=desc.requested_by,
        intended_action=desc.description or f"run shell command: {command}",
        objects_affected=[f"command:{command}"],
        expected_changes=["whatever the command does in its sandboxed working directory"],
        side_effects=["command output is returned to the caller"],
        failure_modes=["non-zero exit", "timeout"],
        rollback_procedure="none — shell commands are not automatically reversible",
        tools_involved=["shell"],
    )

    async def _execute():
        from agents.executor import _run_command

        output, code = await _run_command(command)
        return {"command": command, "exit_code": code, "output": output}

    return spec, _simulate, _execute


register_adapter("file_change", _file_adapter)
register_adapter("calendar_change", _calendar_adapter)
register_adapter("shell_command", _command_adapter)


# ── The guard ────────────────────────────────────────────────────────────────

def build(descriptor: ActionDescriptor):
    """Resolve a descriptor to ``(spec, simulator, execute)`` via its adapter."""
    adapter = _ADAPTERS.get(descriptor.type)
    if adapter is None:
        raise ValueError(f"no adapter registered for action type {descriptor.type!r}")
    return adapter(descriptor)


async def preview_action(descriptor: ActionDescriptor) -> dict[str, Any]:
    """Simulate ONLY: blast radius, derived risk, and the approval it would need.

    Executes nothing and stages nothing — this is the "show the blast radius"
    call. Critical actions are reported as blocked here too, before any token
    could exist.
    """
    from trust.proof_card import build_proof_card, summarise_proof_card

    spec, simulator, _execute = build(descriptor)
    try:
        result = await simulator(spec)
        result.setdefault("ok", False)
    except Exception as exc:  # noqa: BLE001 — a crashing simulator is a failed simulation
        result = {"ok": False, "detail": f"simulator raised: {exc}"}
    spec.simulation = result

    problems = spec.problems()
    card = build_proof_card(spec)
    return {
        "action_id": spec.action_id,
        "would_execute": not problems,
        "risk": spec.risk,
        "approval_required": APPROVAL_BY_RISK[spec.risk],
        "blocked": bool(problems),
        "blocked_reasons": problems,
        "simulation": result,
        "blast_radius": result.get("blast_radius", {}),
        "proof_card": card,
        "proof_card_summary": summarise_proof_card(card),
        # Explicit, so no caller can mistake a preview for an execution.
        "executed": False,
    }


async def guard_action(
    descriptor: ActionDescriptor,
    *,
    is_owner: bool,
    confirm_token: Optional[str] = None,
    approval_artifact: Optional[str] = None,
    task_id: Optional[str] = None,
    principal: Any = None,
) -> dict[str, Any]:
    """Run the full guarded pipeline for one action.

    Without ``confirm_token``: simulate and stage, returning the token and the
    Proof Card. With one: gate, execute, record to the Trust Ledger, and return
    the Proof Card. Critical actions are blocked at both points.
    """
    from actions.gated_execution import confirm_action, stage_action
    from trust.lockdown_enforcement import lockdown_refusal
    from trust.proof_card import summarise_proof_card

    spec, simulator, execute = build(descriptor)

    # P14-R1, FINDING AB. Lockdown was an alarm with no enforcement point for
    # two phases: `initiate_lockdown()` wrote a CRITICAL event and alerted the
    # owner, and nothing consulted the state it set. THIS is the consumer.
    #
    # It sits before BOTH branches deliberately. Refusing only at confirm would
    # let a lockdown-time action stage, mint a token and produce a Proof Card —
    # leaving a staged action to be confirmed the moment lockdown lifts, which
    # is the opposite of containment.
    refusal = await lockdown_refusal(
        risk=spec.risk, action_type=descriptor.type, is_owner=is_owner)
    if refusal is not None:
        return refusal

    if not confirm_token:
        outcome = await stage_action(
            spec, is_owner=is_owner, execute=execute, simulator=simulator,
            principal=principal, governed=descriptor.governed,
        )
    else:
        outcome = await confirm_action(
            spec,
            token=confirm_token,
            approval_artifact=approval_artifact or APPROVAL_BY_RISK[spec.risk],
            task_id=task_id,
            simulator=simulator,
            principal=principal, governed=descriptor.governed,
        )

    outcome.setdefault("risk", spec.risk)
    outcome.setdefault("approval_required", APPROVAL_BY_RISK[spec.risk])
    if "proof_card" in outcome:
        outcome["proof_card_summary"] = summarise_proof_card(outcome["proof_card"])
    return outcome


__all__ = [
    "ActionDescriptor",
    "SNAPSHOT_DIRNAME",
    "register_adapter",
    "build",
    "preview_action",
    "guard_action",
    "classify_file_risk",
    "classify_command_risk",
]
