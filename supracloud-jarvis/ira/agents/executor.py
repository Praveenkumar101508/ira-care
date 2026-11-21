"""
Executor Agent — runs allowlisted commands inside the container.

Safety model:
  - Allowlist-only: only pre-approved command prefixes are permitted
  - 30-second hard timeout on all executions
  - Minimal environment (no network, restricted PATH), in a private working
    directory: every path a command names must be inside it
  - All exec events logged to the security_events table
  - Output capped at 3 000 chars to prevent LLM context overflow
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from langchain_core.messages import AIMessage

logger = logging.getLogger("ira.executor")

from agents.state import IRAState
from utils.llm import chat_complete
from utils.db import acquire
from utils.cmd_safety import check_command_args as _check_cmd_args  # Fix P6: shared path validator

# Only these command prefixes are ever permitted (read-only, no network, no code execution)
# Public: the Action Guard (trust/action_guard.py) grades shell commands against
# the SAME two gates this executor uses, rather than keeping a second opinion.
ALLOWLIST = frozenset({
    "pytest", "ls", "cat", "echo", "grep",
    "find", "git status", "git log", "git diff",
    "docker ps", "docker stats", "docker logs",
})

#: Allowlisted, but they RUN code: `pytest` imports and runs every test file it
#: finds. Reflexion's code check uses it on purpose, on a draft it wrote to its
#: own temporary directory (agents/reflexion_ground.py). A command the model
#: extracted from a chat message never gets it: from the run directory (then /tmp)
#: that is code anyone able to write a file there chose. Refused since
#: 23 October 2026.
RUNS_CODE = frozenset({"pytest"})

#: Where every executed command runs (`_run_command`), and the only place a
#: model-chosen command may name (`confined_to_run_dir`). A private directory
#: (mode 0700) made for this process, not the shared /tmp: until 23 October 2026
#: a command could read keys or credentials other programs left in /tmp, any
#: file under IRA's own install directory, and, through `git diff --no-index`,
#: any readable file on the machine (an independent review of the R14 fix).
_RUN_DIR_PREFIX = "ira-exec-"


def _remove_run_dir(path: str) -> None:
    """Delete a run directory at exit, and only one this module created. An
    unguarded `shutil.rmtree(RUN_DIR)` deletes whatever RUN_DIR names: pointed
    at the system temporary directory, it emptied all of /tmp (23 October 2026,
    in a planted-defect test of this very fix)."""
    if os.path.basename(os.path.normpath(path)).startswith(_RUN_DIR_PREFIX):
        shutil.rmtree(path, ignore_errors=True)


def _new_run_dir() -> str:
    path = tempfile.mkdtemp(prefix=_RUN_DIR_PREFIX)
    atexit.register(_remove_run_dir, path)
    return path


RUN_DIR = _new_run_dir()


def _private_run_dir() -> str:
    """RUN_DIR, checked before every command: still a real directory, not a
    symlink, owned by this process's user and closed to everyone else. A
    long-running install's directory can be removed (a /tmp cleaner); then a
    fresh one is made, never the old path re-created, because anyone could have
    taken that path in the meantime."""
    global RUN_DIR
    try:
        info = os.lstat(RUN_DIR)
        ours = stat.S_ISDIR(info.st_mode)
        if ours and hasattr(os, "getuid"):          # POSIX: owner and mode
            ours = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
    except OSError:
        ours = False
    if not ours:
        RUN_DIR = _new_run_dir()
    return RUN_DIR

# Patterns that must never appear in any command argument (path restriction)
_RESTRICTED_PATH_PATTERNS = (
    "/proc",
    "/etc/passwd",
    "/etc/shadow",
    ".env",
    ".ssh",
    "/root",
    "/home",
    # Defence in depth behind `confined_to_run_dir`: names that hold keys.
    "id_rsa", "id_ed25519", "id_ecdsa", ".pem", ".git-credentials", ".netrc",
    ".pgpass", "credentials", "secrets",
)


def is_safe_path(arg: str) -> bool:
    """Return True only if the argument contains no restricted path patterns."""
    lower = arg.lower()
    return not any(pat in lower for pat in _RESTRICTED_PATH_PATTERNS)


def confined_to_run_dir(parts: list[str]) -> tuple[bool, str]:
    """(True, "") only if no argument names a place outside RUN_DIR.

    Every argument of every command is checked, not only those of the commands
    the shared validator treats as taking paths: `git` was not one of them, so
    `git diff --no-index /etc/hostname /dev/null` printed any readable file. A
    symlink is resolved before the check. An option that carries a path
    (`--git-dir=…`, `-f/…`) is refused outright, because which options take
    a path differs for every command.
    """
    root = Path(RUN_DIR).resolve()
    command = parts[0].lower() if parts else ""
    for arg in parts[1:]:
        if arg.startswith("-"):
            if "/" in arg or "~" in arg:
                return False, f"an option may not carry a path: {arg!r}"
            if command == "git" and arg.lower().startswith("--no-index"):
                return False, "git --no-index reads files outside any repository"
            continue
        try:
            candidate = Path(arg).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return False, f"unreadable path in argument: {arg!r}"
        if resolved != root and root not in resolved.parents:
            return False, f"outside the executor's own directory: {arg!r}"
    return True, ""


# ── Layer 1: Shell metacharacter rejection ────────────────────────────────────
_SHELL_METACHAR_RE = re.compile(r'[;&|`$(){}]')

def _has_shell_metacharacters(cmd: str) -> bool:
    return bool(_SHELL_METACHAR_RE.search(cmd))


# ── Layer 3: Hard-blocked commands (override allowlist) ───────────────────────
BLOCKED_COMMANDS = frozenset({
    "rm", "mkfs", "dd", "fdisk", "reboot", "shutdown", "halt",
    "poweroff", "init", "kill", "killall", "pkill", "chmod", "chown",
    "passwd", "useradd", "userdel", "visudo", "crontab",
    "iptables", "ufw", "systemctl", "service",
    "wget", "curl",   # use requests/httpx from within IRA instead
})


_SYSTEM = """\
You are the Executor module of IRA — a careful, security-first command executor.

Rules you must ALWAYS follow:
1. Before executing anything, explain exactly what the command will do and why
2. Identify any risks (data loss, network calls, privilege escalation)
3. If the command is not on the allowlist, refuse and suggest a safe alternative
4. After execution, summarise what happened and what changed
5. Never execute anything that could modify production data without explicit confirmation

Allowlisted prefixes: ls, cat, echo, grep, find,
                       git status, git log, git diff, docker ps, docker stats, docker logs

For any command outside the allowlist, respond:
  "Sir, that command requires explicit authorisation. Please confirm: [command] [expected outcome]"\
"""


def is_allowed(command: str) -> bool:
    """On the allowlist as a whole word: `ls` admits `ls -la`, not `lsof`."""
    cmd = " ".join(command.strip().lower().split())
    return any(cmd == prefix or cmd.startswith(prefix + " ") for prefix in ALLOWLIST)


def runs_code(command: str) -> bool:
    """Does this allowlisted command execute code (see RUNS_CODE)?"""
    first = (command.strip().lower().split() or [""])[0]
    return first in RUNS_CODE


# Back-compat aliases — existing callers (and their tests) keep working.
_ALLOWLIST = ALLOWLIST
_BLOCKED_COMMANDS = BLOCKED_COMMANDS
_is_allowed = is_allowed


async def _log_exec_attempt(command: str, allowed: bool, session_id: str) -> None:
    severity = "info" if allowed else "medium"
    description = f"Exec {'allowed' if allowed else 'BLOCKED'}: {command[:200]}"
    async with acquire() as conn:
        await conn.execute(
            """INSERT INTO security_events (severity, event_type, description, metadata)
               VALUES ($1, 'executor', $2, $3)""",
            severity, description, json.dumps({"session_id": session_id}),
        )


def _run_env() -> dict[str, str]:
    """The whole environment a command gets. HOME is the private RUN_DIR, so no
    user's git or tool configuration applies. Git may not look for a repository
    above RUN_DIR (a /tmp/.git that someone else wrote would otherwise configure
    it, and git configuration can name programs to run), and ignores the
    system-wide configuration."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": RUN_DIR,
            "GIT_CEILING_DIRECTORIES": os.path.dirname(RUN_DIR),
            "GIT_CONFIG_NOSYSTEM": "1"}


async def _run_command(command: str, timeout: int = 30) -> tuple[str, int]:
    """
    Execute an allowlisted command in a restricted environment.
    Returns (output_text, returncode).
    """
    try:
        parts = shlex.split(command)
        _private_run_dir()
        loop = asyncio.get_running_loop()
        result: subprocess.CompletedProcess = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=RUN_DIR,
                env=_run_env(),
            ),
        )
        output = (result.stdout or "").strip()
        if result.stderr:
            output += f"\n[stderr] {result.stderr.strip()[:500]}"
        return (output[:3_000] or "(no output)"), result.returncode
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s.", 124
    except FileNotFoundError:
        return f"Command not found: {shlex.split(command)[0]}", 127
    except Exception as e:
        return f"Execution error: {e}", 1


#: The refusal a non-owner gets, in the digital agent's words (agents/digital.py).
OWNER_ONLY = "Only the owner can open applications or run commands on this computer."


async def executor(state: IRAState) -> IRAState:
    # Commands run on the owner's computer, so they are the owner's. The owner
    # gate refuses "run ... command" to anyone else, but the router also sends
    # "run ls -la" here, so this node asks itself who is asking, before the
    # model extracts anything and before an attempt is logged. A state that
    # does not say is not the owner.
    if state.get("is_owner") is not True:
        return {
            **state,
            "final_response": OWNER_ONLY,
            "messages": [AIMessage(content=OWNER_ONLY)],
            "latency_ms": 0,
            "model_used": "executor_gate",
        }

    t0 = time.monotonic()
    query = state["user_query"]

    # Use fast model to extract the exact command
    parse_messages = [
        {
            "role": "system",
            "content": (
                "Extract the exact shell command the user wants to run. "
                "Reply with ONLY the command, nothing else. "
                "If no command is identifiable, reply with: NONE"
            ),
        },
        {"role": "user", "content": query},
    ]
    extracted_cmd = await chat_complete(
        parse_messages, use_deep=False, max_tokens=100, temperature=0
    )
    extracted_cmd = extracted_cmd.strip()

    # Layer 1: Reject shell metacharacters immediately — before any allowlist check
    if extracted_cmd.strip().upper() != "NONE" and _has_shell_metacharacters(extracted_cmd):
        await _log_exec_attempt(extracted_cmd, False, state.get("session_id", "unknown"))
        return {
            **state,
            "final_response": "Command rejected: shell metacharacters are not permitted for security reasons.",
            "messages": [AIMessage(content="Command rejected: shell metacharacters are not permitted for security reasons.")],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "model_used": "qwen3-fast",
        }

    # Layer 3: Block known dangerous commands before allowlist check
    first_token = extracted_cmd.strip().split()[0].lower() if extracted_cmd.strip() else ""
    if first_token in _BLOCKED_COMMANDS:
        await _log_exec_attempt(extracted_cmd, False, state.get("session_id", "unknown"))
        return {
            **state,
            "final_response": f"Command '{first_token}' is blocked for security reasons.",
            "messages": [AIMessage(content=f"Command '{first_token}' is blocked for security reasons.")],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "model_used": "qwen3-fast",
        }

    allowed = (extracted_cmd.strip().upper() != "NONE" and _is_allowed(extracted_cmd)
               and not runs_code(extracted_cmd))

    # Path restriction check (Fix P6): use shared cmd_safety validator for robust path check
    path_safe = True
    if allowed and extracted_cmd.strip().upper() != "NONE":
        try:
            parts = shlex.split(extracted_cmd)
            _private_run_dir()
            ok, reason = _check_cmd_args(parts, base=RUN_DIR,
                                         roots=(Path(RUN_DIR).resolve(),))
            if ok:
                unsafe = next((a for a in parts[1:] if not is_safe_path(a)), None)
                if unsafe is not None:
                    ok, reason = False, f"restricted path in argument: {unsafe!r}"
            if ok:
                ok, reason = confined_to_run_dir(parts)
            if not ok:
                path_safe = False
                logger.warning(f"Executor blocked by path check: {reason!r} cmd={extracted_cmd!r}")
        except ValueError:
            path_safe = False

    if not path_safe:
        allowed = False

    # Lockdown: a command that passed every gate above is still not run. Low
    # risk (only allowlisted read-only commands get here), so an UNKNOWN state
    # lets it through and an ACTIVE lockdown does not.
    lockdown = None
    if allowed:
        from trust.lockdown_enforcement import lockdown_refusal

        lockdown = await lockdown_refusal(risk="low", action_type="shell_command",
                                          is_owner=state.get("is_owner") is True)
        if lockdown is not None:
            allowed = False

    await _log_exec_attempt(extracted_cmd, allowed, state.get("session_id", "unknown"))

    # Execute the command if it passes both allowlist and path checks
    exec_context = ""
    if lockdown is not None:
        exec_context = f"\n\nExecution result: Command NOT run. {lockdown['message']}"
    elif not path_safe and extracted_cmd.strip().upper() != "NONE":
        exec_context = "\n\nExecution result: Command rejected: restricted path"
    elif allowed and extracted_cmd.strip().upper() != "NONE":
        output, returncode = await _run_command(extracted_cmd)
        exec_context = (
            f"\n\nExecution result (exit code {returncode}):\n"
            f"```\n{output}\n```"
        )

    messages = [{"role": "system", "content": _SYSTEM}]
    messages.append({
        "role": "system",
        "content": (
            f"Extracted command: {extracted_cmd}\n"
            f"Allowlist check: {'PASSED — command was executed' if allowed else 'FAILED — command was blocked'}"
            f"{exec_context}"
        ),
    })
    messages.append({"role": "user", "content": query})

    response = await chat_complete(messages, use_deep=False, temperature=0.1)

    latency = int((time.monotonic() - t0) * 1000)
    return {
        **state,
        "final_response": response,
        "messages": [AIMessage(content=response)],
        "latency_ms": latency,
        "model_used": "qwen3-fast",
    }
