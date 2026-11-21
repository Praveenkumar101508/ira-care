"""
armory/executors.py — pluggable execution backends (Docker is no longer required).

The Armory's original executor was Docker-only, and Docker isn't always present.
This module adds sandboxes that preserve the two guarantees that matter — the
**network killswitch** and **no host persistence** — WITHOUT a Docker daemon, by
using Linux user namespaces:

  * ``DockerManager``      (strongest) — throwaway container, netns-shared Tor.
  * ``FirejailExecutor``   — ``firejail --net=none`` cuts the network at the
                             kernel for offline tools; Tor tools egress through a
                             host Tor SOCKS via proxychains.
  * ``BubblewrapExecutor`` — ``bwrap --unshare-net`` does the same with bubblewrap;
                             read-only rootfs + private tmpfs, no daemon.
  * ``LocalExecutor``      (weakest, opt-in) — runs the tool directly on the host
                             with NO isolation. Refused unless the owner explicitly
                             sets ``ARMORY_ALLOW_UNSANDBOXED=true``.

``select_executor`` chooses the best available backend for the configured policy.
Every backend is **fail-closed**: a missing sandbox binary, a missing tool binary,
or (for network tools) an unreachable Tor proxy raises rather than running with
less protection than promised. The argv builders are pure functions so the
killswitch/hardening flags are unit-testable without any of these binaries.

Honesty boundary: only Docker shares a network namespace with Tor, so only Docker
can *guarantee* that a network tool's traffic cannot bypass Tor. Firejail/bubblewrap
Tor enforcement is proxychains-based (forces SOCKS for well-behaved tools) — strong,
but not a kernel-level guarantee. Offline (``network: none``) tools ARE cut off at
the kernel by all three sandboxes. Prefer Docker when it is available.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Protocol, runtime_checkable

from armory.docker_manager import (
    DEFAULT_TIMEOUT_S,
    OUTPUT_CAP,
    DockerManager,
    DockerUnavailable,
    ToolResult,
    _run,
)
from armory.manifest import Tool

logger = logging.getLogger("ira.armory.executors")

TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050

BACKENDS = ("auto", "docker", "firejail", "bubblewrap", "local")


class SandboxUnavailable(RuntimeError):
    """A non-Docker sandbox (or its prerequisites) is missing — fail closed."""


#: The interface the gatekeeper depends on. DockerManager already conforms.
@runtime_checkable
class Executor(Protocol):
    name: str

    async def available(self) -> bool: ...

    async def run_tool(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S) -> ToolResult: ...


# ── shared helpers ────────────────────────────────────────────────────────────

def _speaks_socks(tool: Tool) -> bool:
    text = tool.cmd.lower()
    return "socks" in text or "proxychains" in text


def maybe_proxychains(tool: Tool, argv: list[str]) -> list[str]:
    """Force a Tor tool's traffic through the host SOCKS proxy via proxychains.

    Left untouched for offline tools and for tools that already speak SOCKS.
    """
    if tool.network == "tor" and not _speaks_socks(tool):
        return ["proxychains4", "-q", *argv]
    return argv


async def tor_reachable(*, timeout: float = 3.0) -> bool:
    """True if a TCP connection to the host Tor SOCKS port succeeds."""
    try:
        reader_writer = asyncio.open_connection(TOR_SOCKS_HOST, TOR_SOCKS_PORT)
        _reader, writer = await asyncio.wait_for(reader_writer, timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def _require_tor_egress(tool: Tool) -> None:
    """Fail closed unless a Tor tool can actually be forced through Tor.

    Needs proxychains (unless the tool speaks SOCKS itself) AND a reachable Tor
    SOCKS proxy. Without both, a network tool would either not run or leak — so
    we refuse, exactly like the Docker killswitch does when Tor is down.
    """
    if tool.network != "tor":
        return
    if not _speaks_socks(tool) and shutil.which("proxychains4") is None:
        raise SandboxUnavailable(
            "proxychains4 is not installed; cannot force Tor egress for a network tool.")
    if not await tor_reachable():
        raise SandboxUnavailable(
            "Tor SOCKS proxy not reachable at 127.0.0.1:9050 — network tools are "
            "blocked (killswitch). Start Tor and retry.")


async def _finish(tool: Tool, argv: list[str], timeout: int) -> ToolResult:
    code, output, timed_out = await _run(argv, timeout=timeout + 10)
    return ToolResult(
        tool_id=tool.id, exit_code=code,
        output=output[:OUTPUT_CAP] or "(no output)",
        timed_out=timed_out, network=tool.network,
    )


# ── pure argv builders (unit-testable without the binaries) ───────────────────

def build_firejail_argv(tool: Tool, tool_argv: list[str]) -> list[str]:
    """firejail invocation. ``--net=none`` for offline tools cuts the network at
    the kernel; Tor tools keep host networking (to reach 127.0.0.1:9050) and rely
    on the proxychains wrap already applied to ``tool_argv``."""
    argv = ["firejail", "--quiet", "--noprofile",
            "--caps.drop=all", "--nonewprivs", "--private-tmp"]
    if tool.network == "none":
        argv.append("--net=none")   # kernel-level killswitch
    return argv + tool_argv


def build_bubblewrap_argv(tool: Tool, tool_argv: list[str]) -> list[str]:
    """bwrap invocation. ``--unshare-net`` for offline tools removes all network;
    Tor tools share host net (for the SOCKS proxy) and rely on proxychains."""
    argv = ["bwrap", "--die-with-parent",
            "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--ro-bind", "/", "/", "--tmpfs", "/tmp",
            "--proc", "/proc", "--dev", "/dev"]
    if tool.network == "none":
        argv.append("--unshare-net")  # kernel-level killswitch
    return argv + tool_argv


# ── backends ──────────────────────────────────────────────────────────────────

class _HostSandbox:
    """Base for namespace sandboxes that run host-installed tools, no daemon."""

    name = "host-sandbox"
    binary = ""  # firejail | bwrap

    def _wrap(self, tool: Tool, tool_argv: list[str]) -> list[str]:
        raise NotImplementedError

    async def available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def run_tool(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S) -> ToolResult:
        if shutil.which(self.binary) is None:
            raise SandboxUnavailable(f"{self.binary} is not installed.")
        if not tool_argv or shutil.which(tool_argv[0]) is None:
            raise SandboxUnavailable(
                f"tool binary not found on host: {tool_argv[0] if tool_argv else '(none)'}. "
                "Install the tool or use the Docker backend.")
        await _require_tor_egress(tool)
        argv = self._wrap(tool, maybe_proxychains(tool, tool_argv))
        logger.info("armory run (%s): tool=%s network=%s", self.name, tool.id, tool.network)
        return await _finish(tool, argv, timeout)


class FirejailExecutor(_HostSandbox):
    name = "firejail"
    binary = "firejail"

    def _wrap(self, tool: Tool, tool_argv: list[str]) -> list[str]:
        return build_firejail_argv(tool, tool_argv)


class BubblewrapExecutor(_HostSandbox):
    name = "bubblewrap"
    binary = "bwrap"

    def _wrap(self, tool: Tool, tool_argv: list[str]) -> list[str]:
        return build_bubblewrap_argv(tool, tool_argv)


class LocalExecutor:
    """Runs the tool directly on the host with NO isolation. Opt-in only.

    There is no network namespace here, so an offline tool is NOT prevented from
    touching the network — only Tor tools are still forced through proxychains and
    refused if Tor is down. Use only when you accept host-level execution.
    """

    name = "local"

    async def available(self) -> bool:
        return True

    async def run_tool(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S) -> ToolResult:
        if not tool_argv or shutil.which(tool_argv[0]) is None:
            raise SandboxUnavailable(
                f"tool binary not found on host: {tool_argv[0] if tool_argv else '(none)'}.")
        await _require_tor_egress(tool)
        argv = maybe_proxychains(tool, tool_argv)
        logger.warning("armory run (UNSANDBOXED): tool=%s network=%s", tool.id, tool.network)
        return await _finish(tool, argv, timeout)


class NullExecutor:
    """No usable backend — every run fails closed with a helpful reason."""

    name = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def available(self) -> bool:
        return False

    async def run_tool(self, tool: Tool, tool_argv: list[str],
                       *, timeout: int = DEFAULT_TIMEOUT_S) -> ToolResult:
        raise SandboxUnavailable(self.reason)


# ── selection ─────────────────────────────────────────────────────────────────

def select_executor(*, backend: str = "auto", docker_enabled: bool = True,
                    allow_unsandboxed: bool = False) -> Executor:
    """Pick a backend for the configured policy.

    ``auto`` prefers Docker, then firejail, then bubblewrap, then (only if
    explicitly allowed) unsandboxed local. An explicit backend name is honoured
    even if its binary is missing — its ``run_tool`` then fails closed with a
    clear message rather than silently downgrading.
    """
    backend = (backend or "auto").strip().lower()
    have = lambda b: shutil.which(b) is not None  # noqa: E731

    if backend == "docker":
        return DockerManager(enabled=docker_enabled)
    if backend == "firejail":
        return FirejailExecutor()
    if backend == "bubblewrap":
        return BubblewrapExecutor()
    if backend == "local":
        if not allow_unsandboxed:
            return NullExecutor(
                "The 'local' (unsandboxed) backend requires ARMORY_ALLOW_UNSANDBOXED=true.")
        return LocalExecutor()

    # auto
    if docker_enabled and have("docker"):
        return DockerManager(enabled=docker_enabled)
    if have("firejail"):
        return FirejailExecutor()
    if have("bwrap"):
        return BubblewrapExecutor()
    if allow_unsandboxed:
        return LocalExecutor()
    return NullExecutor(
        "No execution sandbox available. Install Docker, firejail, or bubblewrap, "
        "or set ARMORY_ALLOW_UNSANDBOXED=true to run tools unsandboxed on the host.")


__all__ = [
    "Executor", "SandboxUnavailable", "BACKENDS",
    "FirejailExecutor", "BubblewrapExecutor", "LocalExecutor", "NullExecutor",
    "select_executor", "build_firejail_argv", "build_bubblewrap_argv",
    "maybe_proxychains", "tor_reachable",
    "DockerManager", "DockerUnavailable", "ToolResult",
]
