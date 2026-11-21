"""The shell executor runs what the model extracted with no confirmation, so its
gates are the only thing between a model's reply and the owner's files.

On 23 October 2026 these all passed every gate:

- `find .. -delete`: the command runs in /tmp, where `..` is `/`, and `..` did
  not look like a path, so it was never checked;
- `find /tmp -delete`, `find . -fprint …`, `git diff --output=…`: deleting or
  writing through a read-only command;
- `pytest`: runs every test file it finds, in /tmp. It stays on the allowlist
  for reflexion's own sandboxed check, and a chat-extracted command never gets it;
- `lsof`: the allowlist matched `ls` as a prefix, not a word.

An independent review the same day found the "no secret reads" claim did not
hold: `cat /tmp/id_rsa` (keys other programs leave in the shared /tmp), any file
under IRA's own install directory by absolute path, and `git diff --no-index
/etc/hostname /dev/null` (any readable file) passed every gate, and so did a
path inside an option. The executor now runs in a private directory and every
path a command names must be inside it.

Nothing here executes a command: the runner is replaced by a recorder.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agents import executor as ex
from trust import action_guard as guard
from utils.cmd_safety import check_command_args


@pytest.mark.parametrize("cmd", ["lsof -i", "git statusx", "cats x", "finder"])
def test_the_allowlist_matches_whole_words(cmd):
    assert not ex.is_allowed(cmd)


def test_pytest_stays_allowlisted_but_is_marked_as_running_code():
    """Reflexion's code check needs it; a chat-extracted command never gets it."""
    assert ex.is_allowed("pytest -q /tmp/reflexion_x")
    assert ex.runs_code("pytest") and ex.runs_code("PyTest tests/")
    assert not ex.runs_code("ls -la")


@pytest.mark.parametrize("cmd", ["ls", "ls -la /tmp", "git status", "git log -5", "docker ps"])
def test_read_only_commands_are_still_allowed(cmd):
    assert ex.is_allowed(cmd)


def _executor_gates(parts):
    """The executor's argument gates, in its order: the shared validator, then
    its own confinement to RUN_DIR."""
    ok, reason = check_command_args(parts, base=ex.RUN_DIR,
                                    roots=(Path(ex.RUN_DIR).resolve(),))
    if ok:
        ok, reason = ex.confined_to_run_dir(parts)
    return ok, reason


@pytest.mark.parametrize("parts", [
    ["find", "..", "-delete"],
    ["find", ".", "-delete"],
    ["find", "/tmp", "-delete"],
    ["find", ".", "-exec", "rm", "-rf", "x", "+"],
    ["find", ".", "-fprint", "/tmp/out"],
    ["find", "..", "-name", "notes"],
    ["git", "diff", "--output=/tmp/x"],
    ["git", "log", "--ext-diff"],
    ["cat", "../etc/passwd"],
])
def test_destructive_or_escaping_arguments_are_refused(parts):
    ok, reason = _executor_gates(parts)
    assert not ok, f"{parts} passed: {reason}"


@pytest.mark.parametrize("parts", [["find", "..", "-name", "notes"], ["cat", "../etc/passwd"]])
def test_the_shared_validator_alone_still_refuses_escapes_from_its_base(parts):
    """`utils.cmd_safety` also serves the Action Guard and `utils.os_tools`, so
    its own `..` handling stays tested, not only the executor's confinement."""
    base = Path(ex.RUN_DIR).parent          # the shared temporary root
    ok, reason = check_command_args(parts, base=str(base))
    assert not ok, f"{parts} passed: {reason}"


_INSTALL_FILE = str(Path.cwd().resolve() / ".git" / "config")


@pytest.mark.parametrize("parts", [
    ["cat", "/tmp/id_rsa"],                                  # a key in the shared /tmp
    ["grep", "-ri", "secret", "/tmp"],
    ["cat", _INSTALL_FILE],                                  # IRA's own tree, by absolute path
    ["grep", "-r", "token", str(Path.cwd().resolve())],
    ["git", "diff", "--no-index", "/etc/hostname", "/dev/null"],  # any readable file
    ["git", "diff", "--no-index", "a", "b"],
    ["grep", "-f/tmp/id_rsa", "-r", "."],                    # a path inside an option
    ["git", "log", "--git-dir=/tmp/x"],
    ["git", "-C", "/tmp", "log", "-p"],
    ["ls", "/"],
    ["cat", "~/notes"],
    ["ls", ".."],
])
def test_nothing_outside_the_executors_own_directory_can_be_read(parts):
    ok, reason = _executor_gates(parts)
    assert not ok, f"{parts} passed: {reason}"


@pytest.mark.parametrize("name", ["id_rsa", "deploy.pem", ".git-credentials", ".netrc",
                                  ".pgpass", "credentials.json", "secrets.yaml"])
def test_names_that_hold_keys_are_refused_anywhere(name):
    assert not ex.is_safe_path(name)


def test_the_run_directory_is_private_and_not_the_shared_tmp():
    assert Path(ex.RUN_DIR).resolve() != Path("/tmp").resolve()
    info = os.stat(ex.RUN_DIR)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.getuid()


def test_a_symlink_out_of_the_run_directory_is_refused():
    link = Path(ex.RUN_DIR) / "escape-probe"
    link.symlink_to("/etc")
    try:
        ok, _ = ex.confined_to_run_dir(["ls", "escape-probe"])
        assert not ok
    finally:
        link.unlink()


def test_a_removed_or_replaced_run_directory_is_never_reused(monkeypatch):
    """A /tmp cleaner can remove the directory of a long-running install, and
    anyone can then create that path. It must be replaced by a fresh one."""
    first = ex._private_run_dir()
    os.rmdir(first)
    second = ex._private_run_dir()
    assert second != first and os.path.isdir(second)
    os.rmdir(second)
    os.mkdir(second, 0o755)                     # the old path, re-created open to others
    third = ex._private_run_dir()
    assert third != second
    assert stat.S_IMODE(os.stat(third).st_mode) == 0o700
    os.rmdir(second)


def test_the_exit_clean_up_removes_only_a_directory_it_made(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "someone-elses-file").write_text("x")
    ex._remove_run_dir(str(shared))
    assert (shared / "someone-elses-file").exists()
    mine = tmp_path / "ira-exec-probe"
    mine.mkdir()
    ex._remove_run_dir(str(mine))
    assert not mine.exists()


def test_commands_get_a_private_home_and_git_stays_in_the_run_directory():
    env = ex._run_env()
    assert env["HOME"] == ex.RUN_DIR
    assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(ex.RUN_DIR)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert set(env) == {"PATH", "HOME", "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_NOSYSTEM"}


@pytest.mark.parametrize("parts", [
    ["find", ".", "-name", "notes"],
    ["find", ".", "-maxdepth", "1"],
    ["cat", "notes.txt"],
    ["git", "diff", "--stat"],
    ["git", "status"],
    ["grep", "error", "app.log"],
    ["ls", "-la"],
    ["echo", "hello", "world"],
])
def test_listing_and_reading_inside_the_run_directory_still_pass(parts):
    ok, reason = _executor_gates(parts)
    assert ok, reason


def test_the_action_guard_grades_them_with_the_same_gates():
    assert guard.classify_command_risk("find .. -delete")[0] == "critical"
    assert guard.classify_command_risk("git diff --output=/tmp/x")[0] == "critical"
    assert guard.classify_command_risk("pytest")[0] == "high"
    assert guard.classify_command_risk("ls -la")[0] == "low"
    assert guard.classify_command_risk("cat /tmp/id_rsa")[0] == "critical"
    assert guard.classify_command_risk("git diff --no-index /etc/hostname /dev/null")[0] == "critical"


@pytest.fixture
def recorder(monkeypatch):
    ran: list[str] = []

    async def fake_run(cmd, timeout=30):
        ran.append(cmd)
        return ("(output)", 0)

    async def no_log(*_a, **_k):
        return None

    monkeypatch.setattr(ex, "_run_command", fake_run)
    monkeypatch.setattr(ex, "_log_exec_attempt", no_log)
    return ran


def _model_says(monkeypatch, command: str):
    replies = iter([command, "summary"])

    async def fake_chat(*_a, **_k):
        return next(replies)

    monkeypatch.setattr(ex, "chat_complete", fake_chat)


def _state(query: str) -> dict:
    return {"user_query": query, "session_id": "s", "messages": [], "is_owner": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "find .. -delete", "find /tmp -delete", "pytest", "lsof -i", "cat /tmp/.env",
    "git diff --output=/tmp/x", "cat /tmp/id_rsa", f"cat {_INSTALL_FILE}",
    "git diff --no-index /etc/hostname /dev/null", "grep -f/tmp/id_rsa -r .",
    "cat .env", "cat id_rsa",        # inside the run directory: only the name check refuses these
])
async def test_the_executor_never_runs_them(monkeypatch, recorder, command):
    _model_says(monkeypatch, command)
    await ex.executor(_state("tidy up the temporary files"))
    assert recorder == [], f"the executor ran {command!r}"


@pytest.mark.asyncio
async def test_the_executor_checks_paths_against_its_own_directory(monkeypatch, recorder):
    """On macOS the temporary directory is not under /tmp, so the shared
    validator's default roots do not cover RUN_DIR; the executor passes its own."""
    from utils import cmd_safety
    monkeypatch.setattr(cmd_safety, "_allow_roots", lambda: (Path("/nonexistent-root"),))
    _model_says(monkeypatch, "find . -name notes")
    await ex.executor(_state("find my notes"))
    assert recorder == ["find . -name notes"]


@pytest.mark.asyncio
async def test_the_executor_still_runs_a_read_only_command(monkeypatch, recorder):
    """The control: the recorder does see a command the gates allow."""
    _model_says(monkeypatch, "ls -la")
    await ex.executor(_state("list the temporary files"))
    assert recorder == ["ls -la"]
