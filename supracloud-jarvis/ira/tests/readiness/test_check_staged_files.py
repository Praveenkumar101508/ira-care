"""scripts/ci/check_staged_files.py refuses what must not be committed, from the staged index.

Run against a throwaway repository in a temporary folder, as a person would run it before a
commit: a clean change passes, and each kind of bad change (dataset file, weights, environment
file, oversize file, credential) fails with the path and rule but never the value.
"""
from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_staged_files.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), *extra],
                          capture_output=True, text=True)


def _stage(repo: Path, rel: str, data: bytes | str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    _git(repo, "add", "-f", rel)


def test_clean_change_passes(repo):
    _stage(repo, "docs/notes.md", "A plain note with a digest sha256 = " + "ab" * 32 + "\n")
    _stage(repo, "deploy/.env.example", "IRA_SECRET_KEY=change-me\n")
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 staged file(s) checked" in r.stdout


def test_nothing_staged_passes(repo):
    r = _run(repo)
    assert r.returncode == 0 and "0 staged file(s)" in r.stdout


@pytest.mark.parametrize("rel", [
    "datasets/weda/fall_01.txt",
    "scripts/training/kfall_windows.csv",
    "docs/ai/fall/sisfall.zip",
    "watch/app/src/debug/assets/fall_model.tflite",
    "models/adapter_model.safetensors",
    "deploy/.env",
    "docs/private/notes.md",
])
def test_forbidden_paths_fail(repo, rel):
    _stage(repo, rel, "x\n")
    r = _run(repo)
    assert r.returncode == 1
    assert rel in r.stdout


def test_oversize_file_fails(repo):
    _stage(repo, "docs/big.md", "a" * (1024 * 1024 + 1))
    r = _run(repo)
    assert r.returncode == 1 and "docs/big.md" in r.stdout and "limit" in r.stdout


def test_credential_fails_and_is_not_printed(repo):
    value = secrets.token_urlsafe(32).replace("-", "Q").replace("_", "z") + "7Xk"
    _stage(repo, "deploy/settings.py", f'WEBHOOK_SECRET = "{value}"\n')
    r = _run(repo)
    assert r.returncode == 1
    assert "deploy/settings.py:1" in r.stdout
    assert value not in r.stdout + r.stderr


def test_staged_content_is_checked_not_the_working_copy(repo):
    value = secrets.token_urlsafe(32).replace("-", "Q").replace("_", "z") + "7Xk"
    _stage(repo, "deploy/settings.py", f'API_KEY = "{value}"\n')
    (repo / "deploy/settings.py").write_text("API_KEY = ''\n")  # fixed on disk but not re-staged
    assert _run(repo).returncode == 1


def test_unstaged_files_are_ignored(repo):
    (repo / "kfall.csv").write_text("1,2,3\n")  # present but never staged
    assert _run(repo).returncode == 0


def test_all_mode_checks_tracked_files(repo):
    _stage(repo, "data.npz", b"\0\1\2")
    _git(repo, "commit", "-q", "-m", "bad")
    assert _run(repo).returncode == 0, "nothing staged any more"
    r = _run(repo, "--all")
    assert r.returncode == 1 and "data.npz" in r.stdout


def test_this_repository_passes_in_all_mode():
    r = subprocess.run([sys.executable, str(SCRIPT), "--all"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
