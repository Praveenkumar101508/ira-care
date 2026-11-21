"""
Shared command-argument safety validator.  # Fix P6

Two-gate model
--------------
Gate 1 (caller): prefix allowlist — only commands on the allowlist reach gate 2.
Gate 2 (this module): argument path check — reject any path argument that
  resolves outside the allow-roots, contains traversal, glob chars, or shell
  metacharacters.

Usage::

    from utils.cmd_safety import check_command_args
    ok, reason = check_command_args(["cat", "./README.md"])
    if not ok:
        return {"error": reason}
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# ── Allow-roots: arguments resolved outside these directories are rejected ────
def _allow_roots() -> tuple[Path, ...]:
    """Allow-roots resolved at CALL time so a later os.chdir is respected.

    (Previously these were computed once at import; if the process changed working
    directory afterwards, the allow-root pointed at the stale original cwd.)
    """
    return (Path.cwd().resolve(), Path("/tmp").resolve())

# Commands whose arguments should be path-checked (everything else is free-form)
_PATH_ARG_COMMANDS: frozenset[str] = frozenset({
    "cat", "type", "grep", "find", "ls", "dir",
    "docker logs", "head", "tail", "less", "more",
    "wc", "diff", "stat",
})

# `find` primaries that act instead of list: each deletes, runs a program or
# writes a file. `find` is on the executor's read-only allowlist, so any one of
# these turned "list files" into "delete everything under /" (`find .. -delete`
# from the executor's working directory, /tmp).
_FIND_ACTIONS: frozenset[str] = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})

# git options that write a file or run a configured program. `git diff` and
# `git log` are read-only only without them.
_GIT_WRITING_OPTS: tuple[str, ...] = ("--output", "--ext-diff", "--textconv")

# Characters that should never appear in any argument
_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
_SHELL_META_RE = re.compile(r"[;&|`$\\><]")


def _is_under_allow_root(path_str: str, base: str | None = None,
                         roots: tuple[Path, ...] | None = None) -> bool:
    """Return True if path_str resolves to a location under one of _ALLOW_ROOTS.

    A relative path is resolved against `base` when given: the directory the
    command will actually run in. Resolving it against this process's working
    directory instead checked a different path from the one the command reads.
    """
    try:
        candidate = Path(path_str).expanduser()
        if base is not None and not candidate.is_absolute():
            candidate = Path(base) / candidate
        resolved = candidate.resolve()
    except (ValueError, OSError):
        return False
    return any(
        resolved == root or root in resolved.parents
        for root in (roots if roots is not None else _allow_roots())
    )


def check_command_args(parts: list[str], base: str | None = None,
                       roots: tuple[Path, ...] | None = None) -> tuple[bool, str]:
    """
    Validate command arguments.

    Args:
        parts: shlex-split command tokens (parts[0] is the command name).
        base: the directory the command will run in, for resolving relative
            paths. Defaults to this process's working directory.
        roots: the directories a path may resolve into. Defaults to this
            process's working directory and /tmp; the chat executor passes its
            own private directory, which on macOS is not under /tmp.

    Returns:
        (True, "") if safe; (False, rejection_reason) otherwise.
    """
    if not parts:
        return True, ""

    cmd_name = parts[0].lower()
    args = parts[1:]

    if cmd_name == "find":
        acting = next((a for a in args if a.lower() in _FIND_ACTIONS), None)
        if acting is not None:
            return False, f"find {acting} deletes, runs or writes; only listing is allowed"
    if cmd_name == "git":
        writing = next((a for a in args if a.lower().startswith(_GIT_WRITING_OPTS)), None)
        if writing is not None:
            return False, f"git {writing} writes a file or runs a program"

    for arg in args:
        # Reject shell metacharacters in any argument
        if _SHELL_META_RE.search(arg):
            return False, f"Shell metacharacter in argument: {arg!r}"

        # Reject glob characters in any argument
        if _GLOB_CHARS_RE.search(arg):
            return False, f"Glob character in argument: {arg!r}"

        # Path check only for path-taking commands
        if cmd_name not in _PATH_ARG_COMMANDS:
            continue

        # Skip flags (e.g. -n, --name)
        if arg.startswith("-"):
            continue

        # Skip non-path-like args (no / or . prefix → treat as a pattern/name literal).
        # "." and ".." are paths: from /tmp, ".." is "/".
        looks_like_path = (arg in (".", "..") or arg.startswith(("/", "./", "../", "~"))
                           or Path(arg).suffix != "")
        if not looks_like_path:
            continue

        if not _is_under_allow_root(arg, base, roots):
            return False, f"Path outside allowed roots: {arg!r}"

    return True, ""


def safe_check(command_str: str) -> tuple[bool, str]:
    """Convenience wrapper: parse command_str with shlex then call check_command_args."""
    try:
        parts = shlex.split(command_str)
    except ValueError as exc:
        return False, f"Invalid command syntax: {exc}"
    return check_command_args(parts)
