"""
armory/gatekeeper.py — the single chokepoint in front of every security tool.

Nothing runs a tool except through :class:`ArmoryGatekeeper`. It composes the
vault, the unlock session, the whitelist, Shadow World, the Docker manager, and
the Trust Ledger into one fail-closed pipeline:

    unlock(master_key)          verify PBKDF2 hash → decrypt vault to RAM → latch
    execute(tool_id, args)      locked? → prompt for key
                                not owner? → refuse
                                target off whitelist? → refuse
                                external egress w/ IRA_ALLOW_EXTERNAL_API=false? → refuse
                                risk critical? → block;  risk high? → require approval
                                Shadow World preview (log) → run in throwaway
                                container → Trust Ledger records → return output
                                → auto-lock after the task (and after idle TTL)

Every decision defaults to *deny*. Ledger writes are fail-soft (a momentarily
down DB never loses a legitimate action) but reported honestly. The gatekeeper
holds ONE process-wide session so the API and the agent share unlock state.
"""
from __future__ import annotations

import ipaddress
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

from armory import vault as vaultmod
from armory import vault_encryption as venc
from armory.docker_manager import DockerUnavailable, ToolResult
from armory.executors import Executor, SandboxUnavailable, select_executor
from armory.manifest import ManifestError, resolve_command, target_args
from armory.session import DEFAULT_TTL_SECONDS, VaultSession
from armory.whitelist import REFUSAL as WHITELIST_REFUSAL
from armory.whitelist import extract_host

logger = logging.getLogger("ira.armory.gatekeeper")

LOCKED_MESSAGE = "🔒 Armory is locked. Please enter the Master Key to unlock Security Tools."
DISABLED_MESSAGE = ("🚫 The Armory is disabled on this deployment. Set ARMORY_ENABLED=true "
                    "and restart IRA to use security tools.")
NOT_PROVISIONED = ("🔒 The Armory has not been set up on this machine yet. "
                   "Run `python -m scripts.setup_vault` to create the vault.")
NOT_OWNER = "Security tools are restricted to the verified owner."


class ArmoryGatekeeper:
    def __init__(self, *, vault_dir: str, master_key_hash: str,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 executor: Optional[Executor] = None,
                 execution_backend: str = "auto",
                 docker_enabled: bool = True,
                 allow_unsandboxed: bool = False,
                 allow_external_api: bool = False,
                 require_totp_for_high_risk: bool = True,
                 totp_verify: Optional[Callable[[str, str], Awaitable[bool]]] = None,
                 totp_enrolled: Optional[Callable[[str], Awaitable[bool]]] = None,
                 enabled: bool = True,
                 max_unlock_attempts: int = 5,
                 lockout_seconds: int = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.vault_dir = vault_dir
        self.master_key_hash = master_key_hash or ""
        self.allow_external_api = allow_external_api
        # ARMORY_ENABLED master switch: when off, every entry point refuses.
        self.enabled = enabled
        # High-risk tools need a real 2FA (TOTP) code by default; the verifier is
        # injectable so the gate is testable without a database.
        self.require_totp_for_high_risk = require_totp_for_high_risk
        self._totp_verify_fn = totp_verify
        self._totp_enrolled_fn = totp_enrolled
        # Master-Key brute-force backoff (in-process, single-owner).
        self._max_unlock_attempts = max(1, int(max_unlock_attempts))
        self._lockout_seconds = max(1, int(lockout_seconds))
        self._clock = clock
        self._failed_unlocks = 0
        self._locked_out_until = 0.0
        self.session = VaultSession(ttl_seconds=ttl_seconds)
        # The executor is any backend implementing the Executor protocol: Docker,
        # a namespace sandbox (firejail/bubblewrap), or opt-in unsandboxed local.
        self.executor = executor or select_executor(
            backend=execution_backend, docker_enabled=docker_enabled,
            allow_unsandboxed=allow_unsandboxed)

    # ── construction from settings ────────────────────────────────────────────

    @classmethod
    def from_settings(cls) -> "ArmoryGatekeeper":
        from config import get_settings
        cfg = get_settings()
        return cls(
            vault_dir=cfg.armory_vault_dir,
            master_key_hash=cfg.armory_master_key_hash,
            ttl_seconds=cfg.armory_session_ttl_seconds,
            execution_backend=cfg.armory_execution_backend,
            docker_enabled=cfg.armory_docker_enabled,
            allow_unsandboxed=cfg.armory_allow_unsandboxed,
            allow_external_api=cfg.ira_allow_external_api,
            require_totp_for_high_risk=cfg.armory_require_totp_for_high_risk,
            enabled=cfg.armory_enabled,
            max_unlock_attempts=cfg.armory_max_unlock_attempts,
            lockout_seconds=cfg.armory_unlock_lockout_seconds,
        )

    # ── state surface ─────────────────────────────────────────────────────────

    def is_provisioned(self) -> bool:
        return bool(self.master_key_hash) and vaultmod.exists(self.vault_dir)

    def _lockout_remaining(self) -> int:
        return max(0, int(self._locked_out_until - self._clock()))

    def status(self) -> dict:
        unlocked = self.session.is_unlocked()
        out = {
            "enabled": self.enabled,
            "provisioned": self.is_provisioned(),
            "unlocked": unlocked,
            "seconds_remaining": self.session.seconds_remaining(),
            "external_egress_allowed": self.allow_external_api,
            "execution_backend": getattr(self.executor, "name", "unknown"),
            "locked_out": self._lockout_remaining() > 0,
            "lockout_seconds_remaining": self._lockout_remaining(),
        }
        if unlocked:
            out["tools_available"] = len(self.session.contents().manifest)
        return out

    # ── unlock / lock ─────────────────────────────────────────────────────────

    async def unlock(self, master_key: str, *, user_id: str = "owner") -> dict:
        """Verify the master key, decrypt the vault to RAM, and latch unlocked.

        Fail-closed: the master switch, a missing verifier/vault, a wrong key, or a
        corrupt vault all leave the Armory locked. Repeated wrong keys trip a
        time-based lockout (brute-force backoff). The plaintext key is never stored.
        """
        if not self.enabled:
            return {"status": "disabled", "message": DISABLED_MESSAGE}
        if not self.is_provisioned():
            return {"status": "not_provisioned", "message": NOT_PROVISIONED}

        # Brute-force backoff: refuse while locked out.
        remaining = self._lockout_remaining()
        if remaining > 0:
            await self._log("security_event", user_id,
                            {"event": "armory_unlock_locked_out", "seconds_remaining": remaining})
            return {"status": "locked_out",
                    "message": (f"🚫 Too many failed attempts. Try again in {remaining}s."),
                    "seconds_remaining": remaining}

        if not venc.verify_master_key(master_key, self.master_key_hash):
            self._failed_unlocks += 1
            record = {"event": "armory_unlock_failed", "reason": "bad_master_key",
                      "failed_attempts": self._failed_unlocks}
            if self._failed_unlocks >= self._max_unlock_attempts:
                self._locked_out_until = self._clock() + self._lockout_seconds
                self._failed_unlocks = 0
                record["locked_out_for"] = self._lockout_seconds
                await self._log("security_event", user_id, record)
                return {"status": "locked_out",
                        "message": (f"🚫 Too many failed attempts. Armory locked for "
                                    f"{self._lockout_seconds}s."),
                        "seconds_remaining": self._lockout_seconds}
            await self._log("security_event", user_id, record)
            attempts_left = self._max_unlock_attempts - self._failed_unlocks
            return {"status": "denied",
                    "message": (f"❌ Incorrect Master Key. Armory stays locked "
                                f"({attempts_left} attempt(s) left).")}
        try:
            contents = vaultmod.open(self.vault_dir, master_key)
        except vaultmod.VaultError as exc:
            await self._log("security_event", user_id,
                            {"event": "armory_unlock_failed", "reason": str(exc)})
            return {"status": "error", "message": f"🔒 Could not open the vault: {exc}"}
        # Success clears the failure counter.
        self._failed_unlocks = 0
        self.session.unlock(contents)
        await self._log("verification", user_id,
                        {"event": "armory_unlocked", "tools": len(contents.manifest)})
        return {"status": "unlocked",
                "message": "✅ Armory Unlocked.",
                "tools_available": len(contents.manifest),
                "seconds_remaining": self.session.seconds_remaining()}

    async def lock(self, *, user_id: str = "owner", reason: str = "manual") -> dict:
        was_unlocked = self.session.is_unlocked()
        self.session.lock()
        if was_unlocked:
            await self._log("security_event", user_id,
                            {"event": "armory_locked", "reason": reason})
        return {"status": "locked", "message": "🔒 Armory locked."}

    # ── preview (Shadow World) ────────────────────────────────────────────────

    async def preview(self, tool_id: str, args: dict[str, str], *,
                      user_id: str = "owner") -> dict:
        """Simulate a tool via Shadow World without running anything."""
        if not self.enabled:
            return {"status": "disabled", "message": DISABLED_MESSAGE}
        if not self.session.is_unlocked():
            return {"status": "locked", "message": LOCKED_MESSAGE}
        self.session.touch()
        try:
            tool = self.session.contents().manifest.get(tool_id)
            argv = resolve_command(tool, args)
        except ManifestError as exc:
            return {"status": "invalid", "message": f"❌ {exc}"}
        sim = await self._shadow_preview(tool, argv, args)
        return {"status": "preview", "tool": tool_id, "risk": tool.risk,
                "network": tool.network, "command": argv, "simulation": sim}

    # ── execute ───────────────────────────────────────────────────────────────

    async def execute(self, tool_id: str, args: dict[str, str], *,
                      user_id: str = "owner", is_owner: bool = False,
                      approval_code: Optional[str] = None,
                      approve_high_risk: bool = False,
                      lock_after: bool = True) -> dict:
        """Run a tool through every gate. Returns a result dict (never raises out)."""
        # Gate 0a — master switch.
        if not self.enabled:
            return {"status": "disabled", "message": DISABLED_MESSAGE}
        # Gate 0b — owner only.
        if not is_owner:
            return {"status": "forbidden", "message": NOT_OWNER}

        # Gate 1 — vault must be unlocked.
        if not self.session.is_unlocked():
            return {"status": "locked", "message": LOCKED_MESSAGE}
        self.session.touch()
        contents = self.session.contents()

        # Gate 2 — tool must exist and arguments must resolve safely.
        try:
            tool = contents.manifest.get(tool_id)
            argv = resolve_command(tool, args)
        except ManifestError as exc:
            return {"status": "invalid", "message": f"❌ {exc}"}

        # Gate 2b — lockdown. After the tool is resolved, because an UNKNOWN
        # lockdown state refuses by the tool's own risk; an ACTIVE one refuses
        # every tool. Before anything is simulated, logged as run or executed.
        from trust.lockdown_enforcement import lockdown_refusal

        refusal = await lockdown_refusal(risk=tool.risk, action_type=f"armory:{tool_id}",
                                         is_owner=is_owner)
        if refusal is not None:
            return refusal

        # Gate 3 — every target must be in the authorized whitelist.
        targets = target_args(tool, args)
        for target in targets:
            if not contents.whitelist.authorizes(target):
                await self._log("security_event", user_id,
                                {"event": "armory_target_blocked", "tool": tool_id,
                                 "target": extract_host(target)})
                return {"status": "out_of_scope", "message": WHITELIST_REFUSAL}

        # Gate 4 — external egress honours IRA_ALLOW_EXTERNAL_API. When external
        # access is off, only local/private targets may be reached (vault tools
        # run locally). network=none tools never egress and are unaffected.
        if not self.allow_external_api:
            external = [t for t in targets if _is_external_target(t)]
            if external:
                await self._log("security_event", user_id,
                                {"event": "armory_egress_blocked", "tool": tool_id,
                                 "targets": [extract_host(t) for t in external]})
                return {"status": "egress_blocked",
                        "message": ("🚫 External targets are blocked while "
                                    "IRA_ALLOW_EXTERNAL_API=false. Only local/private "
                                    "targets are permitted. Enable external access to "
                                    "scan public hosts you are authorized to test.")}

        # Gate 5 — risk. Critical is blocked outright; high needs explicit approval.
        if tool.risk == "critical":
            await self._log("security_event", user_id,
                            {"event": "armory_critical_blocked", "tool": tool_id})
            return {"status": "blocked",
                    "message": "🚫 This tool is classified critical and cannot be run."}
        approval_method = "auto"
        if tool.risk == "high":
            gate = await self._high_risk_gate(user_id, tool_id, approval_code, approve_high_risk)
            if gate["decision"] != "approved":
                return gate["response"]
            approval_method = gate["method"]

        # Shadow World preview BEFORE anything real happens (logged for the record).
        action_id = uuid.uuid4().hex
        sim = await self._shadow_preview(tool, argv, args)
        await self._log("action", user_id, {
            "event": "armory_tool", "tool": tool_id, "risk": tool.risk,
            "network": tool.network, "targets": [extract_host(t) for t in targets],
            "simulation_ok": bool(sim.get("ok")),
        }, action_id=action_id)
        if tool.risk == "high":
            await self._log("approval", user_id,
                            {"event": "armory_high_risk_approved", "tool": tool_id,
                             "method": approval_method},
                            action_id=action_id)

        # Execute in the selected sandbox (Docker / firejail / bubblewrap / local).
        try:
            result: ToolResult = await self.executor.run_tool(tool, argv)
        except (DockerUnavailable, SandboxUnavailable) as exc:
            await self._log("security_event", user_id,
                            {"event": "armory_exec_failed", "tool": tool_id, "reason": str(exc)},
                            action_id=action_id)
            if lock_after:
                await self.lock(user_id=user_id, reason="task_complete")
            return {"status": "error", "message": f"🚫 {exc}"}

        # Record ONLY that the tool ran (never the hacking output) to the Ledger.
        await self._log("execution", user_id, {
            "event": "armory_tool_ran", "tool": tool_id,
            "exit_code": result.exit_code, "timed_out": result.timed_out,
        }, action_id=action_id)

        if lock_after:
            await self.lock(user_id=user_id, reason="task_complete")

        return {"status": "executed", "tool": tool_id, "risk": tool.risk,
                "network": tool.network, "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "output": result.output,  # returned to the owner, not persisted
                "action_id": action_id,
                "locked": lock_after}

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _totp_enrolled(self, user_id: str) -> bool:
        if self._totp_enrolled_fn is not None:
            return await self._totp_enrolled_fn(user_id)
        from security import totp
        return await totp.is_enrolled(user_id)

    async def _totp_verify(self, user_id: str, code: str) -> bool:
        if self._totp_verify_fn is not None:
            return await self._totp_verify_fn(user_id, code)
        from security import totp
        return await totp.verify(user_id, code)

    async def _high_risk_gate(self, user_id: str, tool_id: str,
                              approval_code: Optional[str], approve_flag: bool) -> dict:
        """Decide whether a high-risk tool may run. Returns
        {"decision": "approved", "method": ...} or
        {"decision": "denied", "response": <dict>}. Fail-closed throughout."""
        # Legacy/opt-out path: a simple explicit confirmation flag.
        if not self.require_totp_for_high_risk:
            if approve_flag:
                return {"decision": "approved", "method": "confirm"}
            return {"decision": "denied", "response": {
                "status": "approval_required", "risk": "high", "tool": tool_id,
                "message": "⚠️ This is a HIGH-risk tool. Confirm to proceed."}}

        # 2FA path (default): require a valid current TOTP code.
        if not await self._totp_enrolled(user_id):
            await self._log("security_event", user_id,
                            {"event": "armory_high_risk_no_2fa", "tool": tool_id})
            return {"decision": "denied", "response": {
                "status": "approval_unavailable", "risk": "high", "tool": tool_id,
                "message": ("🔐 High-risk tools require 2FA. Enrol TOTP first "
                            "(POST /auth/totp/enroll), then retry with your code.")}}
        if not approval_code:
            return {"decision": "denied", "response": {
                "status": "approval_required", "risk": "high", "tool": tool_id,
                "message": ("⚠️ This is a HIGH-risk tool. Enter your 6-digit "
                            "authenticator (2FA) code to approve.")}}
        if not await self._totp_verify(user_id, approval_code):
            await self._log("security_event", user_id,
                            {"event": "armory_2fa_rejected", "tool": tool_id})
            return {"decision": "denied", "response": {
                "status": "approval_denied", "risk": "high", "tool": tool_id,
                "message": "❌ Invalid or expired 2FA code. Armory not run."}}
        return {"decision": "approved", "method": "totp"}

    async def _shadow_preview(self, tool, argv: list[str], args: dict[str, str]) -> dict:
        """Build a Shadow World ActionSpec and run its side-effect-free simulator."""
        try:
            from actions.shadow_world import ActionSpec, simulate
            _ensure_armory_simulator()
            spec = ActionSpec(
                kind="armory_tool",
                risk=tool.risk,
                requested_by="owner",
                intended_action=f"run armory tool {tool.id}: {' '.join(argv)}",
                objects_affected=[f"target:{extract_host(t)}" for t in target_args(tool, args)],
                tools_involved=[tool.id],
                data_leaving_device=(["network egress via Tor"] if tool.network != "none" else []),
                side_effects=["output returned to owner; not persisted"],
                failure_modes=["container/network error", "tool non-zero exit"],
                rollback_procedure="none — reconnaissance/read-only tools are not reversible",
            )
            # Carry the resolved argv/network on the spec so the simulator (which
            # only receives the spec) can report the exact blast radius.
            spec.simulation = None
            spec.__dict__["_argv"] = argv
            spec.__dict__["_network"] = tool.network
            return await simulate(spec)
        except Exception as exc:  # noqa: BLE001 — preview must never block the pipeline
            logger.debug("shadow preview unavailable: %s", exc)
            return {"ok": True, "detail": "shadow world unavailable; preview skipped",
                    "command": argv}

    async def _log(self, kind: str, user_id: str, record: dict,
                   *, action_id: Optional[str] = None) -> None:
        """Append to the Trust Ledger, fail-soft (DB down must not block a tool)."""
        try:
            from trust.ledger import append_record
            await append_record(kind, user_id=user_id,
                                action_id=action_id or uuid.uuid4().hex,
                                record=record)
        except Exception as exc:  # noqa: BLE001
            from trust.ledger import LedgerNotBound, LedgerTenantMismatch
            if isinstance(exc, (LedgerNotBound, LedgerTenantMismatch)):
                # A missing tenant context is our defect, not a flaky database,
                # and it will not clear on its own — see trust/ledger.py.
                logger.error("ARMORY LEDGER NOT WRITTEN — defect, not an "
                             "outage (%s): %s", kind, exc)
            else:
                logger.warning("armory ledger append failed (%s): %s", kind, exc)


# ── external-target classifier ────────────────────────────────────────────────

def _is_external_target(target: str) -> bool:
    """True if a target is a public/non-local host (subject to the egress gate).

    Local/loopback/private IPs and ``localhost`` are NOT external. A hostname that
    is not an IP is treated as external (conservative) unless it is a localhost
    alias.
    """
    host = extract_host(target)
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        # Not an IP → a domain name. Try the shared local-host check if available.
        try:
            from config import _is_local_or_private_host
            if _is_local_or_private_host(host):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True


# ── Shadow World simulator registration ───────────────────────────────────────

_simulator_registered = False


def _ensure_armory_simulator() -> None:
    """Register the (side-effect-free) armory_tool simulator once, on first use."""
    global _simulator_registered
    if _simulator_registered:
        return
    from actions.shadow_world import register_simulator

    async def _simulate_armory(spec) -> dict:
        argv = spec.__dict__.get("_argv", [])
        network = spec.__dict__.get("_network", "none")
        return {
            "ok": True,
            "detail": f"would run in an ephemeral container: {' '.join(argv)}",
            "blast_radius": {
                "command": argv,
                "network": network,
                "targets": spec.objects_affected,
                "persists_output": False,
                "container_lifetime": "single command (--rm)",
            },
        }

    register_simulator("armory_tool", _simulate_armory)
    _simulator_registered = True


# ── process-wide singleton ────────────────────────────────────────────────────

_gatekeeper: Optional[ArmoryGatekeeper] = None


def get_gatekeeper() -> ArmoryGatekeeper:
    """Return the shared gatekeeper (one unlock session per process)."""
    global _gatekeeper
    if _gatekeeper is None:
        _gatekeeper = ArmoryGatekeeper.from_settings()
    return _gatekeeper


def reset_gatekeeper() -> None:
    """Testing/hot-reload hook: drop the singleton (locks any open session)."""
    global _gatekeeper
    if _gatekeeper is not None:
        _gatekeeper.session.lock()
    _gatekeeper = None


__all__ = [
    "ArmoryGatekeeper", "get_gatekeeper", "reset_gatekeeper",
    "LOCKED_MESSAGE", "NOT_PROVISIONED", "NOT_OWNER",
]
