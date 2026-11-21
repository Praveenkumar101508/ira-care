"""P2-04: stop-ira.sh stops only this checkout's API.

Found by reading on 1 November 2026: the script's last step was

    pkill -f "uvicorn main:app.*--port ${IRA_API_PORT:-8000}"

under a comment saying it swept "only processes belonging to this checkout".
It matched any `uvicorn main:app` on that port from any folder (any user's,
when run as root), it matched longer port numbers (`--port 80000` for 8000),
and it took the port from the environment only, so a custom IRA_API_PORT in
.env, which start-ira.sh uses, was not the port it swept.

Every process these tests stop is a decoy they started: a sleeping Python
whose command line reads like IRA's API (`uvicorn main:app --port N`), started
from a temporary folder, on a port number that nothing on the machine uses.
The script under test is a copy of stop-ira.sh in a temporary "checkout", so
the folder it treats as its own holds nothing but decoys. Every test also sets
IRA_API_PORT in the environment to a decoy port, so even the old pkill line
could only ever reach decoys.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
STOP_SH = APP_ROOT / "stop-ira.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("ps"),
    reason="needs bash and ps")

SLEEPER = "import time; time.sleep(120)"


def _unused_port() -> int:
    """A port number no process on this machine is listening on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Decoy:
    """A sleeping process whose command line reads like IRA's API.

    Its own session (so its PID is a process-group leader, as start-ira.sh
    makes the API), and reaped by a thread here as soon as it exits, so a
    stopped decoy does not linger as a zombie that `kill -0` still finds.
    """

    def __init__(self, cwd: Path, *argv: str) -> None:
        self.proc = subprocess.Popen([sys.executable, "-c", SLEEPER, *argv], cwd=cwd,
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._reaper = threading.Thread(target=self.proc.wait, daemon=True)
        self._reaper.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stopped(self, within: float = 15.0) -> bool:
        self._reaper.join(within)
        return not self._reaper.is_alive()

    def running(self) -> bool:
        return not self.stopped(within=1.0)


@pytest.fixture
def decoys():
    started: list[Decoy] = []

    def start(cwd: Path, port, *, argv: tuple[str, ...] = ()) -> Decoy:
        """`uvicorn main:app --host 127.0.0.1 --port <port>` from `cwd`, or `argv`."""
        cwd.mkdir(parents=True, exist_ok=True)
        argv = argv or ("uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port))
        decoy = Decoy(cwd, *argv)
        started.append(decoy)
        return decoy

    yield start
    for decoy in started:
        if decoy.proc.poll() is None:
            decoy.proc.kill()
        decoy.stopped(within=5.0)


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A folder laid out like supracloud-jarvis/, holding a copy of stop-ira.sh."""
    root = tmp_path / "checkout"
    (root / "ira").mkdir(parents=True)
    (root / ".ira-run").mkdir()
    shutil.copy(STOP_SH, root / "stop-ira.sh")      # with its mode: it must stay executable
    return root


def _stop(root: Path, **env: str) -> str:
    run_env = {k: v for k, v in os.environ.items() if k not in ("IRA_API_PORT", "_IRA_STOP_CWD_FROM")}
    run_env.update(env)
    # As INSTALL.md gives it: `./stop-ira.sh`, from its folder.
    out = subprocess.run(["./stop-ira.sh"], cwd=root, env=run_env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


# ── The sweep for a stray API ────────────────────────────────────────────────


def test_the_sweep_stops_this_checkouts_api(checkout, decoys):
    port = _unused_port()
    ours = decoys(checkout / "ira", port)

    out = _stop(checkout, IRA_API_PORT=str(port))

    assert ours.stopped(), out
    assert f"swept stray uvicorn on port {port} (pid {ours.pid})" in out


def test_an_api_from_another_folder_is_left_running(checkout, decoys):
    """The same command line and port, from a folder whose name only starts
    with this checkout's."""
    port = _unused_port()
    ours = decoys(checkout / "ira", port)
    elsewhere = decoys(checkout.parent / (checkout.name + "-old") / "ira", port)

    out = _stop(checkout, IRA_API_PORT=str(port))

    assert ours.stopped(), out
    assert elsewhere.running(), f"stopped an API from another folder:\n{out}"


def test_only_this_exact_port_is_swept(checkout, decoys):
    port = _unused_port()
    ours = decoys(checkout / "ira", port)
    longer = decoys(checkout / "ira", f"{port}0")
    other = decoys(checkout / "ira", port + 1)
    by_equals = decoys(checkout / "ira", None, argv=("uvicorn", "main:app", f"--port={port}"))

    out = _stop(checkout, IRA_API_PORT=str(port))

    assert ours.stopped() and by_equals.stopped(), out
    assert longer.running(), f"--port {port}0 was taken for --port {port}:\n{out}"
    assert other.running(), out


# ── The port, as start-ira.sh chose it ───────────────────────────────────────


def test_the_env_file_port_wins_over_the_environment(checkout, decoys):
    """start-ira.sh loads .env over the environment, so .env's port is the one
    it started the API on."""
    from_env_file, from_environment = _unused_port(), _unused_port()
    (checkout / ".env").write_text(f"IRA_API_PORT={from_env_file}\n", encoding="utf-8")
    started = decoys(checkout / "ira", from_env_file)
    not_ours = decoys(checkout / "ira", from_environment)

    out = _stop(checkout, IRA_API_PORT=str(from_environment))

    assert started.stopped(), out
    assert not_ours.running(), out


@pytest.mark.parametrize("line", [
    "IRA_API_PORT={port}",
    "IRA_API_PORT={port}   # the API's port",
    'IRA_API_PORT="{port}"',
    "  IRA_API_PORT = '{port}'  # quoted",
    "IRA_API_PORT=1\nIRA_API_PORT={port}",           # the last line wins, as in start-ira.sh
])
def test_the_env_file_is_read_by_start_ira_rules(checkout, decoys, line):
    port, decoy_env = _unused_port(), _unused_port()
    (checkout / ".env").write_text("OWNER_NAME=Synthetic Owner\n" + line.format(port=port) + "\n",
                                   encoding="utf-8")
    ours = decoys(checkout / "ira", port)

    out = _stop(checkout, IRA_API_PORT=str(decoy_env))

    assert ours.stopped(), out


def test_a_port_that_is_not_a_number_sweeps_nothing(checkout, decoys):
    port = _unused_port()
    (checkout / ".env").write_text("IRA_API_PORT=not-a-port\n", encoding="utf-8")
    survivor = decoys(checkout / "ira", port)

    out = _stop(checkout, IRA_API_PORT=str(port))

    assert "IRA_API_PORT is not a number (not-a-port); no sweep" in out
    assert survivor.running(), out


# ── PID files ────────────────────────────────────────────────────────────────


def test_a_pid_file_stops_its_process(checkout, decoys):
    api = decoys(checkout / "ira", _unused_port())
    (checkout / ".ira-run" / "ira-api.pid").write_text(f"{api.pid}\n", encoding="utf-8")

    out = _stop(checkout, IRA_API_PORT=str(_unused_port()))

    assert api.stopped(), out
    assert f"stopped (pid {api.pid})" in out
    assert not (checkout / ".ira-run" / "ira-api.pid").exists()


def test_a_stale_pid_file_naming_another_process_is_left_alone(checkout, decoys):
    """After a crash or a reboot, a PID file can name another program."""
    other = decoys(checkout.parent / "someone-else", _unused_port())
    for name in ("ira-api", "worker", "frontend"):
        (checkout / ".ira-run" / f"{name}.pid").write_text(f"{other.pid}\n", encoding="utf-8")

    out = _stop(checkout, IRA_API_PORT=str(_unused_port()))

    assert other.running(), f"stopped another folder's process named by a stale PID file:\n{out}"
    assert out.count(f"pid {other.pid} is not this checkout's") == 3, out
    assert not list((checkout / ".ira-run").glob("*.pid")), "the stale PID files were kept"


def test_an_ollama_pid_file_naming_something_else_is_left_alone(checkout, decoys):
    """start-ira.sh starts ollama from wherever it was run, so the check is its name."""
    other = decoys(checkout / "ira", _unused_port())
    (checkout / ".ira-run" / "ollama.pid").write_text(f"{other.pid}\n", encoding="utf-8")

    out = _stop(checkout, IRA_API_PORT=str(_unused_port()))

    assert other.running(), out
    assert f"pid {other.pid} is not this checkout's ollama" in out


# ── macOS reads a process's folder with lsof ─────────────────────────────────


@pytest.mark.skipif(not shutil.which("lsof") or not Path("/proc/self/cwd").exists(),
                    reason="needs lsof and /proc to compare the two")
def test_the_lsof_reading_finds_the_same_processes(checkout, decoys):
    """macOS has no /proc; the script reads the folder with lsof there. Forced
    here on Linux, where both exist."""
    port = _unused_port()
    ours = decoys(checkout / "ira", port)
    elsewhere = decoys(checkout.parent / "elsewhere" / "ira", port)
    stale = decoys(checkout.parent / "someone-else", _unused_port())
    (checkout / ".ira-run" / "worker.pid").write_text(f"{stale.pid}\n", encoding="utf-8")

    out = _stop(checkout, IRA_API_PORT=str(port), _IRA_STOP_CWD_FROM="lsof")

    assert ours.stopped(), out
    assert elsewhere.running(), out
    assert stale.running(), out
