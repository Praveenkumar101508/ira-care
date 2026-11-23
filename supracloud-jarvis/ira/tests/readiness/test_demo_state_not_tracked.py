"""The demo's local state never enters the repository.

`.demo/` holds whatever the demo scripts generate on one machine: seeded logins,
device tokens, the sealing key, scenario logs and soak reports. Three of those
files were committed once. This ratchet keeps that from happening again, and
keeps the seed from carrying a password anyone could read in the repository.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts"


def _tracked(prefix: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "--", prefix], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_nothing_under_demo_is_tracked():
    assert _tracked(".demo") == []


def test_demo_directory_is_ignored():
    ignore = (ROOT / ".gitignore").read_text()
    assert re.search(r"^\.demo/\s*$", ignore, re.M), ".demo/ must be ignored"


def test_the_seed_has_no_fixed_password_or_key():
    for name in ("demo/seed_demo.py", "demo/api.py", "demo/scenarios.py", "soak_24h.py", "simulate_patient.py"):
        text = (SCRIPTS / name).read_text()
        assert "pass-01" not in text, name
        assert not re.search(r'IRA_CONNECTOR_ENCRYPTION_KEY",\s*"[A-Za-z0-9+/=]{40,}"', text), name
        assert "prepare_environment" in text, f"{name} must take its local values from scripts/demo/_local_state.py"


def test_the_seed_generates_a_password_per_account():
    text = (SCRIPTS / "demo/seed_demo.py").read_text()
    assert "fresh_password()" in text
    assert "write_private(out" in text


def test_local_state_values_are_random_and_private(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_local_state", SCRIPTS / "demo" / "_local_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "KEY_FILE", tmp_path / ".demo" / "connector.key")
    monkeypatch.delenv("IRA_CONNECTOR_ENCRYPTION_KEY", raising=False)
    first = mod.connector_key()
    assert first == mod.connector_key(), "the key is stable until renewed"
    assert first != mod.connector_key(renew=True), "renew replaces it"
    assert (tmp_path / ".demo" / "connector.key").stat().st_mode & 0o077 == 0, "private to the user"
    assert mod.fresh_password() != mod.fresh_password()
    assert len(mod.fresh_password()) >= 20


def test_gitleaks_runs_in_ci_over_the_whole_history():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "scripts/ci/gitleaks.sh" in workflow
    assert "fetch-depth: 0" in workflow, "a shallow clone would scan only the newest commit"
    assert '--log-opts="HEAD"' in (ROOT / "scripts" / "ci" / "gitleaks.sh").read_text(), (
        "the scan covers the history that leads to HEAD, not stale branch refs")
    assert (ROOT / ".gitleaksignore").exists()
