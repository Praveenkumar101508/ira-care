"""The auto-apply denylist decides on where a write lands, not on how the header spells it.

`utils/auto_implement.py::_is_protected_path` matched path components as the diff
header spelt them. That is defeated by any spelling that reaches a protected file
under another name:

    api/routes/../middleware/auth.py     traversal — components never line up
    Security/jwt.py                      case — the same file on macOS and Windows
    ｓecurity/jwt.py, ѕecurity/jwt.py     look-alikes — fullwidth and Cyrillic
    shim/x.py  (shim -> security)        an existing symbolic link
    new file mode 120000                 a link planted now, used by the next apply
    a diff block with no readable header git decides the destination, not the screen

`git apply` refuses writes beyond a symbolic link and paths it cannot stat, and
those refusals are recorded here too — but the denylist is the control that is
meant to say *why*, before git runs, and it must not depend on git's reading of a
path matching its own. The canonical form is NFKC → plain printable ASCII only →
not absolute, not escaping → ``..`` collapsed → resolved against the repository →
case-folded; anything ambiguous is refused outright.

Negative controls (each planted and watched fail): drop ``posixpath.normpath``
*and* the resolve step (either alone still collapses ``..``) → the traversal
cases pass the screen; drop ``casefold`` → the case variants pass;
drop the ``isascii`` refusal → the Cyrillic look-alike passes; drop the
``resolve`` → ``shim/x.py`` passes; drop the mode-120000 refusal → the planted
link is applied; drop the empty-header refusal → the headerless block proceeds.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import utils.auto_implement as auto_implement  # noqa: E402
from utils.auto_implement import (  # noqa: E402
    _is_protected_path,
    apply_implementation,
    extract_changed_files,
)

# ── Spellings that reach a protected file ────────────────────────────────────

TRAVERSAL = [
    "api/routes/../middleware/auth.py",
    "./security/../security/jwt.py",
    "utils/llm/../../security/owner_gate.py",
    "supracloud-jarvis/ira/features/../utils/approval.py",
]

CASE_VARIANTS = [
    "Security/jwt.py",
    "SECURITY/SCOPE_ENFORCER.PY",
    "api/Middleware/auth.py",
    ".GitHub/workflows/test.yml",
    "Utils/Approval.py",
]

SEPARATORS = [
    "security\\jwt.py",
    ".github\\workflows\\test.yml",
    "api\\middleware\\scope.py",
]

LOOK_ALIKES_AND_NOISE = [
    "\uff53ecurity/jwt.py",            # fullwidth s — NFKC folds it to 'security'
    "\u0455ecurity/jwt.py",            # Cyrillic dze — never ASCII, so ambiguous
    "security/jwt\u200b.py",           # zero-width space
    "se\u0301curity/jwt.py",           # combining accent
    "security/jwt.py\n",               # a newline smuggled into the header
    "api/routes/feature.py\x00",       # NUL
]

OUTSIDE_THE_TREE = [
    "/etc/passwd",
    "/home/ira/.ssh/authorized_keys",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "../outside.py",
    "..",
    "features/../../escape.py",
]

STILL_ALLOWED = [
    "api/routes/image_gen.py",
    "features/some_feature.py",
    "utils/llm.py",
    "api/routes/../routes/image_gen.py",   # traversal that lands on an ordinary file
    "docs/notes/Security.md",              # 'security' as a file name, not the directory
    "features/securityish/helper.py",      # a component that merely starts with it
]


@pytest.mark.parametrize("path", TRAVERSAL)
def test_traversal_lands_on_the_protected_file(path):
    assert _is_protected_path(path), f"{path!r} reaches a protected file and was allowed"


@pytest.mark.parametrize("path", CASE_VARIANTS)
def test_case_variants_are_the_same_file_on_a_case_insensitive_disk(path):
    assert _is_protected_path(path), f"{path!r} is the protected file on macOS/Windows"


@pytest.mark.parametrize("path", SEPARATORS)
def test_backslash_separators_are_normalised(path):
    assert _is_protected_path(path)


@pytest.mark.parametrize("path", LOOK_ALIKES_AND_NOISE)
def test_look_alikes_and_control_characters_are_refused(path):
    assert _is_protected_path(path), f"{path!r} has no honest reading and must be refused"


@pytest.mark.parametrize("path", OUTSIDE_THE_TREE)
def test_absolute_and_escaping_paths_are_refused(path):
    assert _is_protected_path(path), f"{path!r} is outside the repository and must be refused"


@pytest.mark.parametrize("path", STILL_ALLOWED)
def test_ordinary_files_are_still_applicable(path):
    assert not _is_protected_path(path), f"{path!r} is an ordinary file and was refused"


def test_extract_changed_files_sees_copies_too():
    diff = (
        "diff --git a/security/jwt.py b/features/jwt_copy.py\n"
        "similarity index 100%\n"
        "copy from security/jwt.py\n"
        "copy to features/jwt_copy.py\n"
    )
    assert extract_changed_files(diff) == ["security/jwt.py", "features/jwt_copy.py"]


# ── Against a real repository: links, modes, whole patches ───────────────────

def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.test", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    """A real git repository with a protected directory and a link into it."""
    r = tmp_path / "repo"
    r.mkdir()
    assert _git(r, "init", "-q", ".").returncode == 0
    (r / "security").mkdir()
    (r / "security" / "x.py").write_text("x = 1\n")
    (r / "api" / "routes").mkdir(parents=True)
    (r / "api" / "routes" / "feature.py").write_text("f = 1\n")
    (r / "README.md").write_text("readme\n")
    os.symlink("security", r / "shim")
    assert _git(r, "add", "-A").returncode == 0
    assert _git(r, "commit", "-q", "-m", "files").returncode == 0
    return r


def _block(diff: str) -> str:
    return "```diff\n" + diff + "```\n"


def _modify(path: str, old: str, new: str) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{old}\n+{new}\n")


def _apply(repo, text: str, dry_run: bool = True):
    return asyncio.run(apply_implementation(text, author_name="t", author_email="t@t.test",
                                            dry_run=dry_run))


def test_a_path_through_an_existing_link_is_its_target(repo):
    assert _is_protected_path("shim/x.py", repo=repo), "shim -> security; the write lands in security/"
    assert not _is_protected_path("api/routes/feature.py", repo=repo)


def test_git_itself_refuses_a_write_beyond_a_link_and_the_screen_says_why_first(repo, monkeypatch):
    monkeypatch.setattr(auto_implement, "_REPO_ROOT", repo)
    patch = repo / "via-link.patch"
    patch.write_text(_modify("shim/x.py", "x = 1", "x = 2"))
    git_says = _git(repo, "apply", "--check", str(patch))
    assert git_says.returncode != 0 and "beyond a symbolic link" in git_says.stderr

    result = _apply(repo, _block(_modify("shim/x.py", "x = 1", "x = 2")))
    assert result.success is False
    assert "protected path" in result.error, result.error   # refused by the screen, not by git


def test_one_protected_file_refuses_the_whole_patch_and_nothing_is_applied(repo, monkeypatch):
    monkeypatch.setattr(auto_implement, "_REPO_ROOT", repo)
    text = _block(_modify("README.md", "readme", "changed")) + _block(_modify("security/x.py", "x = 1", "x = 9"))

    result = _apply(repo, text, dry_run=False)

    assert result.success is False and "protected path" in result.error
    assert set(result.files_changed) == {"README.md", "security/x.py"}
    assert (repo / "README.md").read_text() == "readme\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_a_patch_that_creates_a_link_is_an_unsupported_operation(repo, monkeypatch):
    """Not protected by name — `features/shim` — but a link into the protected tree
    that the next apply, or the running service, would follow."""
    monkeypatch.setattr(auto_implement, "_REPO_ROOT", repo)
    link_diff = (
        "diff --git a/features/shim b/features/shim\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/features/shim\n"
        "@@ -0,0 +1 @@\n"
        "+../security\n"
        "\\ No newline at end of file\n"
    )
    result = _apply(repo, _block(link_diff), dry_run=False)
    assert result.success is False
    assert "symbolic link" in result.error
    assert not (repo / "features" / "shim").exists()


def test_a_block_with_no_readable_header_is_refused(repo, monkeypatch):
    monkeypatch.setattr(auto_implement, "_REPO_ROOT", repo)
    result = _apply(repo, _block("@@ -1 +1 @@\n-x = 1\n+x = 2\n"))
    assert result.success is False
    assert "no readable file header" in result.error


def test_the_control_an_ordinary_change_still_validates(repo, monkeypatch):
    monkeypatch.setattr(auto_implement, "_REPO_ROOT", repo)
    result = _apply(repo, _block(_modify("api/routes/feature.py", "f = 1", "f = 2")))
    assert result.success is True, result.error
    assert result.files_changed == ["api/routes/feature.py"]
