"""
armory/docker_manager.py — throwaway, network-gated tool containers.

Every Armory tool runs in a fresh container that is destroyed the instant it
exits (``docker run --rm``), with no volumes mounted — so hacking output is never
persisted to disk (only the audit fact that a tool ran, via the Trust Ledger).

Network killswitch (fail-closed by construction):
  * ``network=none`` tools launch with ``--network none`` — zero connectivity.
  * ``network=tor``  tools share the Tor container's network namespace
    (``--network container:armory_tor``) and egress through its SOCKS proxy on
    127.0.0.1:9050. Kali tools are wrapped with ``proxychains4`` so their traffic
    is forced through Tor. If the Tor container is not running, the tool has NO
    network at all — there is no direct-internet fallback.

Hardening applied to every run: ``--cap-drop ALL``, ``--security-opt
no-new-privileges``, a read-only rootfs with a small tmpfs, and CPU/memory/pid
caps so a tool can't exhaust the host.

The module shells out to the ``docker`` CLI via asyncio (no dockerode/SDK
dependency). It imports cleanly with no Docker present; ``available()`` reports
whether Docker is usable, and every run fails closed if it is not. The argv
builders are pure functions so they can be unit-tested without Docker.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from typing import Optional

from armory.manifest import Tool

logger = logging.getLogger("ira.armory.docker")

TOR_CONTAINER = "armory_tor"
TOR_IMAGE = "dperson/torproxy:latest"

#: Manifest image label → concrete image reference.
IMAGE_MAP = {
    "kali": "kalilinux/kali-rolling:latest",
    "tor": TOR_IMAGE,
}

# Resource caps applied to every tool container.
CPU_LIMIT = "1.0"
MEMORY_LIMIT = "1g"
PIDS_LIMIT = "256"
DEFAULT_TIMEOUT_S = 120
OUTPUT_CAP = 12_000  # chars returned to the caller (keeps LLM context sane)


@dataclass
class ToolResult:
    """Outcome of one containerised tool run. Output is returned, not stored."""

    tool_id: str
    exit_code: int
    output: str
    timed_out: bool = False
    network: str = "none"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class DockerUnavailable(RuntimeError):
    """Docker is not installed or the daemon is unreachable — fail closed."""


def _docker_bin() -> Optional[str]:
    return shutil.which("docker")


#: How long to wait for a SIGKILLed child to actually be reaped.
#:
#: P14-04 FINDING AD. This used to be `await proc.wait()` with no bound, which
#: is P13-01 finding Z's shape at a different layer: a carefully chosen timeout
#: undone on the next line by an unbounded wait. SIGKILL is not instantaneous
#: when the child is blocked in an uninterruptible syscall — and the realistic
#: way for the `docker` CLI to get there is a wedged daemon socket or a stuck
#: storage driver, which is *the same condition that made the command time out
#: in the first place*. The failure mode is correlated with its own trigger,
#: which is the worst kind.
REAP_TIMEOUT_S = 5

#: Bounded removal of a container left behind by a killed client.
CLEANUP_TIMEOUT_S = 10


async def _run(argv: list[str], *, timeout: int) -> tuple[int, str, bool]:
    """Run a command, capturing combined output. Returns (code, output, timed_out)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace"), False
    except asyncio.TimeoutError:
        reaped = await _kill_and_reap(proc, argv)
        # P14-04 FINDING AD, second half. This used to return
        # "container timed out and was killed", and **both halves of that could
        # be false**. `proc.kill()` signals the `docker` CLI CLIENT, not the
        # container: with `docker run --rm` and no `-d`, killing the client
        # leaves the container running, and `--rm` cleanup is performed by the
        # very client that just died — so the container can be left behind too.
        #
        # Saying so is this phase's subject applied to itself: an outcome nobody
        # confirmed must not be reported as a clean one. Removing the container
        # is `run_tool`'s job, because only it knows the container's name.
        detail = ("the docker client was killed"
                  if reaped else
                  f"the docker client did NOT exit within {REAP_TIMEOUT_S}s of "
                  f"SIGKILL")
        return 124, (f"timed out after {timeout}s; {detail}. Whether the "
                     f"container itself stopped is reported separately."), True


async def _kill_and_reap(proc, argv: list[str]) -> bool:
    """SIGKILL the child and wait for it, **bounded**. True if it was reaped.

    Returns rather than raises, because a child that will not die is not a
    reason to lose the timeout result the caller is waiting for — it is a reason
    to say so.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        return True                      # already gone; nothing to reap
    try:
        await asyncio.wait_for(proc.wait(), timeout=REAP_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        logger.error(
            "%r did not exit within %ss of SIGKILL — the process is most "
            "likely blocked in an uninterruptible syscall against the same "
            "Docker daemon that caused the timeout. Abandoning the reap so the "
            "caller is not blocked with it; this leaks one child process.",
            argv[:3], REAP_TIMEOUT_S)
        return False


class DockerManager:
    """Builds and runs ephemeral tool containers; manages the Tor sidecar.

    Conforms to the armory ``Executor`` protocol (``name`` + ``available`` +
    ``run_tool``), so it slots into the same selection path as the non-Docker
    sandboxes in ``armory/executors.py``.
    """

    name = "docker"

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    async def available(self) -> bool:
        """True only if Docker is enabled, installed, and the daemon responds."""
        if not self.enabled or _docker_bin() is None:
            return False
        try:
            code, _out, _ = await _run(["docker", "version", "--format", "{{.Server.Version}}"],
                                       timeout=10)
            return code == 0
        except Exception:  # noqa: BLE001
            return False

    async def _tor_running(self) -> bool:
        code, out, _ = await _run(
            ["docker", "ps", "--filter", f"name=^{TOR_CONTAINER}$", "--format", "{{.Names}}"],
            timeout=10,
        )
        return code == 0 and TOR_CONTAINER in out

    async def ensure_tor(self) -> None:
        """Start the Tor sidecar if needed. Raises if it cannot be brought up.

        This is the killswitch's live half: if Tor cannot run, network tools get
        no network (the caller must not fall back to direct internet).
        """
        if await self._tor_running():
            return
        # Remove any stale stopped container of the same name, then start fresh.
        await _run(["docker", "rm", "-f", TOR_CONTAINER], timeout=15)
        code, out, _ = await _run(
            ["docker", "run", "-d", "--rm", "--name", TOR_CONTAINER,
             "-p", "127.0.0.1:9050:9050", TOR_IMAGE],
            timeout=60,
        )
        if code != 0:
            raise DockerUnavailable(f"failed to start Tor proxy: {out.strip()[:300]}")
        # Give Tor a moment to open a circuit before tools egress through it.
        await asyncio.sleep(3)

    def build_run_argv(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S,
                       name: Optional[str] = None) -> list[str]:
        """Construct the hardened ``docker run`` argv for a tool (pure function).

        No volumes are mounted and ``--rm`` discards the container when it
        exits. Network mode is derived from the tool; ``tor`` tools join the Tor
        namespace and kali tools are proxychained through it.

        ``name`` is P14-04 FINDING AD's other half. Without it a timed-out
        container **cannot be addressed**, so there is no way to stop it: the
        only handle was the `docker` CLI process, and killing that does not stop
        the container it started. ``--rm`` covers the container *exiting*, which
        is exactly the case a timeout is not.

        Passed in rather than generated here so this stays a pure function —
        `test_docker_manager.py` compares argv lists directly, and a uuid
        minted inside would make every one of those comparisons unwriteable.
        """
        image = IMAGE_MAP.get(tool.image)
        if image is None:
            raise ValueError(f"no image mapping for {tool.image!r}")

        argv = [
            "docker", "run", "--rm",
        ]
        if name is not None:
            argv += ["--name", name]
        argv += [
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cpus", CPU_LIMIT,
            "--memory", MEMORY_LIMIT,
            "--pids-limit", PIDS_LIMIT,
            "--stop-timeout", "5",
        ]

        if tool.network == "tor":
            # Share the Tor container's netns: 127.0.0.1:9050 is the SOCKS proxy.
            argv += ["--network", f"container:{TOR_CONTAINER}"]
        else:
            argv += ["--network", "none"]

        argv.append(image)

        # Force kali/tor tools through Tor with proxychains, unless the command
        # already speaks SOCKS itself (its template will name a socks flag).
        cmd_text = tool.cmd.lower()
        speaks_socks = "socks" in cmd_text or "proxychains" in cmd_text
        if tool.network == "tor" and tool.image == "kali" and not speaks_socks:
            return argv + ["proxychains4", "-q", *tool_argv]
        return argv + tool_argv

    async def run_tool(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S) -> ToolResult:
        """Run one tool in a throwaway container. Fail-closed on any Docker fault."""
        if not await self.available():
            raise DockerUnavailable(
                "Docker is not available — the Armory cannot run tools without it.")
        if tool.network == "tor":
            await self.ensure_tor()  # raises if Tor can't come up → no network

        # P14-04 FINDING AD. Named so a timeout has something to address. The
        # name is minted here, per run, and never reused: `docker run --name`
        # fails outright on a collision, which would turn a retry into a refusal.
        container = f"ira-tool-{uuid.uuid4().hex[:16]}"
        argv = self.build_run_argv(tool, tool_argv, timeout=timeout,
                                   name=container)
        logger.info("armory run: tool=%s network=%s container=%s",
                    tool.id, tool.network, container)
        code, output, timed_out = await _run(argv, timeout=timeout + 10)

        if timed_out:
            # The container is very probably still running: `proc.kill()` killed
            # the CLI client, not the workload, and `--rm` is performed by that
            # same client. This is an untrusted tool sandbox, so a survivor is a
            # security problem and not merely an untidy one.
            removed = await self._force_remove(container)
            output += (
                f" Container {container} was removed." if removed else
                f" WARNING: container {container} could NOT be confirmed "
                f"removed — it may still be running. An untrusted tool "
                f"container outliving its timeout is a containment failure, "
                f"not an untidy one.")

        return ToolResult(
            tool_id=tool.id,
            exit_code=code,
            output=output[:OUTPUT_CAP] or "(no output)",
            timed_out=timed_out,
            network=tool.network,
        )

    async def _force_remove(self, container: str) -> bool:
        """`docker rm -f`, bounded, and honest about not having worked.

        Returns False rather than raising: a container that will not go away is
        something the caller must be TOLD about, and swallowing the failure
        would put this back where finding AD started — a clean-looking outcome
        for something nobody confirmed.
        """
        try:
            code, out, timed_out = await _run(
                ["docker", "rm", "-f", container], timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            logger.error("could not even run `docker rm -f %s`: %s: %s",
                         container, type(exc).__name__, exc)
            return False
        if timed_out or code != 0:
            # `docker rm -f` on a container that is already gone exits non-zero
            # with "No such container". That is a SUCCESS for our purposes and
            # is the ordinary case when the tool finished just as we gave up.
            if "no such container" in out.lower():
                return True
            logger.error(
                "`docker rm -f %s` failed (code=%s, timed_out=%s): %s. The "
                "container may still be running.",
                container, code, timed_out, out.strip()[:200])
            return False
        return True

    async def teardown(self) -> None:
        """Best-effort removal of the Tor sidecar (tool containers are ``--rm``)."""
        await _run(["docker", "rm", "-f", TOR_CONTAINER], timeout=15)


__all__ = [
    "DockerManager", "ToolResult", "DockerUnavailable",
    "IMAGE_MAP", "TOR_CONTAINER", "DEFAULT_TIMEOUT_S",
]
