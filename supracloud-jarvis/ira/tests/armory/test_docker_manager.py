"""Tests for the DockerManager argv builder (killswitch + hardening, pure)."""
import pytest

from armory.docker_manager import IMAGE_MAP, TOR_CONTAINER, DockerManager
from armory.manifest import load_manifest


def _tool(tool_id, manifest):
    return load_manifest(manifest).get(tool_id)


_M = """
{"tools": [
  {"id": "nmap_scan", "image": "kali", "cmd": "nmap -sV {target}", "risk": "low", "network": "tor"},
  {"id": "searchsploit", "image": "kali", "cmd": "searchsploit {query}", "risk": "low", "network": "none"},
  {"id": "tor_check", "image": "tor", "cmd": "curl --socks5-hostname 127.0.0.1:9050 {url}", "risk": "low", "network": "tor"}
]}
"""


def test_network_none_uses_isolated_network():
    dm = DockerManager()
    argv = dm.build_run_argv(_tool("searchsploit", _M), ["searchsploit", "cve-2024"])
    assert "--network" in argv and "none" in argv
    assert f"container:{TOR_CONTAINER}" not in " ".join(argv)


def test_tor_kali_tool_is_proxychained_through_tor():
    dm = DockerManager()
    argv = dm.build_run_argv(_tool("nmap_scan", _M), ["nmap", "-sV", "localhost"])
    assert ["--network", f"container:{TOR_CONTAINER}"] == argv[argv.index("--network"):argv.index("--network")+2]
    # proxychains wraps the tool argv after the image name
    assert "proxychains4" in argv
    assert argv[-3:] == ["nmap", "-sV", "localhost"]


def test_tor_native_socks_tool_not_double_wrapped():
    dm = DockerManager()
    argv = dm.build_run_argv(_tool("tor_check", _M), ["curl", "--socks5-hostname", "127.0.0.1:9050", "x"])
    assert "proxychains4" not in argv  # already speaks SOCKS


def test_hardening_flags_present():
    dm = DockerManager()
    argv = dm.build_run_argv(_tool("searchsploit", _M), ["searchsploit", "x"])
    joined = " ".join(argv)
    for flag in ("--rm", "--cap-drop", "--security-opt", "--read-only",
                 "--memory", "--pids-limit"):
        assert flag in joined
    assert "-v" not in argv  # no volumes → no persistent storage
    assert argv[0:2] == ["docker", "run"]
    assert IMAGE_MAP["kali"] in argv


def test_unknown_image_rejected():
    dm = DockerManager()
    bad = load_manifest('{"tools":[{"id":"x","image":"kali","cmd":"x {target}","risk":"low"}]}').get("x")
    object.__setattr__(bad, "image", "windows")
    with pytest.raises(ValueError):
        dm.build_run_argv(bad, ["x", "y"])
