"""Integration tests for the ArmoryGatekeeper fail-closed pipeline.

Docker is faked and the vault session is driven directly, so these run with no
Docker, no DB, and (mostly) no native crypto. The unlock/crypto path is covered
separately with ``importorskip('cryptography')``.
"""
import pytest

from armory.docker_manager import ToolResult
from armory.gatekeeper import ArmoryGatekeeper
from armory.manifest import load_manifest
from armory.session import VaultContents
from armory.whitelist import parse_whitelist

# Not a lockdown suite: the lockdown state is pinned INACTIVE here, because
# with no Redis or PostgreSQL it reads UNKNOWN and refuses high-risk actions.
# See `lockdown_inactive` in tests/conftest.py; the policy has its own suites.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")

_MANIFEST = """
{"tools": [
  {"id": "nmap_scan", "image": "kali", "cmd": "nmap -sV -Pn {target}", "risk": "low",  "network": "tor"},
  {"id": "hydra",     "image": "kali", "cmd": "hydra ssh://{target}",   "risk": "high", "network": "tor"},
  {"id": "danger",    "image": "kali", "cmd": "wipe {target}",          "risk": "critical", "network": "none"},
  {"id": "offline",   "image": "kali", "cmd": "searchsploit {query}",   "risk": "low",  "network": "none"}
]}
"""


class FakeDocker:
    """Records the argv it was asked to run and returns a canned result."""

    def __init__(self):
        self.calls = []

    async def available(self):
        return True

    async def run_tool(self, tool, argv, *, timeout=120):
        self.calls.append((tool.id, argv))
        return ToolResult(tool_id=tool.id, exit_code=0, output="scan output here",
                          network=tool.network)


def _unlocked_gk(*, allow_external_api=False, whitelist="localhost\n127.0.0.1\nexample.com\n",
                 require_totp=False, totp_verify=None, totp_enrolled=None):
    gk = ArmoryGatekeeper(vault_dir="/nonexistent", master_key_hash="x",
                          executor=FakeDocker(), allow_external_api=allow_external_api,
                          require_totp_for_high_risk=require_totp,
                          totp_verify=totp_verify, totp_enrolled=totp_enrolled)
    gk.session.unlock(VaultContents(manifest=load_manifest(_MANIFEST),
                                    whitelist=parse_whitelist(whitelist)))
    return gk


async def test_non_owner_forbidden():
    gk = _unlocked_gk()
    res = await gk.execute("nmap_scan", {"target": "localhost"}, is_owner=False)
    assert res["status"] == "forbidden"


async def test_locked_prompts_for_key():
    gk = ArmoryGatekeeper(vault_dir="/nonexistent", master_key_hash="x", executor=FakeDocker())
    res = await gk.execute("nmap_scan", {"target": "localhost"}, is_owner=True)
    assert res["status"] == "locked"
    assert "Master Key" in res["message"]


async def test_off_whitelist_target_refused():
    gk = _unlocked_gk()
    res = await gk.execute("nmap_scan", {"target": "attacker.evil"}, is_owner=True)
    assert res["status"] == "out_of_scope"
    assert gk.executor.calls == []  # nothing ran


async def test_local_target_runs_and_auto_locks():
    gk = _unlocked_gk(allow_external_api=False)
    res = await gk.execute("nmap_scan", {"target": "localhost"}, is_owner=True)
    assert res["status"] == "executed"
    assert res["output"] == "scan output here"
    assert gk.executor.calls[0] == ("nmap_scan", ["nmap", "-sV", "-Pn", "localhost"])
    assert res["locked"] is True
    assert gk.session.is_unlocked() is False  # auto-locked after the task


async def test_external_target_blocked_when_egress_off():
    gk = _unlocked_gk(allow_external_api=False)
    res = await gk.execute("nmap_scan", {"target": "example.com"}, is_owner=True)
    assert res["status"] == "egress_blocked"
    assert gk.executor.calls == []


async def test_external_target_allowed_when_egress_on():
    gk = _unlocked_gk(allow_external_api=True)
    res = await gk.execute("nmap_scan", {"target": "example.com"}, is_owner=True,
                           lock_after=False)
    assert res["status"] == "executed"


async def test_high_risk_needs_approval_confirm_mode():
    # require_totp=False → legacy explicit-confirmation flag path
    gk = _unlocked_gk(require_totp=False)
    pending = await gk.execute("hydra", {"target": "localhost"}, is_owner=True)
    assert pending["status"] == "approval_required"
    assert gk.executor.calls == []
    approved = await gk.execute("hydra", {"target": "localhost"}, is_owner=True,
                                approve_high_risk=True)
    assert approved["status"] == "executed"


async def test_high_risk_totp_flow():
    async def enrolled(_u):
        return True

    async def verify(_u, code):
        return code == "654321"

    gk = _unlocked_gk(require_totp=True, totp_enrolled=enrolled, totp_verify=verify)
    # no code → prompt for 2FA
    r1 = await gk.execute("hydra", {"target": "localhost"}, is_owner=True)
    assert r1["status"] == "approval_required" and "2FA" in r1["message"] or "code" in r1["message"].lower()
    # wrong code → denied, nothing ran
    r2 = await gk.execute("hydra", {"target": "localhost"}, is_owner=True, approval_code="000000")
    assert r2["status"] == "approval_denied"
    assert gk.executor.calls == []
    # correct code → executed
    r3 = await gk.execute("hydra", {"target": "localhost"}, is_owner=True, approval_code="654321")
    assert r3["status"] == "executed"


async def test_high_risk_blocked_when_totp_not_enrolled():
    async def enrolled(_u):
        return False

    gk = _unlocked_gk(require_totp=True, totp_enrolled=enrolled)
    r = await gk.execute("hydra", {"target": "localhost"}, is_owner=True, approval_code="123456")
    assert r["status"] == "approval_unavailable"
    assert gk.executor.calls == []


async def test_low_risk_ignores_totp_requirement():
    # A low-risk tool never needs 2FA even when TOTP enforcement is on.
    gk = _unlocked_gk(require_totp=True)
    res = await gk.execute("nmap_scan", {"target": "localhost"}, is_owner=True)
    assert res["status"] == "executed"


async def test_critical_blocked_outright():
    gk = _unlocked_gk()
    res = await gk.execute("danger", {"target": "localhost"}, is_owner=True,
                           approve_high_risk=True)
    assert res["status"] == "blocked"
    assert gk.executor.calls == []


async def test_offline_tool_ignores_egress_gate():
    gk = _unlocked_gk(allow_external_api=False)
    res = await gk.execute("offline", {"query": "cve-2024"}, is_owner=True)
    assert res["status"] == "executed"


async def test_status_reports_lock_state():
    gk = _unlocked_gk()
    st = gk.status()
    assert st["unlocked"] is True
    assert st["tools_available"] == 4
    await gk.lock()
    assert gk.status()["unlocked"] is False


# ── ARMORY_ENABLED master switch ──────────────────────────────────────────────

async def test_disabled_refuses_everything():
    gk = ArmoryGatekeeper(vault_dir="/nonexistent", master_key_hash="x",
                          executor=FakeDocker(), enabled=False)
    assert (await gk.unlock("k"))["status"] == "disabled"
    assert (await gk.execute("nmap_scan", {"target": "localhost"}, is_owner=True))["status"] == "disabled"
    assert (await gk.preview("nmap_scan", {"target": "localhost"}))["status"] == "disabled"
    assert gk.status()["enabled"] is False
    assert gk.executor.calls == []


# ── Master-Key brute-force lockout ────────────────────────────────────────────

async def test_unlock_lockout_after_max_attempts(tmp_path):
    from armory import vault_encryption as venc
    # A dummy sealed file makes is_provisioned() true; wrong keys never reach
    # decrypt (verify_master_key is stdlib PBKDF2), so no cipher is needed.
    (tmp_path / "vault.sealed").write_bytes(b"dummy-sealed-bytes")
    verifier = venc.hash_master_key("realkey", iterations=1000)
    clock = {"t": 0.0}
    gk = ArmoryGatekeeper(vault_dir=str(tmp_path), master_key_hash=verifier,
                          executor=FakeDocker(), max_unlock_attempts=3,
                          lockout_seconds=100, clock=lambda: clock["t"])
    assert gk.is_provisioned() is True

    assert (await gk.unlock("wrong"))["status"] == "denied"
    assert (await gk.unlock("wrong"))["status"] == "denied"
    tripped = await gk.unlock("wrong")
    assert tripped["status"] == "locked_out"
    # Even the CORRECT key is refused during the lockout window.
    assert (await gk.unlock("realkey"))["status"] == "locked_out"
    assert gk.status()["locked_out"] is True

    clock["t"] += 101  # window elapses
    assert gk.status()["locked_out"] is False
    # counter reset → a wrong key is merely "denied" again, not still locked out
    assert (await gk.unlock("wrong"))["status"] == "denied"


# ── unlock path (needs the AES body cipher) ───────────────────────────────────

async def test_unlock_with_real_vault(tmp_path):
    pytest.importorskip("cryptography")
    from armory import vault as vaultmod
    from armory import vault_encryption as venc

    key = "correct-horse-battery"
    vaultmod.create_from_template(tmp_path, key)
    gk = ArmoryGatekeeper(vault_dir=str(tmp_path),
                          master_key_hash=venc.hash_master_key(key, iterations=1000),
                          executor=FakeDocker())
    assert gk.is_provisioned() is True

    bad = await gk.unlock("wrong-key")
    assert bad["status"] == "denied"
    assert gk.session.is_unlocked() is False

    good = await gk.unlock(key)
    assert good["status"] == "unlocked"
    assert good["tools_available"] >= 20
    assert gk.session.is_unlocked() is True
