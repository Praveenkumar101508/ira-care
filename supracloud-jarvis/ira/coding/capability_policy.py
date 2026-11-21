"""
coding/capability_policy.py — code-enforced capabilities for Forge workers.

Constitution B5: models propose actions; THIS module decides whether a tool
may execute them. A worker receives a CapabilityToken scoped to its task —
capabilities, a path sandbox, a network policy, and an expiry. Every check is
fail-closed: unknown capability → deny, path outside the sandbox → deny,
protected path → deny for writes regardless of the token (only an explicit
elevated approval recorded on the token can unlock a protected path, C4).

These tokens are plain in-process objects, not cryptographic credentials —
they constrain what OUR runtime will do on a worker's behalf, exactly like
utils/approval.py constrains actions. No stronger claim is made.

Protected paths come from docs/architecture/constitution.yaml when readable,
with a hard-coded fallback so policy never silently vanishes (fail-closed).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ira.coding.capability")


class Capability(str, Enum):
    READ_REPO = "read_repo"
    WRITE_WORKTREE = "write_worktree"
    RUN_COMMANDS = "run_commands"
    GIT_BRANCH = "git_branch"
    GIT_COMMIT = "git_commit"
    RUN_TESTS = "run_tests"
    INSTALL_PACKAGES = "install_packages"
    NETWORK_LOCAL = "network_local"
    NETWORK_EXTERNAL = "network_external"   # requires owner consent upstream (A1)


# Capabilities that can NEVER be granted to an implementation worker.
# There is deliberately no PUSH / MERGE / DELETE_BRANCH capability at all (C7).
FORBIDDEN_FOR_WORKERS = frozenset({Capability.NETWORK_EXTERNAL})

_FALLBACK_PROTECTED = (
    "supracloud-jarvis/ira/security/",
    "supracloud-jarvis/ira/utils/net_safety.py",
    "supracloud-jarvis/ira/utils/approval.py",
    "supracloud-jarvis/ira/utils/auto_implement.py",
    "supracloud-jarvis/ira/api/middleware/",
    "docs/architecture/ARCHITECTURE_CONSTITUTION.md",
    "docs/architecture/constitution.yaml",
    ".github/workflows/",
    ".gitignore",
)


def load_protected_paths(repo_root: Path | str | None = None) -> tuple[str, ...]:
    """Protected paths from constitution.yaml, else the built-in fallback."""
    if repo_root:
        cfg = Path(repo_root) / "docs" / "architecture" / "constitution.yaml"
        try:
            import yaml  # available via existing utils/yaml_config.py dependency
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            paths = tuple(str(p) for p in data.get("protected_paths", ()))
            if paths:
                return paths
        except Exception as exc:  # fail-closed: fall back, never return empty
            logger.warning("constitution.yaml unreadable (%s); using fallback", exc)
    return _FALLBACK_PROTECTED


@dataclass(frozen=True)
class CapabilityToken:
    """What one worker may do for one task. Immutable once issued."""
    task_id: str
    worker_id: str
    capabilities: frozenset[Capability]
    allowed_roots: tuple[str, ...]            # absolute paths the worker may touch
    protected_paths: tuple[str, ...]          # repo-relative prefixes, write-denied
    elevated_protected_approval: Optional[str] = None  # approver name, unlocks C4
    network: str = "none"                     # none | local
    expires_at: float = field(default_factory=lambda: time.time() + 4 * 3600)

    def expired(self) -> bool:
        return time.time() >= self.expires_at


def issue_token(
    task_id: str,
    worker_id: str,
    capabilities: set[Capability] | frozenset[Capability],
    allowed_roots: list[Path] | list[str],
    *,
    repo_root: Path | str | None = None,
    network: str = "none",
    ttl_seconds: float = 4 * 3600,
    elevated_protected_approval: Optional[str] = None,
) -> CapabilityToken:
    """Issue a worker token. Refuses forbidden capabilities outright."""
    caps = frozenset(Capability(c) for c in capabilities)
    bad = caps & FORBIDDEN_FOR_WORKERS
    if bad:
        raise PermissionError(f"capabilities never grantable to workers: {sorted(c.value for c in bad)}")
    if network not in ("none", "local"):
        raise PermissionError(f"invalid worker network policy: {network!r}")
    roots = tuple(str(Path(r).resolve()) for r in allowed_roots)
    if not roots:
        raise PermissionError("a token must have at least one allowed root")
    return CapabilityToken(
        task_id=task_id, worker_id=worker_id, capabilities=caps,
        allowed_roots=roots, protected_paths=load_protected_paths(repo_root),
        network=network, expires_at=time.time() + ttl_seconds,
        elevated_protected_approval=elevated_protected_approval,
    )


def _within(path: Path, root: str) -> bool:
    try:
        path.resolve().relative_to(Path(root))
        return True
    except ValueError:
        return False


def _is_protected(rel_posix: str, protected: tuple[str, ...]) -> bool:
    for p in protected:
        p_norm = p.replace("\\", "/")
        if p_norm.endswith("/"):
            if rel_posix.startswith(p_norm) or ("/" + p_norm) in ("/" + rel_posix):
                return True
            # match the prefix anywhere relative to a worktree root
            if rel_posix.startswith(p_norm.rstrip("/") + "/"):
                return True
        elif rel_posix == p_norm or rel_posix.endswith("/" + p_norm):
            return True
        elif rel_posix.startswith(p_norm.rstrip("/") + "/"):
            return True
    return False


def check(
    token: CapabilityToken,
    capability: Capability | str,
    *,
    path: Path | str | None = None,
) -> tuple[bool, str]:
    """May this token exercise `capability` (optionally on `path`)?
    Returns (allowed, reason). Fail-closed on anything unexpected."""
    try:
        capability = Capability(capability)
    except ValueError:
        return False, f"unknown capability {capability!r}"
    if token.expired():
        return False, "token expired"
    if capability not in token.capabilities:
        return False, f"capability {capability.value} not granted"

    if path is not None:
        p = Path(path)
        if not p.is_absolute():
            return False, "path checks require absolute paths"
        root = next((r for r in token.allowed_roots if _within(p, r)), None)
        if root is None:
            return False, f"path outside allowed roots: {p}"
        # C4: writes to protected paths need explicit elevated approval.
        if capability in (Capability.WRITE_WORKTREE, Capability.GIT_COMMIT):
            rel = p.resolve().relative_to(Path(root)).as_posix()
            if _is_protected(rel, token.protected_paths):
                if token.elevated_protected_approval:
                    return True, f"protected path allowed by elevated approval ({token.elevated_protected_approval})"
                return False, f"protected path requires elevated approval: {rel}"
    return True, "ok"


__all__ = [
    "Capability", "CapabilityToken", "FORBIDDEN_FOR_WORKERS",
    "issue_token", "check", "load_protected_paths",
]
