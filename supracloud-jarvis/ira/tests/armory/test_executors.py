"""Tests for the pluggable execution backends (Docker-optional killswitch)."""
import pytest

import armory.executors as ex
from armory.docker_manager import DockerManager
from armory.manifest import load_manifest

_M = """
{"tools": [
  {"id": "nmap_scan", "image": "kali", "cmd": "nmap -sV {target}", "risk": "low", "network": "tor"},
  {"id": "offline",   "image": "kali", "cmd": "searchsploit {query}", "risk": "low", "network": "none"},
  {"id": "torcurl",   "image": "tor",  "cmd": "curl --socks5-hostname 127.0.0.1:9050 {url}", "risk": "low", "network": "tor"}
]}
"""


def _tool(tid):
    return load_manifest(_M).get(tid)


# ── argv builders: the killswitch is in the flags ─────────────────────────────

def test_firejail_cuts_network_for_offline_tool():
    argv = ex.build_firejail_argv(_tool("offline"), ["searchsploit", "cve"])
    assert "--net=none" in argv                 # kernel-level killswitch
    assert argv[0] == "firejail"
    assert argv[-2:] == ["searchsploit", "cve"]


def test_firejail_no_netnone_for_tor_tool():
    argv = ex.build_firejail_argv(_tool("nmap_scan"), ["proxychains4", "-q", "nmap", "-sV", "x"])
    assert "--net=none" not in argv             # needs host net to reach Tor SOCKS
    for flag in ("--caps.drop=all", "--nonewprivs", "--private-tmp"):
        assert flag in argv


def test_bubblewrap_unshares_net_for_offline_tool():
    argv = ex.build_bubblewrap_argv(_tool("offline"), ["searchsploit", "cve"])
    assert "--unshare-net" in argv              # kernel-level killswitch
    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv and "--tmpfs" in argv


def test_bubblewrap_shares_net_for_tor_tool():
    argv = ex.build_bubblewrap_argv(_tool("nmap_scan"), ["nmap", "x"])
    assert "--unshare-net" not in argv          # shares host net for Tor egress


# ── proxychains wrapping ──────────────────────────────────────────────────────

def test_proxychains_wraps_tor_tool():
    assert ex.maybe_proxychains(_tool("nmap_scan"), ["nmap", "x"]) == ["proxychains4", "-q", "nmap", "x"]


def test_proxychains_skips_offline_tool():
    assert ex.maybe_proxychains(_tool("offline"), ["searchsploit", "x"]) == ["searchsploit", "x"]


def test_proxychains_skips_native_socks_tool():
    # tool already speaks SOCKS → not double-wrapped
    argv = ["curl", "--socks5-hostname", "127.0.0.1:9050", "x"]
    assert ex.maybe_proxychains(_tool("torcurl"), argv) == argv


# ── backend selection ─────────────────────────────────────────────────────────

def test_auto_prefers_docker(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: "/usr/bin/" + b)  # everything present
    sel = ex.select_executor(backend="auto", docker_enabled=True)
    assert isinstance(sel, DockerManager)
    assert sel.name == "docker"


def test_auto_falls_back_to_firejail_when_no_docker(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: None if b == "docker" else "/usr/bin/" + b)
    sel = ex.select_executor(backend="auto", docker_enabled=True)
    assert sel.name == "firejail"


def test_auto_falls_back_to_bubblewrap(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    sel = ex.select_executor(backend="auto", docker_enabled=True)
    assert sel.name == "bubblewrap"


def test_auto_with_nothing_returns_failclosed_null(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: None)
    sel = ex.select_executor(backend="auto", allow_unsandboxed=False)
    assert sel.name == "none"


def test_auto_uses_local_only_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: None)
    assert ex.select_executor(backend="auto", allow_unsandboxed=True).name == "local"


def test_docker_disabled_skips_docker_in_auto(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: "/usr/bin/" + b)  # all present
    sel = ex.select_executor(backend="auto", docker_enabled=False)
    assert sel.name == "firejail"  # docker present but disabled → next best


def test_explicit_local_refused_without_flag():
    assert ex.select_executor(backend="local", allow_unsandboxed=False).name == "none"
    assert ex.select_executor(backend="local", allow_unsandboxed=True).name == "local"


async def test_null_executor_fails_closed():
    sel = ex.NullExecutor("nothing here")
    assert await sel.available() is False
    with pytest.raises(ex.SandboxUnavailable):
        await sel.run_tool(_tool("offline"), ["searchsploit", "x"])


async def test_host_sandbox_missing_binary_fails_closed(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda b: None)  # no firejail, no tool
    with pytest.raises(ex.SandboxUnavailable):
        await ex.FirejailExecutor().run_tool(_tool("offline"), ["searchsploit", "x"])
