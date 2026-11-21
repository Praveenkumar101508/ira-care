"""The Redis that start-ira.sh starts listens on this machine only.

start-ira.sh starts its own redis-server when nothing answers on
REDIS_HOST:REDIS_PORT: a container, WSL without systemd, or a Mac whose
`brew services` Redis is stopped. It started it without --bind, and a
redis-server with no configuration file listens on every interface. Pull
request #190 saw 0.0.0.0:6379 on a clean Ubuntu 24.04 install. Only protected
mode, which applies only while no password is set, turned other machines away.
Evidence: supracloud-jarvis/docs/testing/evidence/redis-bind-localhost-2026-11-01/.

The first tests read the command; the last one runs start-ira.sh's own Redis
block on a free port and reads the kernel's listening sockets.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
START_SH = ROOT / "start-ira.sh"


def _script() -> str:
    return START_SH.read_text(encoding="utf-8")


def _redis_command() -> str:
    """The redis-server command start-ira.sh runs, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", _script())
    commands = [line.strip() for line in joined.splitlines()
                if re.match(r"\s*redis-server\s", line)]
    assert len(commands) == 1, f"expected one redis-server command in start-ira.sh: {commands}"
    return commands[0]


def _bind_values() -> list[str]:
    """Every address start-ira.sh puts in REDIS_BIND, the array --bind expands."""
    values: list[str] = []
    for match in re.finditer(r"REDIS_BIND\+?=\(([^)]*)\)", _script()):
        values += match.group(1).split()
    return values


def test_redis_is_started_with_bind_protected_mode_and_the_configured_port():
    command = _redis_command()

    assert '--bind "${REDIS_BIND[@]}"' in command, f"no loopback --bind: {command}"
    assert "--protected-mode yes" in command, f"protected mode not stated: {command}"
    assert '--port "$REDIS_PORT_V"' in command, (
        f"Redis started on its default port, not REDIS_PORT, which start-ira.sh then "
        f"waits for: {command}")


def test_every_bind_address_is_loopback():
    values = _bind_values()

    assert values, "start-ira.sh assigns no address to REDIS_BIND"
    assert values[0] == "127.0.0.1", f"IPv4 loopback must always be bound: {values}"
    for value in values:
        # Redis 7 reads a leading "-" as "optional: skip it if it cannot be bound".
        address = ipaddress.ip_address(value.removeprefix("-"))
        assert address.is_loopback, f"REDIS_BIND holds {value}, which is not loopback"


def test_the_optional_ipv6_loopback_is_only_offered_to_redis_7_and_later():
    """`-::1` is Redis 7 syntax. An older redis-server would read it as a host
    name and refuse to start, so it is added only behind a version check."""
    text = _script()
    added = re.search(r"REDIS_BIND\+=\(-::1\)", text)
    if added is None:
        return
    line = text[text.rfind("\n", 0, added.start()) + 1:text.find("\n", added.end())]
    assert re.search(r"-ge 7\b", line), f"-::1 is added without a Redis 7 check: {line!r}"


def test_api_default_redis_host_is_loopback():
    """The API must find the loopback-only Redis without any setting."""
    from config import Settings

    assert Settings.model_fields["redis_host"].default in {"localhost", "127.0.0.1", "::1"}
    example = re.search(r"^REDIS_HOST=(\S*)", (ROOT / ".env.example").read_text(encoding="utf-8"),
                        re.M)
    assert example is not None and example.group(1) in {"localhost", "127.0.0.1", "::1"}, (
        "the template points the API at a Redis host the started Redis does not listen on")


# ── Run start-ira.sh's own Redis block ───────────────────────────────────────

def _redis_block() -> str:
    """From `if command -v redis-server` to its closing `fi`, as start-ira.sh has it."""
    lines = _script().splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.strip().startswith("if command -v redis-server"))
    indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == f"{indent}fi")
    return "\n".join(lines[start:end + 1])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> list[str]:
    """Addresses with a LISTEN socket on `port`, from the kernel's tables."""
    found = []
    for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            rows = Path(path).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            local, state = row.split()[1], row.split()[3]
            addr_hex, port_hex = local.split(":")
            if state != "0A" or int(port_hex, 16) != port:
                continue
            raw = bytes.fromhex(addr_hex)
            if v6:
                raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                found.append(str(ipaddress.IPv6Address(raw)))
            else:
                found.append(str(ipaddress.IPv4Address(raw[::-1])))
    return found


def _resp(port: int, *args: str) -> bytes:
    payload = f"*{len(args)}\r\n".encode() + b"".join(
        f"${len(a)}\r\n{a}\r\n".encode() for a in args)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        try:
            return s.recv(4096)
        except OSError:
            return b""


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server is not installed")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/net/tcp")
def test_the_redis_start_ira_starts_listens_on_loopback_only(tmp_path):
    port = _free_port()
    # The block as start-ira.sh has it, plus a pid file and a log in tmp_path:
    # daemonized without one, Redis writes /var/run/redis.pid and deletes it on
    # shutdown, which would take another Redis's pid file with it.
    block = _redis_block()
    assert "redis-server --daemonize" in block, block
    block = block.replace(
        "redis-server --daemonize",
        f'redis-server --pidfile "{tmp_path}/redis.pid" --logfile "{tmp_path}/redis.log" '
        "--daemonize", 1)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    script = f'RUN_DIR="{run_dir}"\nREDIS_PORT_V={port}\n{block}\n'
    subprocess.run(["bash", "-c", script], check=True, timeout=60)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not _listening(port):
            time.sleep(0.2)
        listening = _listening(port)
        log = (tmp_path / "redis.log").read_text(errors="replace") if (tmp_path / "redis.log").exists() else ""

        assert listening, f"start-ira.sh's Redis did not start on {port}:\n{log[-2000:]}"
        outside = [a for a in listening if not ipaddress.ip_address(a).is_loopback]
        assert not outside, f"start-ira.sh's Redis listens on {outside}, not only on loopback"
        assert b"yes" in _resp(port, "CONFIG", "GET", "protected-mode")
    finally:
        # By the pid file, so a Redis that started on some other port (a
        # command without --port) is stopped too.
        pidfile = tmp_path / "redis.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text().strip() or 0)
            if pid > 0:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
                    time.sleep(0.2)
