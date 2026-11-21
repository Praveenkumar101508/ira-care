"""verify-native-install.sh must see IRA's listening ports without `ss`.

On 23 October 2026 a genuine install of `main` @ `0a88694` reported
`loopback FAIL nothing is listening on 3000 or 8000` while its own smoke test
passed 9 of 9 against the API on port 8000. The machine had no `ss`, and
`listeners()` hid the error, so "could not look" read as "nothing there". The
same blindness made the stop step's "nothing left listening" PASS meaningless.

`listeners()` now falls back to the kernel's own tables (`/proc/net/tcp`,
`/proc/net/tcp6`), and only then to `lsof`, which misses a process whose name
holds an unclosed "(": the frontend's `next-server (v16.4.1)`, cut to 15
characters. When it can list nothing
at all, it says so (exit 3) instead of reporting an empty list.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
SCRIPT = APP_ROOT / "scripts" / "verify-native-install.sh"

# What the script needs before --print-listeners exits. Deliberately no ss and no lsof.
_TOOLS = ("bash", "dirname", "date", "uname", "paste", "awk", "grep", "sort")


def _bin(tmp_path: Path, extra: tuple[str, ...] = (), fake_uname: str | None = None) -> Path:
    d = tmp_path / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for tool in _TOOLS + extra:
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"{tool} is not installed here")
        if tool == "uname" and fake_uname:
            (d / "uname").write_text(f"#!/bin/sh\necho {fake_uname}\n")
            (d / "uname").chmod(0o755)
            continue
        (d / tool).symlink_to(found)
    return d


def _listeners(bin_dir: Path, ports: str, proc_net: Path | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": str(bin_dir), "HOME": str(bin_dir.parent), "IRA_VERIFY_PORTS": ports}
    if proc_net is not None:
        env["IRA_VERIFY_PROC_NET"] = str(proc_net)
    return subprocess.run([str(bin_dir / "bash"), str(SCRIPT), "--print-listeners"],
                          env=env, capture_output=True, text=True, timeout=30)


_HEADER4 = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
_HEADER6 = ("  sl  local_address                         remote_address                        st tx_queue "
            "rx_queue tr tm->when retrnsmt   uid  timeout inode\n")


def _row(n: int, local: str, state: str) -> str:
    return f"   {n}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000  1000        0 {1000 + n} 1\n"


def test_listeners_read_the_kernel_tables_without_ss_or_lsof(tmp_path):
    proc = tmp_path / "net"
    proc.mkdir()
    (proc / "tcp").write_text(_HEADER4
        + _row(0, "0100007F:1F40", "0A")     # 127.0.0.1:8000, LISTEN
        + _row(1, "00000000:0BB8", "0A")     # 0.0.0.0:3000, LISTEN
        + _row(2, "0200007F:1F40", "01")     # 127.0.0.2:8000, ESTABLISHED: not a listener
        + _row(3, "0100007F:270F", "0A")     # 127.0.0.1:9999, not an IRA port
        + _row(4, "0100007F:7531", "0A"))    # 127.0.0.1:30001, ends in "3000…" but is not port 3000
    (proc / "tcp6").write_text(_HEADER6
        + _row(0, "00000000000000000000000001000000:1F40", "0A")    # [::1]:8000
        + _row(1, "00000000000000000000000000000000:0BB8", "0A")    # [::]:3000
        + _row(2, "0000000000000000FFFF00000100007F:1F40", "0A")    # ::ffff:127.0.0.1:8000
        + _row(3, "B80D0120000000000000000001000000:1F40", "0A"))   # [2001:db8::1]:8000
    result = _listeners(_bin(tmp_path), "3000 8000", proc)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == sorted({
        "0.0.0.0:3000",
        "127.0.0.1:8000",
        "[2001:0db8:0000:0000:0000:0000:0000:0001]:8000",
        "[::1]:8000",
        "[::]:3000",
    }), result.stdout


def _bound(host: str = "127.0.0.1") -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    s.listen()
    return s


def test_a_real_listening_socket_is_found_without_ss_or_lsof(tmp_path):
    if not os.access("/proc/net/tcp", os.R_OK):
        pytest.skip("no /proc/net/tcp on this system")
    s = _bound()
    try:
        port = s.getsockname()[1]
        result = _listeners(_bin(tmp_path), str(port))
    finally:
        s.close()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [f"127.0.0.1:{port}"]


def test_a_listener_named_like_next_server_is_found_although_lsof_is_installed(tmp_path):
    """The frontend runs as "next-server (v16.4.1)". The kernel keeps 15 characters,
    "next-server (v1", and lsof 4.95 skips a process whose name holds an unclosed "(".
    With lsof ahead of /proc, port 3000 was invisible at the loopback step and the
    stop step alike (23 October 2026, run 2)."""
    if not os.access("/proc/net/tcp", os.R_OK):
        pytest.skip("no /proc/net/tcp on this system")
    code = (
        "import ctypes, socket, sys, time\n"
        "ctypes.CDLL(None).prctl(15, b'next-server (v16.4.1)', 0, 0, 0)\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True); time.sleep(30)\n")
    proc = subprocess.Popen([shutil.which("python3") or "python3", "-c", code],
                            stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        extra = ("lsof",) if shutil.which("lsof") else ()
        result = _listeners(_bin(tmp_path, extra=extra), str(port))
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [f"127.0.0.1:{port}"]


def test_lsof_is_the_last_resort_when_ss_and_proc_are_missing(tmp_path):
    if shutil.which("lsof") is None:
        pytest.skip("lsof is not installed here")
    s = _bound()
    try:
        port = s.getsockname()[1]
        result = _listeners(_bin(tmp_path, extra=("lsof",)), str(port), tmp_path / "no-proc")
    finally:
        s.close()
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [f"127.0.0.1:{port}"]


def test_nothing_listening_is_an_empty_list_and_success(tmp_path):
    proc = tmp_path / "net"
    proc.mkdir()
    (proc / "tcp").write_text(_HEADER4 + _row(0, "0100007F:270F", "0A"))
    result = _listeners(_bin(tmp_path), "3000 8000", proc)
    assert (result.returncode, result.stdout) == (0, "")


def test_no_way_to_list_sockets_is_not_reported_as_nothing_listening(tmp_path):
    # Linux with no ss, no lsof and no readable socket table.
    linux = _listeners(_bin(tmp_path), "3000 8000", tmp_path / "no-proc")
    assert linux.returncode == 3, (linux.returncode, linux.stdout, linux.stderr)
    # macOS with no lsof.
    mac_bin = _bin(tmp_path / "mac", fake_uname="Darwin")
    mac = _listeners(mac_bin, "3000 8000")
    assert mac.returncode == 3, (mac.returncode, mac.stdout, mac.stderr)
