"""Nothing that must stay private is tracked in this public repository (docs/security/PUBLIC_REPOSITORY.md).

The repository became public (Apache 2.0) on 26 November 2026, and history is never rewritten,
so these checks guard what is tracked now and what can be committed next:

- no tracked file is dataset content, an archive, a recording, model weights or an environment
  file, outside the explicit allow-list in scripts/ci/check_staged_files.py;
- no tracked file is larger than the size limit, outside its own allow-listed ceiling;
- .gitignore ignores those patterns, and ignores no tracked file;
- no tracked file carries a credential shape (the content rules of the same script);
- the SisFall-trained v1 fall model is not tracked anywhere, under any name.

Each rule is run once on a deliberately bad input (the negative controls), so a rule that has
stopped matching anything fails here rather than passing in silence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "ci" / "check_staged_files.py"

_spec = importlib.util.spec_from_file_location("check_staged_files", SCRIPT)
check = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_staged_files", check)
_spec.loader.exec_module(check)

#: SHA-256 of the SisFall-trained v1 fall model (docs/ai/MODEL_WEIGHTS.md). Removed from the
#: tree in #99; it is still in public history, which is recorded, not rewritten.
SISFALL_V1_SHA256 = "8182ea37154ab52e1d9383b3de42add2a2bb902cafb38daa6056f767d84ae6cb"


def _tracked() -> list[str]:
    return check.tracked_paths(ROOT)


def _ignored(paths: list[str]) -> set[str]:
    out = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT, input="\n".join(paths),
                         capture_output=True, text=True)
    return set(out.stdout.split())


# ── (a) dataset, archive, weights and environment files ─────────────────────────

def test_no_tracked_file_is_data_weights_or_an_env_file():
    found = [str(f) for p in _tracked() for f in check.path_findings(p)]
    assert found == [], "\n".join(found)


@pytest.mark.parametrize("path", [
    "datasets/sisfall/SA01/D01_SA01_R01.txt",
    "scripts/training/data/raw/kfall.csv",
    "docs/ai/fall/weda_fall.zip",
    "docs/ai/heart/galaxyppg.tar.gz",
    "supracloud-jarvis/ira/care/models/fall_windows.npz",
    "supracloud-jarvis/ira/care/models/adapter_model.safetensors",
    "supracloud-jarvis/ira/care/models/gemma.gguf",
    "watch/app/src/debug/assets/fall_model.tflite",
    "docs/evidence/speech/sample.wav",
    "deploy/.env",
    "deploy/prod.env",
    "supracloud-jarvis/.env.production",
    "docs/investors/deck.md",
    "docs/private/cap_table.md",
])
def test_negative_control_forbidden_paths_are_caught(path):
    assert check.path_findings(path), f"{path} should be refused"


def test_negative_control_allow_list_is_exact():
    # The shipped model is allowed only at its own path; the same file elsewhere is refused.
    assert check.path_findings("watch/app/src/main/assets/fall_model.tflite") == []
    assert check.path_findings("watch/app/src/main/assets/fall_model_v1.tflite")
    assert check.path_findings("deploy/.env.example") == []
    assert check.path_findings("supracloud-jarvis/ira/config/cortex.env.example") == []


def test_every_allow_listed_path_is_tracked():
    tracked = set(_tracked())
    stale = [p for p in check.ALLOWED if p not in tracked]
    assert stale == [], f"allow-list entries for files that are no longer tracked: {stale}"


# ── (b) file size ───────────────────────────────────────────────────────────────

def test_no_tracked_file_is_over_the_size_limit():
    found = [str(f) for p in _tracked() if (ROOT / p).is_file()
             for f in check.size_findings(p, (ROOT / p).stat().st_size)]
    assert found == [], "\n".join(found)


def test_negative_control_oversize_files_are_caught():
    assert check.size_findings("docs/evidence/new_recording.mp4", check.SIZE_LIMIT + 1)
    assert check.size_findings("docs/film/ira_care.mp4", check.SIZE_ALLOWED["docs/film/ira_care.mp4"] + 1)
    assert check.size_findings("docs/README.md", check.SIZE_LIMIT) == []


def test_every_size_allowance_is_tracked():
    tracked = set(_tracked())
    assert [p for p in check.SIZE_ALLOWED if p not in tracked] == []


# ── .gitignore agrees with the script ───────────────────────────────────────────

def test_gitignore_ignores_the_forbidden_patterns():
    samples = ["datasets/x/a.txt", "data/raw/a.txt", "a.zip", "a.tar.gz", "a.7z", "a.rar", "a.csv", "a.npz",
               "a.h5", "a.parquet", "a.mat", "a.edf", "a.wav", "a.flac", "a.safetensors", "a.bin", "a.pt",
               "a.ckpt", "a.gguf", "a.onnx", "a.tflite", "deploy/.env", "prod.env", ".env.production",
               "docs/private/a.md", "docs/confidential/a.md", "docs/investors/a.md"]
    assert sorted(set(samples) - _ignored(samples)) == []


def test_gitignore_keeps_the_allow_listed_files():
    keep = list(check.ALLOWED) + ["deploy/.env.example", "supracloud-jarvis/.env.cloud.example",
                                  "supracloud-jarvis/ira/config/cortex.env.example",
                                  "supracloud-jarvis/ira/editions/private/new_module.py"]
    assert sorted(_ignored(keep)) == []


def test_no_tracked_file_is_ignored():
    out = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.split()
    assert out == [], f"tracked files that .gitignore now ignores (add a `!path` line): {out}"


def test_negative_control_ignore_check_sees_an_ignored_path():
    assert _ignored(["a.npz", "README.md"]) == {"a.npz"}


# ── credentials in tracked content ──────────────────────────────────────────────

def test_no_tracked_file_carries_a_credential_shape():
    found = check.scan(_tracked(), size_of=lambda p: (ROOT / p).stat().st_size if (ROOT / p).is_file() else 0,
                       read=lambda p: (ROOT / p).read_bytes() if (ROOT / p).is_file() else b"")
    assert [str(f) for f in found] == []


def _planted() -> dict[str, tuple[str, str]]:
    """Credential-shaped lines and their values, assembled here so this file holds no literal one."""
    import secrets
    rnd = secrets.token_urlsafe(30).replace("-", "A").replace("_", "b") + "9Zq"
    aws = "AKIA" + "Q" * 4 + "7RZ2" * 3
    ghp = "ghp" + "_" + rnd[:36]
    hook = "https://hooks.slack.com/" + "services/T0000/B0000/" + rnd[:24]
    return {
        "aws": (f"key = '{aws}'", aws),
        "github": (f"token: {ghp}", ghp),
        "private-key": ("-----BEGIN " + "PRIVATE KEY-----", "PRIVATE KEY"),
        "slack-webhook": (f"url = '{hook}'", hook),
        "assigned": (f'IRA_WEBHOOK_SECRET = "{rnd}"', rnd),
    }


@pytest.mark.parametrize("kind", ["aws", "github", "private-key", "slack-webhook", "assigned"])
def test_negative_control_planted_credentials_are_caught(kind):
    line, value = _planted()[kind]
    found = check.content_findings("deploy/settings.py", line)
    assert found, f"{kind} should be found"
    assert all(value not in str(f) for f in found), "the value is never printed"


def test_placeholders_and_digests_are_not_credentials():
    assert check.content_findings("x.py", 'IRA_SECRET_KEY = "change-me-change-me-change-me"') == []
    assert check.content_findings("x.py", 'password = "a long enough passphrase 1"') == []
    assert check.content_findings("x.py", 'token_sha256 = "' + "0123456789abcdef" * 4 + '"') == []
    assert check.content_findings("x.py", 'TOKEN = "ExponentPushToken[family-a-phone-0001]"') == []
    assert check.content_findings("x.py", 'KEY = "' + "Zq7" * 10 + '"  # gitleaks:allow') == []


# ── the SisFall-trained model ───────────────────────────────────────────────────

def files_with_sha256(root: Path, paths: list[str], digest: str) -> list[str]:
    files = [p for p in paths if (root / p).is_file() and (root / p).stat().st_size < 1_000_000]
    return [p for p in files if hashlib.sha256((root / p).read_bytes()).hexdigest() == digest]


def test_sisfall_v1_weights_are_not_tracked_under_any_name():
    assert files_with_sha256(ROOT, _tracked(), SISFALL_V1_SHA256) == []


def test_negative_control_a_renamed_copy_is_found(tmp_path):
    (tmp_path / "renamed.bin").write_bytes(b"stand-in for the v1 weights")
    (tmp_path / "other.txt").write_bytes(b"something else")
    digest = hashlib.sha256(b"stand-in for the v1 weights").hexdigest()
    assert files_with_sha256(tmp_path, ["renamed.bin", "other.txt"], digest) == ["renamed.bin"]
