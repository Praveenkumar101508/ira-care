"""
IRA Security Guardian — Continuous monitoring daemon.

Monitors every 60 seconds:
  1. Nginx access logs     — brute-force, scanner, injection, anomaly detection
  2. security_events table — alerts on new CRITICAL/HIGH events
  3. System metrics        — CPU/memory spikes, disk pressure

Alert thresholds (configurable via env):
  >10 failed auth from one IP in 5 min   → HIGH alert
  >3  critical security events unresolved → CRITICAL alert
  >50 requests/min from one IP            → HIGH alert (rate abuse)
  Any SQL injection / XSS pattern detected → CRITICAL alert

All events are written to the security_events table and delivered
via the notifier (Telegram, WebSocket, Email).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from enum import Enum

import psutil

from config import get_settings
from utils.db import acquire
from utils.security_alerts import send_alert
from worker.notifier import notify

logger = logging.getLogger("ira.security")

# Fix P7: track whether the "SSH monitoring inactive" warning has been logged so
# we emit it once per process lifetime, not on every 60-second scan cycle.
_ssh_inactive_warned: bool = False

# Fix P13: cap log reads to prevent OOM when offset resets to 0 on a busy server.
# 50_000 lines ≈ 50MB of typical nginx log. Offset advances to the read stop point
# so the next cycle continues from there — no lines are dropped or double-counted.
_MAX_LINES_PER_SCAN: int = 50_000

# ── Attack pattern regexes ────────────────────────────────────────────────────
_SQLI_PATTERN = re.compile(
    r"(?:union\s+select|drop\s+table|insert\s+into|or\s+1=1|'--|\bxp_|\bexec\b|"
    r"information_schema|sleep\(\d+\)|benchmark\(|load_file\()",
    re.I,
)
_XSS_PATTERN = re.compile(
    r"(?:<script|javascript:|on(?:load|error|click|mouse)\s*=|eval\s*\(|alert\s*\(|"
    r"document\.cookie|<iframe|<svg.*?on\w+)",
    re.I,
)
# Fix #70: removed python-requests/, go-http-client, curl/\d, wget/\d — these
# match huge swaths of legitimate traffic (Python services, k8s probes, CI
# scripts) and produce noisy false positives. Only named security tools are kept.
_SCANNER_UA = re.compile(
    r"(?:nmap|nikto|sqlmap|dirbuster|masscan|zgrab|nuclei|burpsuite)",
    re.I,
)
_PATH_TRAVERSAL = re.compile(r"\.\./|%2e%2e/|%252e", re.I)

# Fix L16: log paths removed from module level — read lazily via get_settings()
# (same os.getenv() at import-time anti-pattern as Fix #58).

_SSH_FAIL_RE = re.compile(
    r"Failed password for (?:invalid user )?(\S+) from ([\d.]+) port \d+ ssh",
    re.I,
)
_SSH_INVALID_RE = re.compile(
    r"Invalid user (\S+) from ([\d.]+)",
    re.I,
)


# ── Log analysis ──────────────────────────────────────────────────────────────

def _parse_nginx_line(line: str) -> dict | None:
    """Parse one nginx JSON access log line."""
    import json
    try:
        return json.loads(line.strip())
    except Exception:
        return None


def _analyse_lines(lines: list[str]) -> list[dict]:
    """
    Analyse a batch of nginx log lines.
    Returns list of security events to record.

    P13-01 finding W. `_parse_nginx_line` returns None for a line it cannot
    read, and skipping it here is right — that IS the "is this a JSON line"
    test. What was missing is that nobody counted the skips.

    This parser assumes nginx's JSON log format. Measured against 30 lines of
    the same brute-force traffic: 1 event in JSON, **0 in combined format**,
    silently. And `_read_new_log_lines` advances `nginx_log_offset` to
    `f.tell()` whether or not anything was understood, so the unparsed lines
    are consumed and never revisited — the same destructive bookkeeping as
    finding T.

    A security monitor reporting zero events over a log it could not read is
    the worst possible version of a quiet system. The skip is unchanged; the
    silence is not.
    """
    ip_requests: dict[str, list] = defaultdict(list)
    ip_failures: dict[str, int] = defaultdict(int)
    events: list[dict] = []
    unparsed = 0

    for line in lines:
        entry = _parse_nginx_line(line)
        if not entry:
            unparsed += 1
            continue

        ip = entry.get("remote_addr", "unknown")
        uri = entry.get("uri", "")
        ua = entry.get("http_user_agent", "")
        status = str(entry.get("status", "200"))
        ip_requests[ip].append(entry)

        # Track auth failures
        if status in ("401", "403"):
            ip_failures[ip] += 1

        # SQL injection
        if _SQLI_PATTERN.search(uri):
            events.append({
                "severity": "critical",
                "event_type": "sql_injection_attempt",
                "source_ip": ip,
                "description": f"SQL injection pattern in URI from {ip}: {uri[:100]}",
                "raw_log": line[:500],
            })

        # XSS
        elif _XSS_PATTERN.search(uri):
            events.append({
                "severity": "high",
                "event_type": "xss_attempt",
                "source_ip": ip,
                "description": f"XSS pattern detected from {ip}: {uri[:100]}",
                "raw_log": line[:500],
            })

        # Path traversal
        elif _PATH_TRAVERSAL.search(uri):
            events.append({
                "severity": "high",
                "event_type": "path_traversal",
                "source_ip": ip,
                "description": f"Path traversal attempt from {ip}: {uri[:100]}",
                "raw_log": line[:500],
            })

        # Known scanner user-agent
        elif _SCANNER_UA.search(ua):
            events.append({
                "severity": "medium",
                "event_type": "scanner_detected",
                "source_ip": ip,
                "description": f"Known scanner tool detected from {ip}: {ua[:80]}",
                "raw_log": line[:500],
            })

    # Brute-force detection
    for ip, failures in ip_failures.items():
        if failures >= 10:
            events.append({
                "severity": "high",
                "event_type": "brute_force",
                "source_ip": ip,
                "description": f"Brute-force detected: {failures} auth failures from {ip}",
                "raw_log": "",
            })

    # Rate abuse (>50 requests from one IP in this batch)
    for ip, reqs in ip_requests.items():
        if len(reqs) > 50:
            events.append({
                "severity": "medium",
                "event_type": "rate_abuse",
                "source_ip": ip,
                "description": f"Rate abuse: {len(reqs)} requests from {ip} in one scan window",
                "raw_log": "",
            })

    # P13-01 finding W. Two distinct messages, because the two states need
    # different responses: NONE understood is a format mismatch — the parser and
    # the log have diverged, and this scan analysed nothing at all. SOME
    # unparsed is ordinary noise in a busy log until it is not, so it is a
    # warning with the ratio rather than an error.
    if lines and unparsed == len(lines):
        logger.error(
            "security monitor read %d nginx line(s) and could parse NONE of "
            "them. This parser expects nginx's JSON log format; the log is "
            "evidently something else. NO events were analysed this scan, and "
            "the read offset has already advanced past these lines. First "
            "line: %.200r", len(lines), lines[0])
    elif unparsed:
        logger.warning(
            "security monitor skipped %d of %d unparseable nginx line(s); "
            "those requests were not analysed", unparsed, len(lines))

    return events


#: P14-04 FINDING AE. See `_resolve_offset`.
class OffsetOrigin(str, Enum):
    """Where a log-reading offset came from. Not a detail — see FINDING AE."""

    #: A stored offset that is still valid for this file.
    STORED = "stored"
    #: No stored offset. First run.
    COLD_START = "cold_start"
    #: The file this offset described is gone: a different inode, or smaller
    #: than the offset. Reading resumes at 0.
    ROTATED = "rotated"
    #: A stored value that would not parse. Reading resumes at 0.
    CORRUPT = "corrupt"


def _parse_offset(value: object) -> tuple[int, int | None] | None:
    """`(offset, inode)` from a stored value, or None if it will not parse.

    Two formats are accepted. The current one is JSON carrying the inode; the
    original is a bare integer, which every existing deployment has. The bare
    form is read as inode-unknown rather than rejected — a migration that
    invalidated every deployment's offset would re-read every log from the
    start on upgrade, which is a self-inflicted alert storm.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                return int(data["offset"]), (
                    int(data["inode"]) if data.get("inode") is not None else None)
            except Exception:
                return None
        try:
            return int(text), None
        except ValueError:
            return None
    return None


def _resolve_offset(path: str, stored: object, *, key: str) -> tuple[int, OffsetOrigin]:
    """Where to start reading, and why. **FINDING AE.**

    The original code was `offset = int(row["value"]) if row else 0` followed by
    `f.seek(offset)`, and it silently ended security monitoring the first time a
    log rotated.

    Seeking past EOF is legal and does not error: `readline()` simply returns
    nothing. The offset is only written back `if new_offset > offset`, which is
    never true again, so after a rotation this function returns **zero lines
    forever** — until the new file happens to grow past the old offset, which on
    a daily rotation it never does.

    So brute-force detection, the 401 sweep and the SSH failure alerts all stop,
    and nothing anywhere reports it: no error, no exception, no empty-result
    warning. A monitor that has silently stopped monitoring is worse than no
    monitor, because a dashboard of zeroes reads as "no attacks".

    Rotation is detected two ways, because neither alone is enough:

    * **inode change** — the reliable signal, available once an offset has been
      written in the JSON form.
    * **size < offset** — the fallback for a bare-integer offset from before
      this fix, and for a truncate-in-place (`logrotate copytruncate`), where
      the inode does not change at all.

    On rotation, reading restarts at 0. That re-reads the new file from its
    beginning and can re-alert on lines already seen. That is the owner's
    finding-T direction applied consistently: *duplicate alerts are preferable
    to silently losing the window.*
    """
    parsed = _parse_offset(stored)
    if stored is None:
        return 0, OffsetOrigin.COLD_START
    if parsed is None:
        logger.error(
            "%s is stored but UNPARSEABLE (%r). Restarting this log from the "
            "beginning, which may re-alert on events already seen — that is "
            "deliberate, and preferable to a monitor that silently sees "
            "nothing.", key, stored)
        return 0, OffsetOrigin.CORRUPT

    offset, inode = parsed
    try:
        stat = os.stat(path)
    except OSError:
        # The caller's own error handling covers an unreadable file; nothing to
        # decide here, so the stored offset stands.
        return offset, OffsetOrigin.STORED

    rotated_by_inode = inode is not None and inode != stat.st_ino
    rotated_by_size = stat.st_size < offset
    if rotated_by_inode or rotated_by_size:
        logger.error(
            "%s: the log at %s was ROTATED or TRUNCATED (%s). Resuming from "
            "offset 0 instead of %d. Without this the monitor would seek past "
            "the end of the new file and read NOTHING, silently, on every scan "
            "from now on. Events written to the previous file after the last "
            "scan are lost; events in the new one may be re-alerted.",
            key, path,
            "inode changed" if rotated_by_inode else
            f"size {stat.st_size} < offset {offset}",
            offset)
        return 0, OffsetOrigin.ROTATED
    return offset, OffsetOrigin.STORED


def _encode_offset(path: str, offset: int) -> str:
    """The stored form: the offset AND the inode it belongs to.

    The inode is what makes rotation detectable at all in the ordinary case
    where the new file has already grown past the old offset.
    """
    try:
        inode = os.stat(path).st_ino
    except OSError:
        inode = None
    return json.dumps({"offset": int(offset), "inode": inode})


async def _read_new_log_lines() -> list[str]:
    """Read new nginx log lines since last scan using stored file offset."""
    nginx_log = get_settings().nginx_log_path  # Fix L16: lazy read from config
    if not os.path.exists(nginx_log):
        return []

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM monitor_state WHERE key='nginx_log_offset'"
        )
    offset, _origin = _resolve_offset(nginx_log, row["value"] if row else None,
                                      key="nginx_log_offset")

    try:
        with open(nginx_log, "r", errors="replace") as f:
            f.seek(offset)
            lines = []
            for _ in range(_MAX_LINES_PER_SCAN):  # Fix P13: use module constant
                line = f.readline()
                if not line:
                    break
                lines.append(line)
            if len(lines) == _MAX_LINES_PER_SCAN:
                logger.warning(
                    f"Security monitor: log read capped at {_MAX_LINES_PER_SCAN} lines. "
                    "Some events may be skipped this cycle — will catch up next scan."
                )
            new_offset = f.tell()

        if new_offset > offset:
            async with acquire() as conn:
                await conn.execute(
                    "UPDATE monitor_state SET value=$1, updated_at=NOW() WHERE key='nginx_log_offset'",
                    _encode_offset(nginx_log, new_offset),
                )
        return lines
    except Exception as e:
        logger.warning(f"Could not read nginx log: {e}")
        return []


async def _write_security_events(events: list[dict]) -> int:
    """Persist security events and return count of new critical/high events."""
    if not events:
        return 0
    high_count = 0
    async with acquire() as conn:
        for ev in events:
            await conn.execute(
                """INSERT INTO security_events
                   (severity, event_type, source_ip, description, raw_log)
                   VALUES ($1, $2, $3::inet, $4, $5)""",
                ev["severity"],
                ev["event_type"],
                ev.get("source_ip"),
                ev["description"],
                ev.get("raw_log", ""),
            )
            if ev["severity"] in ("critical", "high"):
                high_count += 1
    return high_count


# ── System health check ───────────────────────────────────────────────────────

async def _check_system_health() -> list[dict]:
    """Check CPU, memory, and disk for anomalies."""
    events = []
    # Fix #71: cpu_percent(interval=1) sleeps for 1 second in the calling thread.
    # Running it in an executor prevents blocking the asyncio event loop.
    loop = asyncio.get_running_loop()
    cpu = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=1))
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    if cpu > 90:
        events.append({
            "severity": "medium",
            "event_type": "high_cpu",
            "description": f"CPU usage critical: {cpu:.1f}%",
        })
        # send_alert uses requests (sync) — run in executor to avoid blocking the event loop
        owner_name = get_settings().owner_name
        alert_msg = (
            f"{owner_name}, CPU usage is *{cpu:.1f}%* — potential cryptominer or runaway process.\n"
            f"Say _\"IRA, scan threats\"_ to check active connections."
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: send_alert(alert_msg, priority="warning"),
        )
    if mem.percent > 90:
        events.append({
            "severity": "medium",
            "event_type": "high_memory",
            "description": f"Memory usage critical: {mem.percent:.1f}%",
        })
    if disk.percent > 85:
        events.append({
            "severity": "high",
            "event_type": "disk_pressure",
            "description": f"Disk usage at {disk.percent:.1f}% — action required",
        })
    return events


# ── SSH auth log monitoring ───────────────────────────────────────────────────

async def _check_ssh_failures() -> list[dict]:
    """Parse auth.log for failed SSH logins since last scan offset.

    Fix P7: when both auth.log is unreadable AND journald is unavailable, log
    a single WARNING at first occurrence so operators know SSH monitoring is
    inactive, without spamming every 60-second scan cycle.

    Working path: set SSH_LOG_PATH env to a readable copy of auth.log
    (e.g. via a log-shipper sidecar or by adding the 'ira' user to the 'adm'
    group on Ubuntu — `usermod -aG adm ira` + matching GID in Dockerfile.worker).
    """
    global _ssh_inactive_warned  # noqa: PLW0603

    ssh_log = get_settings().ssh_log_path
    if not os.path.exists(ssh_log):
        result = await _check_ssh_failures_journald()
        if not result and not _ssh_inactive_warned:
            _ssh_inactive_warned = True
            logger.warning(
                "SSH monitoring inactive: auth.log not found at %s and journalctl "
                "missing — see docs for fix (set SSH_LOG_PATH or add ira user to adm group).",
                ssh_log,
            )
        return result

    result = await _check_ssh_failures_file()
    return result


def _parse_ssh_lines(lines: list[str]) -> list[dict]:
    """Extract SSH brute-force events from a list of log lines (file or journald)."""
    from collections import defaultdict
    ip_failures: dict[str, int] = defaultdict(int)
    ip_users: dict[str, set] = defaultdict(set)

    for line in lines:
        m = _SSH_FAIL_RE.search(line) or _SSH_INVALID_RE.search(line)
        if m:
            user, ip = m.group(1), m.group(2)
            ip_failures[ip] += 1
            ip_users[ip].add(user)

    events = []
    for ip, count in ip_failures.items():
        if count >= 3:
            users_tried = ", ".join(list(ip_users[ip])[:5])
            events.append({
                "severity": "high" if count >= 10 else "medium",
                "event_type": "ssh_brute_force",
                "source_ip": ip,
                "description": (
                    f"SSH brute-force: {count} failed attempts from {ip}. "
                    f"Usernames tried: {users_tried}"
                ),
                "raw_log": "",
            })
    return events


async def _check_ssh_failures_journald() -> list[dict]:
    """Fallback: read SSH failures from journald (no file mount required)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "ssh", "-u", "sshd", "--since", "1 hour ago",
            "--no-pager", "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        lines = stdout.decode(errors="replace").splitlines()
    except (FileNotFoundError, asyncio.TimeoutError, Exception):
        # journalctl not available (e.g., inside minimal container) — skip silently
        return []

    return _parse_ssh_lines(lines)


async def _check_ssh_failures_file() -> list[dict]:
    """Parse auth.log for failed SSH logins since last scan offset.

    Fix L6: IP-tracking loop removed — delegated to _parse_ssh_lines() to avoid
    code duplication with _check_ssh_failures_journald(). The Telegram push for
    time-critical attacks (count >= 5) is handled here from the returned events.
    """
    ssh_log = get_settings().ssh_log_path  # Fix L16: lazy read from config

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM monitor_state WHERE key='ssh_log_offset'"
        )
    # FINDING AE, the second of the two. `auth.log` rotates on exactly the same
    # schedule as the nginx log, so this had the identical blind spot: after the
    # first rotation, SSH brute-force detection silently read nothing forever.
    offset, _origin = _resolve_offset(ssh_log, row["value"] if row else None,
                                      key="ssh_log_offset")

    try:
        with open(ssh_log, "r", errors="replace") as f:
            MAX_LINES_PER_SCAN = 10_000   # auth.log is less verbose than nginx
            f.seek(offset)
            lines = []
            for _ in range(MAX_LINES_PER_SCAN):
                line = f.readline()
                if not line:
                    break
                lines.append(line)
            new_offset = f.tell()
    except PermissionError:
        # Fix P7: emit warning once when file is unreadable (640 root:adm vs non-adm user)
        global _ssh_inactive_warned  # noqa: PLW0603
        if not _ssh_inactive_warned:
            _ssh_inactive_warned = True
            logger.warning(
                "SSH monitoring inactive: auth.log unreadable and journalctl missing — "
                "add the 'ira' user to the 'adm' group (usermod -aG adm ira) or set "
                "SSH_LOG_PATH to a readable copy. See deployment docs.",
            )
        return []

    if new_offset > offset:
        async with acquire() as conn:
            await conn.execute(
                "UPDATE monitor_state SET value=$1, updated_at=NOW() WHERE key='ssh_log_offset'",
                _encode_offset(ssh_log, new_offset),
            )

    # Delegate event detection to shared helper — eliminates duplicate IP-tracking loop
    events = _parse_ssh_lines(lines)

    # Direct Telegram push for time-critical SSH attacks (>= 5 attempts)
    # Extract count from standardised description written by _parse_ssh_lines()
    import re as _re
    for ev in events:
        m = _re.search(r"(\d+) failed attempts", ev.get("description", ""))
        count = int(m.group(1)) if m else 0
        if count >= 5:
            ssh_msg = (
                f"{get_settings().owner_name}, *{count} failed SSH logins* "
                f"from `{ev['source_ip']}`.\n\n"
                f"Say _\"IRA, scan threats\"_ to investigate."
            )
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda msg=ssh_msg: send_alert(msg, priority="critical"),
            )

    return events


# ── Check existing unresolved critical events ─────────────────────────────────

async def _check_unresolved_criticals() -> int:
    async with acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM security_events WHERE severity='critical' AND resolved=FALSE"
        ) or 0


# ── Main scan loop ────────────────────────────────────────────────────────────

async def run_security_scan() -> None:
    """
    Run one full security scan cycle.
    Called every 60 seconds by the APScheduler.
    """
    logger.debug("Running security scan...")

    # 1. Analyse nginx logs
    lines = await _read_new_log_lines()
    log_events = _analyse_lines(lines) if lines else []

    # 2. Check SSH auth failures
    ssh_events = await _check_ssh_failures()

    # 3. Check system health
    system_events = await _check_system_health()

    all_events = log_events + ssh_events + [
        {**ev, "source_ip": None, "raw_log": ""}
        for ev in system_events
        if "source_ip" not in ev
    ]

    # 4. Write events to DB  (Fix L7: was "3." — duplicate of the check-system-health step)
    new_highs = await _write_security_events(all_events)

    # 5. P6.2: Apply bounded automated playbooks for critical events
    if all_events:
        try:
            from worker.self_healing import run_security_playbooks
            await run_security_playbooks(all_events)
        except Exception as exc:
            logger.warning("run_security_playbooks failed (non-critical): %s", exc)

    # 6. Alert if significant threats found
    if new_highs > 0:
        threat_summary = "\n".join(
            f"• [{e['severity'].upper()}] {e['description']}"
            for e in all_events if e["severity"] in ("critical", "high")
        )
        await notify(
            f"Security Alert — {new_highs} new threat(s) detected",
            f"{get_settings().owner_name}, IRA has detected the following security events:\n\n{threat_summary}\n\n"
            f"Please review and I can suggest remediation steps.",
            category="security",
            priority="critical" if any(e["severity"] == "critical" for e in all_events) else "warning",
        )

    # 6. Hourly: alert if unresolved criticals still open
    criticals = await _check_unresolved_criticals()
    if criticals > 0:
        logger.warning(f"{criticals} unresolved critical security events")
