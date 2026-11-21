"""Phase 1 — sovereign coding agent: owner-gated, branch-only, local-by-default.

No aider / git / Ollama needed — subprocess.run is dispatched by a fake.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
from types import SimpleNamespace

import pytest

import agents.coding_agent as ca
from agents.coding_agent import is_coding_request, needs_confirmation, run_coding_task

# Not a lockdown suite: the lockdown state is pinned INACTIVE here, because
# with no Redis or PostgreSQL it reads UNKNOWN and refuses high-risk actions.
# See `lockdown_inactive` in tests/conftest.py; the policy has its own suites.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")


@pytest.fixture(autouse=True)
def _isolate_repo_allowlist(monkeypatch):
    """Host configuration must not change unit-test control flow. These tests
    are about the agent once the owner has turned it on; that it is off by
    default is test_coding_and_architect_off_by_default.py's job."""
    monkeypatch.setattr(ca, "_ALLOWED_REPOS", [])
    monkeypatch.setenv("IRA_CODING_AGENT_ENABLED", "true")


# ── Intent detection ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "fix the bug in voice/tts.py",
    "add a test for the transcribe endpoint",
    "refactor the owner gate",
    "find the bug in chat.py",
    "implement a new health endpoint",
])
def test_is_coding_request_positive(q):
    assert is_coding_request(q) is True


@pytest.mark.parametrize("q", [
    "what time is it",
    "fix my dinner reservation for tonight",
    "implement my morning routine",
    "remind me to call mom",
])
def test_is_coding_request_negative(q):
    assert is_coding_request(q) is False


def test_needs_confirmation_flags_danger():
    assert needs_confirmation("delete the repo and redeploy") is not None
    assert needs_confirmation("force-push to main") is not None
    assert needs_confirmation("add a test for the gate") is None


# ── Gates ─────────────────────────────────────────────────────────────────────

def test_non_owner_is_refused():
    res = asyncio.run(run_coding_task("fix the bug in x.py", "/tmp/repo", is_owner=False))
    assert res["ok"] is False and res["reason"] == "owner-gate"


def test_dangerous_intent_needs_confirmation():
    res = asyncio.run(run_coding_task("delete the repo", "/tmp/repo", is_owner=True))
    assert res["ok"] is False and res["reason"] == "needs-confirmation"
    assert res["action"]


# ── Backend selection (local by default, cloud only on an explicit opt-in) ────

def test_aider_cmd_local_uses_ollama(monkeypatch):
    monkeypatch.setattr(ca, "CODER_BACKEND", "local")
    monkeypatch.setattr(ca, "CODER_MODEL", "qwen2.5-coder:14b")
    cmd, env, cloud = ca._aider_cmd("fix it")
    assert cloud is False
    assert "ollama/qwen2.5-coder:14b" in cmd
    assert env.get("OLLAMA_API_BASE", "").startswith("http")
    assert "--yes-always" in cmd and "--no-auto-commits" in cmd


def test_aider_cmd_cloud_backend_uses_the_configured_alias(monkeypatch):
    monkeypatch.setattr(ca, "CODER_BACKEND", "cloud")
    monkeypatch.setattr(ca, "CODER_CLOUD_MODEL", "some-cloud-alias")
    cmd, _env, cloud = ca._aider_cmd("fix it")
    assert cloud is True and "some-cloud-alias" in cmd


def test_cloud_backend_without_a_model_stays_local(monkeypatch):
    """No provider is hard-coded: an unconfigured cloud backend must not guess one."""
    monkeypatch.setattr(ca, "CODER_BACKEND", "cloud")
    monkeypatch.setattr(ca, "CODER_CLOUD_MODEL", "")
    monkeypatch.setattr(ca, "CODER_MODEL", "qwen2.5-coder:14b")
    cmd, env, cloud = ca._aider_cmd("fix it")
    assert cloud is False
    assert "ollama/qwen2.5-coder:14b" in cmd
    assert env.get("OLLAMA_API_BASE", "").startswith("http")


# ── Happy path (branch -> aider -> tests -> commit), all subprocess stubbed ────

def _fake_run(cmd, **kwargs):
    joined = " ".join(cmd)
    if cmd[0] == "git":
        sub = cmd[1]
        if sub == "rev-parse" and "--abbrev-ref" in cmd:
            return SimpleNamespace(returncode=0, stdout="ira/fix-the-bug-123\n", stderr="")
        if sub == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef1234567\n", stderr="")
        if sub == "status":
            return SimpleNamespace(returncode=0, stdout=" M voice/x.py\n?? new_test.py\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if "aider" in cmd[0]:
        return SimpleNamespace(returncode=0, stdout="Applied edits to voice/x.py", stderr="")
    if "pytest" in joined:
        return SimpleNamespace(returncode=0, stdout="6 passed", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_owner_happy_path_branch_and_tests(monkeypatch):
    monkeypatch.setattr(ca, "CODER_BACKEND", "local")
    monkeypatch.setattr(ca.subprocess, "run", _fake_run)
    res = asyncio.run(run_coding_task("fix the bug in voice/x.py", "/tmp/repo", is_owner=True))
    assert res["ok"] is True
    assert res["branch"].startswith("ira/")
    assert res["tests_passed"] is True
    assert "voice/x.py" in res["files_changed"]
    assert res["commit"] and res["backend"] == "local"


def test_failed_branch_create_reported(monkeypatch):
    def _git_fail(cmd, **kwargs):
        if cmd[0] == "git" and cmd[1] == "checkout":
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal: a branch named ... already exists")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ca.subprocess, "run", _git_fail)
    res = asyncio.run(run_coding_task("refactor the gate", "/tmp/repo", is_owner=True))
    assert res["ok"] is False and res["reason"] == "git-branch-failed"
