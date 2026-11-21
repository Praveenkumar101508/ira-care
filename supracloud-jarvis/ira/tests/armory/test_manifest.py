"""Unit tests for the Armory manifest + injection-proof command construction."""
import pytest

from armory.manifest import (
    ManifestError,
    load_manifest,
    resolve_command,
    target_args,
)

_GOOD = """
{"tools": [
  {"id": "nmap_scan", "category": "recon", "image": "kali", "cmd": "nmap -sV -Pn {target}", "risk": "low", "network": "tor"},
  {"id": "msf", "category": "exploit", "image": "kali", "cmd": "msfconsole -q -x \\"use {module}; set RHOSTS {target}; run\\"", "risk": "high", "network": "tor"}
]}
"""


def test_loads_and_indexes():
    m = load_manifest(_GOOD)
    assert len(m) == 2
    assert m.ids() == ["msf", "nmap_scan"]
    assert m.get("nmap_scan").risk == "low"


def test_placeholders_ordered_unique():
    m = load_manifest(_GOOD)
    assert m.get("nmap_scan").placeholders() == ["target"]
    assert m.get("msf").placeholders() == ["module", "target"]


def test_resolve_builds_argv_not_shell_string():
    m = load_manifest(_GOOD)
    argv = resolve_command(m.get("nmap_scan"), {"target": "localhost"})
    assert argv == ["nmap", "-sV", "-Pn", "localhost"]


@pytest.mark.parametrize("bad", [
    "localhost; rm -rf /",     # command chaining
    "$(whoami)",               # substitution
    "a b",                     # embedded whitespace / extra token
    "-oN/etc/passwd",          # flag injection (leading dash)
    "`id`",                    # backticks
    "",                        # empty
])
def test_rejects_injection_in_args(bad):
    m = load_manifest(_GOOD)
    with pytest.raises(ManifestError):
        resolve_command(m.get("nmap_scan"), {"target": bad})


def test_missing_and_extra_args_rejected():
    m = load_manifest(_GOOD)
    with pytest.raises(ManifestError):
        resolve_command(m.get("msf"), {"target": "localhost"})  # missing module
    with pytest.raises(ManifestError):
        resolve_command(m.get("nmap_scan"), {"target": "localhost", "bogus": "x"})


def test_embedded_placeholder_token_resolves():
    m = load_manifest('{"tools":[{"id":"h","image":"kali","cmd":"hydra ssh://{target}","risk":"high"}]}')
    argv = resolve_command(m.get("h"), {"target": "example.com"})
    assert argv == ["hydra", "ssh://example.com"]


def test_target_args_only_returns_targets():
    m = load_manifest(_GOOD)
    tool = m.get("msf")
    assert target_args(tool, {"module": "exploit/x", "target": "10.0.0.1"}) == ["10.0.0.1"]


@pytest.mark.parametrize("doc", [
    "{}",                                                        # no tools
    '{"tools": []}',                                             # empty
    '{"tools": [{"id": "X!", "image": "kali", "cmd": "x", "risk": "low"}]}',  # bad id
    '{"tools": [{"id": "x", "image": "windows", "cmd": "x", "risk": "low"}]}',  # bad image
    '{"tools": [{"id": "x", "image": "kali", "cmd": "x", "risk": "nuclear"}]}',  # bad risk
    '{"tools": [{"id": "x", "image": "kali", "cmd": "x", "risk": "low"},'
    '           {"id": "x", "image": "kali", "cmd": "y", "risk": "low"}]}',   # dup id
])
def test_malformed_manifests_rejected(doc):
    with pytest.raises(ManifestError):
        load_manifest(doc)


def test_shipped_template_manifest_is_valid():
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[2] / "armory" / "vault_template" / "manifest.json"
    m = load_manifest(tpl.read_bytes())
    assert len(m) >= 100


def test_every_shipped_tool_resolves_with_clean_args():
    """Each shipped tool builds an argv from clean, single-token args (no
    injection false-rejects, no unresolvable multi-word templates)."""
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[2] / "armory" / "vault_template" / "manifest.json"
    m = load_manifest(tpl.read_bytes())
    dummy = {
        "target": "10.0.0.1", "url": "http://10.0.0.1", "host": "10.0.0.1",
        "username": "bob", "wordlist": "/w.txt", "userlist": "/u.txt",
        "passlist": "/p.txt", "file": "/f.bin", "password": "secret",
        "hash": "deadbeef", "module": "exploit/x", "payload": "linux/x86/shell",
        "query": "apache", "interface": "wlan0", "bssid": "AA:BB:CC:DD:EE:FF",
        "min": "6", "max": "8", "mode": "0", "port": "443",
    }
    for tid in m.ids():
        tool = m.get(tid)
        args = {p: dummy[p] for p in tool.placeholders()}
        assert set(tool.placeholders()) <= set(dummy), f"{tid} uses an untested placeholder"
        argv = resolve_command(tool, args)          # must not raise
        assert argv and all(argv)
