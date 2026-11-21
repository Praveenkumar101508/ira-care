"""P6.2 — Bounded automated security playbooks.

Exactly three automated security-response actions, invokable only through
run_playbook() which validates against a fixed ALLOWED_ACTIONS allowlist.

Actions:
  rotate_tokens(username)               Bump the per-account token version in
                                        PostgreSQL (migration 096), immediately
                                        invalidating every outstanding token for
                                        that account.

  block_ip(ip_address, ttl_seconds)     Add the IP to the Redis blocklist
                                        (ira:blocked_ip:{ip}) for ttl_seconds.
                                        The auth middleware checks this key on
                                        every request and returns 403 if set.

  snapshot_logs(label)                  Read recent security_events from the DB
                                        and write them to a timestamped JSON file
                                        in IRA_LOG_SNAPSHOT_DIR for forensics.

Safety invariants:
  - Only actions in ALLOWED_ACTIONS are executable — anything else is refused
    with a logged warning and no side-effect.
  - Every action call is written to security_events as 'playbook_action' (MEDIUM)
    before the action executes, creating an immutable audit trail.
  - All actions are fail-soft: infrastructure errors are logged as warnings and
    return False; no action ever raises to its caller.
  - No destructive operations: no `rm`, no `git`, no service kill, no DROP TABLE.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("ira.playbooks")

# ── Allowlist ─────────────────────────────────────────────────────────────────

ALLOWED_ACTIONS: frozenset[str] = frozenset({"rotate_tokens", "block_ip", "snapshot_logs"})

_BLOCKED_IP_PREFIX = "ira:blocked_ip:"
_DEFAULT_BLOCK_TTL = 3600          # 1 hour
_MAX_BLOCK_TTL = 86400 * 7         # 7 days cap
#: Forensic snapshots default under a world-writable directory, so the
#: directory is VERIFIED before it is written to — see `_secure_snapshot_dir`.
#: The path itself is not the defect; accepting whatever already sits at it is.
_DEFAULT_SNAPSHOT_DIR = "/tmp/ira-logs"


# ── Gatekeeper ────────────────────────────────────────────────────────────────

async def run_playbook(action: str, **kwargs: Any) -> bool:
    """Execute a bounded playbook action by name.

    Returns True on success, False on failure or unknown action.
    Unknown actions are logged as a WARNING and immediately refused.
    Every execution (successful or not) is written to security_events.
    """
    if action not in ALLOWED_ACTIONS:
        logger.warning(
            "run_playbook: refused unknown action %r (allowed: %s)",
            action, ", ".join(sorted(ALLOWED_ACTIONS)),
        )
        return False

    # Audit log BEFORE executing the action
    await _audit(action, kwargs, outcome="initiated")

    try:
        result = await _dispatch(action, **kwargs)
        await _audit(action, kwargs, outcome="success" if result else "failed")
        return result
    except Exception as exc:
        logger.warning("run_playbook: %r raised unexpectedly: %s", action, exc)
        await _audit(action, kwargs, outcome=f"error: {exc!s:.80}")
        return False


async def _dispatch(action: str, **kwargs: Any) -> bool:
    if action == "rotate_tokens":
        return await rotate_tokens(**kwargs)
    if action == "block_ip":
        return await block_ip(**kwargs)
    if action == "snapshot_logs":
        return await snapshot_logs(**kwargs)
    return False  # unreachable but satisfies type checker


async def _audit(action: str, params: dict, outcome: str) -> None:
    """Write one row to security_events as the playbook audit trail."""
    description = (
        f"Playbook '{action}' {outcome}. "
        f"Params: {json.dumps({k: str(v)[:60] for k, v in params.items()})}"
    )
    try:
        from utils.security_events import emit_event
        await emit_event("playbook_action", "medium", description=description[:1000])
    except Exception as exc:
        logger.warning("playbook audit write failed: %s", exc)


# ── Action: rotate_tokens ─────────────────────────────────────────────────────

async def rotate_tokens(username: str) -> bool:
    """Bump the per-account token version, invalidating every outstanding token.

    Through `core.identity.token_versions`, the one writer of the counter
    `require_auth` reads (migration 096), so tokens issued with an older `ver`
    are rejected on their next use.

    A failure **raises**. It used to increment Redis directly and turn any
    error into `False` with a warning, which `run_playbook` audited as a plain
    "failed": the reason was lost. Raised, `run_playbook` records it in the
    security audit trail as `error: ...`.
    """
    if not username:
        logger.warning("rotate_tokens: empty username — skipping")
        return False
    from core.identity import token_versions

    try:
        new_ver = await token_versions.bump_subject(username)
    except Exception as exc:
        logger.error("rotate_tokens: could not invalidate tokens for %r: %s", username, exc)
        raise
    logger.info("rotate_tokens: invalidated all tokens for %r (ver now %d)", username, new_ver)
    return True


# ── Action: block_ip ──────────────────────────────────────────────────────────

async def block_ip(ip_address: str, ttl_seconds: int = _DEFAULT_BLOCK_TTL) -> bool:
    """Add ip_address to the Redis blocklist for ttl_seconds.

    The key ira:blocked_ip:{ip} is checked by the auth middleware on every
    request; any request from a blocked IP receives 403 immediately.
    TTL is capped at 7 days to prevent indefinite blocks from automated logic.
    """
    if not ip_address:
        logger.warning("block_ip: empty ip_address — skipping")
        return False
    ttl = min(int(ttl_seconds), _MAX_BLOCK_TTL)
    try:
        from utils.redis_client import get_redis
        key = f"{_BLOCKED_IP_PREFIX}{ip_address}"
        await get_redis().setex(key, ttl, "1")
        logger.info("block_ip: %s blocked for %ds (key: %s)", ip_address, ttl, key)
        return True
    except Exception as exc:
        logger.warning("block_ip: Redis error for %s: %s", ip_address, exc)
        return False


async def is_ip_blocked(ip_address: str) -> bool:
    """Check whether ip_address is on the Redis blocklist. Fail-open on error."""
    if not ip_address:
        return False
    try:
        from utils.redis_client import get_redis
        return bool(await get_redis().exists(f"{_BLOCKED_IP_PREFIX}{ip_address}"))
    except Exception as exc:
        logger.warning("is_ip_blocked: Redis error (fail-open): %s", exc)
        return False


# ── Action: snapshot_logs ─────────────────────────────────────────────────────


def _secure_snapshot_dir(snapshot_dir: Path) -> str | None:
    """Create and VERIFY the snapshot directory; return why it is unusable, or None.

    P16-05 (SEC-03, B108). The old code was
    `mkdir(parents=True, exist_ok=True)` followed by `write_text`, under a
    default of `/tmp/ira-logs`. Every step of that is fine in isolation and
    the combination is not:

    * `/tmp` is world-writable, and the path is **predictable** — it is a
      constant in this file.
    * `exist_ok=True` accepts a directory somebody else already created, so a
      local user who wins the race owns the destination.
    * `mkdir` follows a symlink at the final component, and `write_text`
      follows one at the file, so the dump can be redirected anywhere the
      service can write.

    What lands there is the last 500 rows of `security_events` — source IPs,
    raw log lines, incident descriptions. The forensic record of an intrusion
    is exactly what an intruder wants to read and to edit.

    So the directory is checked rather than assumed: not a symlink, a real
    directory, owned by this process's uid, and not writable by group or
    other. Anything else **refuses** — a snapshot nobody can trust is worse
    than an absent one, because it will be read as evidence.
    """
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        return f"cannot create {snapshot_dir}: {exc}"

    try:
        st = os.lstat(snapshot_dir)
    except OSError as exc:
        return f"cannot stat {snapshot_dir}: {exc}"

    if stat.S_ISLNK(st.st_mode):
        return (f"{snapshot_dir} is a symlink. Refusing to follow it — whoever "
                f"created it, not this service, would be choosing where the "
                f"security events land.")
    if not stat.S_ISDIR(st.st_mode):
        return f"{snapshot_dir} exists and is not a directory"

    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return (f"{snapshot_dir} is owned by uid {st.st_uid}, not {getuid()}. "
                f"It was created by somebody else.")

    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return (f"{snapshot_dir} is group- or world-writable (mode "
                f"{stat.S_IMODE(st.st_mode):04o}). Anything written there can "
                f"be read and replaced by another local user.")
    return None


async def snapshot_logs(label: str = "manual") -> bool:
    """Dump recent security_events to a timestamped JSON file for forensics.

    Writes to IRA_LOG_SNAPSHOT_DIR (default /tmp/ira-logs). The file is
    named ira-security-snapshot-{label}-{timestamp}.json and contains the
    last 500 security events ordered newest-first.
    """
    snapshot_dir = Path(os.environ.get("IRA_LOG_SNAPSHOT_DIR", _DEFAULT_SNAPSHOT_DIR))
    unusable = _secure_snapshot_dir(snapshot_dir)
    if unusable is not None:
        logger.warning("snapshot_logs: refusing to write: %s", unusable)
        return False

    try:
        from utils.db import acquire
        async with acquire() as conn:
            rows = await conn.fetch(
                """SELECT severity, event_type, source_ip::text,
                          description, raw_log, resolved, created_at
                   FROM security_events
                   ORDER BY created_at DESC
                   LIMIT 500"""
            )
    except Exception as exc:
        logger.warning("snapshot_logs: DB read failed: %s", exc)
        return False

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    filename = snapshot_dir / f"ira-security-snapshot-{safe_label}-{ts}.json"

    payload = {
        "snapshot_label": label,
        "created_at": ts,
        "event_count": len(rows),
        "events": [
            {
                "severity": r["severity"],
                "event_type": r["event_type"],
                "source_ip": r["source_ip"],
                "description": r["description"],
                "raw_log": r["raw_log"],
                "resolved": r["resolved"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }

    # O_EXCL|O_NOFOLLOW, 0o600: refuse a file that already exists and refuse to
    # follow a symlink sitting at that name. The directory check above closes
    # the directory; this closes the file, and both are needed — a verified
    # directory can still be raced if it is ever made writable again.
    try:
        fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, default=str))
        logger.info("snapshot_logs: %d events written to %s", len(rows), filename)
        return True
    except OSError as exc:
        logger.warning("snapshot_logs: write failed: %s", exc)
        return False


__all__ = [
    "ALLOWED_ACTIONS",
    "run_playbook",
    "rotate_tokens",
    "block_ip",
    "snapshot_logs",
    "is_ip_blocked",
]
