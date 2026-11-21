"""P14-04 — bounded waits, and the outcomes reported for them.

§3.4 of the plan: *"Every wait is bounded, and a timeout is never followed by an
unbounded one — P13-01's finding Z, generalised."*

── The sweep, including what it did NOT find ───────────────────────────────

Outbound HTTP was swept first and came back clean: every `httpx.AsyncClient` in
product code passes a `timeout`, the sync Telegram call passes one, and every
`asyncio.create_subprocess_exec` wraps `communicate()` in `wait_for`. Recorded
because a negative result nobody writes down gets re-derived by the next person,
and because a sweep that only reports hits looks like a sweep that found
everything.

Two `await`s are unbounded **and correct**, for the same reason
`get_pubsub_redis` has no socket timeout: `voice/voice_output.py`'s
`await self._q.get()` and `worker/main.py`'s `await stop_event.wait()` are loops
waiting for their next item of work. A queue with nothing in it has not stalled.

── FINDING AD — the timeout undone on the next line, at the tool sandbox ────

`armory/docker_manager.py::_run` bounded `communicate()` with `wait_for` and
then, on timeout, did:

    proc.kill()
    await proc.wait()          # <- unbounded

SIGKILL is not instantaneous when the child is blocked in an uninterruptible
syscall, and the realistic way for the `docker` CLI to get there is a wedged
daemon socket or a stuck storage driver — **the same condition that made the
command time out in the first place.** The failure mode is correlated with its
own trigger, so the unbounded wait is most likely to be hit exactly when the
timeout mattered. Finding Z's shape, one layer down.

── FINDING AD, second half — the outcome it reported was not established ────

The timeout returned `"container timed out and was killed"`. Both halves could
be false. `proc.kill()` signals the `docker` CLI **client**; with
`docker run --rm` and no `-d`, killing the client does not stop the container,
and `--rm` cleanup is performed by the client that just died — so the container
can be left running *and* left behind.

Nothing could stop it either, because `build_run_argv` passed no `--name`: the
container had no address. The only handle was the process that had just been
killed.

This is an **untrusted tool sandbox**. A container outliving its timeout with a
Tor-shared network namespace is a containment failure, not an untidy one — so
the fix names every container, removes it on timeout with a bounded
`docker rm -f`, and reports whether that actually worked instead of asserting
it.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


# ── FINDING AD: the reap is bounded ─────────────────────────────────────────


class _UnkillableProc:
    """A child that ignores SIGKILL, the way a D-state process does.

    Not a mock of `_kill_and_reap` — a stand-in for the *process*, so the real
    function under test runs unchanged. `kill()` is recorded and does nothing,
    which is precisely what SIGKILL does to a task blocked in an uninterruptible
    syscall: it is delivered and then queued until the syscall returns.
    """

    def __init__(self) -> None:
        self.killed = False
        self.returncode = None

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        await asyncio.Event().wait()      # never returns, exactly as intended

    async def communicate(self):
        await asyncio.Event().wait()


async def test_a_child_that_ignores_sigkill_does_not_hang_the_caller():
    """The finding. Without the bound this test never returns.

    The outer `wait_for` is a guard with a deliberately generous margin — 10 s
    against a 5 s reap — so a failure here reads as "unbounded", not as "slow
    machine".
    """
    from armory import docker_manager

    proc = _UnkillableProc()
    started = asyncio.get_running_loop().time()
    reaped = await asyncio.wait_for(
        docker_manager._kill_and_reap(proc, ["docker", "run", "x"]),
        timeout=10.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert proc.killed, "the child was never signalled, so nothing was injected"
    assert reaped is False, (
        "a child that never exited was reported as reaped, which is the "
        "unconfirmed-outcome shape this phase exists to remove")
    assert elapsed < 8.0, (
        f"the reap took {elapsed:.1f}s against a "
        f"{docker_manager.REAP_TIMEOUT_S}s bound — it is still unbounded")


async def test_the_abandoned_reap_says_so(caplog):
    """A leaked child process must be audible.

    Abandoning the reap is the right call — blocking the caller with a process
    that will not die helps nobody — but it leaks a child, and a leak nobody
    logs is a leak nobody finds.

    The `wait_for` guard is not decoration. Without it, running the plant that
    restores the unbounded reap made this test **hang** rather than fail, and
    the whole suite was killed by an outer timeout — so the plant produced
    "Terminated" instead of a red test naming the property. That is exactly the
    lesson `test_concurrency_and_failover.py` recorded about its own failover
    tests, arriving in the suite written to enforce it: **a test with no
    deadline does not fail, it wedges.**
    """
    from armory import docker_manager

    with caplog.at_level(logging.ERROR, logger="ira.armory.docker"):
        await asyncio.wait_for(
            docker_manager._kill_and_reap(_UnkillableProc(),
                                          ["docker", "run", "x"]),
            timeout=10.0)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "did not exit" in messages
    assert "leaks one child process" in messages, (
        "the log does not say a process was leaked")


async def test_a_child_that_dies_normally_is_reported_as_reaped():
    """The control. Returning False always would satisfy the tests above."""
    from armory import docker_manager

    class _Normal(_UnkillableProc):
        async def wait(self):
            return 0

    proc = _Normal()
    assert await docker_manager._kill_and_reap(proc, ["docker", "x"]) is True
    assert proc.killed


async def test_a_child_already_gone_is_not_an_error():
    """`ProcessLookupError` means it exited between the timeout and the signal.

    Racing with the child's own exit is the ordinary case, not a fault, and
    reporting it as an unreaped process would fire the warning above on every
    borderline timeout until nobody read it.
    """
    from armory import docker_manager

    class _Gone(_UnkillableProc):
        def kill(self):
            raise ProcessLookupError

    assert await docker_manager._kill_and_reap(_Gone(), ["docker", "x"]) is True


# ── FINDING AD second half: the container is addressable and removed ────────


def _tool(network="none"):
    from armory.manifest import Tool

    return Tool(id="t", category="recon", image="kali", cmd="echo {{x}}",
                risk="low", network=network, description="d")


def test_a_named_container_is_addressable():
    """Without `--name` a timed-out container cannot be stopped at all.

    `--rm` covers the container *exiting*, which is exactly the case a timeout
    is not. The only handle used to be the CLI process, and killing that does
    not stop the container it started.
    """
    from armory.docker_manager import DockerManager

    argv = DockerManager().build_run_argv(_tool(), ["echo", "hi"],
                                          name="ira-tool-deadbeef")
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "ira-tool-deadbeef"
    assert "--rm" in argv, "the container no longer cleans up on normal exit"


def test_the_argv_builder_is_still_a_pure_function():
    """The name is passed in, not minted inside, and that is load-bearing.

    `tests/armory/test_docker_manager.py` compares argv lists directly. A uuid
    generated inside `build_run_argv` would make every one of those comparisons
    unwriteable, so a later "tidy-up" that moves the generation in here breaks
    the existing suite — this states why before somebody tries.
    """
    from armory.docker_manager import DockerManager

    dm = DockerManager()
    first = dm.build_run_argv(_tool(), ["echo", "hi"], name="fixed")
    second = dm.build_run_argv(_tool(), ["echo", "hi"], name="fixed")
    assert first == second

    unnamed = dm.build_run_argv(_tool(), ["echo", "hi"])
    assert "--name" not in unnamed, (
        "an unnamed build now injects a name, so the callers that compare argv "
        "lists cannot predict the result")


async def test_a_timeout_removes_the_container_and_says_it_did(monkeypatch):
    """Driven through `run_tool`, which is the real product path.

    Asserting on `_force_remove` alone would prove the helper works and prove
    nothing about whether a timeout ever calls it.
    """
    from armory import docker_manager
    from armory.docker_manager import DockerManager

    calls: list[list[str]] = []

    async def fake_run(argv, *, timeout):
        calls.append(list(argv))
        if argv[:3] == ["docker", "run", "--rm"]:
            return 124, "timed out after 5s; the docker client was killed.", True
        return 0, "", False           # `docker rm -f` and `docker version`

    monkeypatch.setattr(docker_manager, "_run", fake_run)
    monkeypatch.setattr(DockerManager, "available",
                        lambda self: asyncio.sleep(0, result=True))

    result = await DockerManager().run_tool(_tool(), ["echo", "hi"], timeout=5)

    run_argv = [c for c in calls if c[:2] == ["docker", "run"]][0]
    container = run_argv[run_argv.index("--name") + 1]
    assert container.startswith("ira-tool-")

    removals = [c for c in calls if c[:3] == ["docker", "rm", "-f"]]
    assert removals, (
        "a timed-out container was never removed — killing the CLI client does "
        "not stop the container it started")
    assert removals[0][3] == container, (
        f"the removal targeted {removals[0][3]!r}, not the container that ran")

    assert result.timed_out is True
    assert "was removed" in result.output


async def test_a_removal_that_fails_is_not_reported_as_a_removal(monkeypatch):
    """The half that matters most, and the one an optimistic fix gets wrong.

    If `docker rm -f` cannot confirm the container is gone, the result must say
    so. An untrusted tool container outliving its timeout — with a Tor-shared
    network namespace, in the `tor` case — is a containment failure, and
    reporting it as cleaned up is exactly the false-clean shape.
    """
    from armory import docker_manager
    from armory.docker_manager import DockerManager

    async def fake_run(argv, *, timeout):
        if argv[:3] == ["docker", "rm", "-f"]:
            return 1, "Error response from daemon: cannot remove", False
        if argv[:2] == ["docker", "run"]:
            return 124, "timed out", True
        return 0, "", False

    monkeypatch.setattr(docker_manager, "_run", fake_run)
    monkeypatch.setattr(DockerManager, "available",
                        lambda self: asyncio.sleep(0, result=True))

    result = await DockerManager().run_tool(_tool(), ["echo", "hi"], timeout=5)

    assert "could NOT be confirmed removed" in result.output, (
        f"a failed removal was reported as a success: {result.output!r}")
    assert "containment failure" in result.output
    assert result.ok is False


async def test_an_already_gone_container_counts_as_removed(monkeypatch):
    """`docker rm -f` exits non-zero for a container that no longer exists.

    That is the ordinary case — the tool finished just as we gave up — and
    treating it as a failed removal would cry containment failure on every
    borderline timeout until the warning meant nothing.
    """
    from armory import docker_manager
    from armory.docker_manager import DockerManager

    async def fake_run(argv, *, timeout):
        if argv[:3] == ["docker", "rm", "-f"]:
            return 1, "Error: No such container: ira-tool-x", False
        if argv[:2] == ["docker", "run"]:
            return 124, "timed out", True
        return 0, "", False

    monkeypatch.setattr(docker_manager, "_run", fake_run)
    monkeypatch.setattr(DockerManager, "available",
                        lambda self: asyncio.sleep(0, result=True))

    result = await DockerManager().run_tool(_tool(), ["echo", "hi"], timeout=5)
    assert "was removed" in result.output
    assert "could NOT be confirmed" not in result.output


async def test_a_successful_run_removes_nothing(monkeypatch):
    """The control. Removing unconditionally would satisfy the tests above and
    would spend a `docker rm -f` on every single tool invocation."""
    from armory import docker_manager
    from armory.docker_manager import DockerManager

    calls: list[list[str]] = []

    async def fake_run(argv, *, timeout):
        calls.append(list(argv))
        return 0, "done", False

    monkeypatch.setattr(docker_manager, "_run", fake_run)
    monkeypatch.setattr(DockerManager, "available",
                        lambda self: asyncio.sleep(0, result=True))

    result = await DockerManager().run_tool(_tool(), ["echo", "hi"], timeout=5)

    assert result.ok is True
    assert not [c for c in calls if c[:3] == ["docker", "rm", "-f"]], (
        "a successful run issued a removal, so the timeout path's removal "
        "proves nothing")


async def test_the_timeout_message_no_longer_claims_the_container_was_killed():
    """The exact claim that was wrong, asserted by EXECUTION.

    `"container timed out and was killed"` asserted two things nobody had
    established — the shape finding X had, an unconfirmed outcome recorded as a
    clean one — and it is worth pinning by its text, because that string is what
    an operator reads.

    The first version of this test read `inspect.getsource(_run)` and searched
    for the phrase. It failed against the correct code, because the comment
    explaining the fix **quotes the old string**. A syntactic check that trips
    over its own documentation is P13-01's lesson repeated: run the code and
    look at what it returns.
    """
    from armory import docker_manager

    class _Hang:
        returncode = None

        def kill(self):
            pass

        async def wait(self):
            return 0

        async def communicate(self):
            await asyncio.Event().wait()

    async def fake_exec(*a, **k):
        return _Hang()

    real_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    try:
        code, output, timed_out = await docker_manager._run(
            ["docker", "run", "x"], timeout=0.05)
    finally:
        asyncio.create_subprocess_exec = real_exec

    assert (code, timed_out) == (124, True)
    assert "container timed out and was killed" not in output, (
        f"the timeout is again claiming the container was killed; killing the "
        f"docker CLI client does not stop the container: {output!r}")
    assert "reported separately" in output, (
        "the message does not tell the reader that the container's fate is a "
        "separate question")


# ── The sweep's negative results, recorded rather than assumed ──────────────


def test_every_outbound_http_client_in_product_code_is_bounded():
    """The clean part of the sweep, executed so it cannot rot.

    A future `httpx.AsyncClient()` with no timeout inherits httpx's default,
    which is 5 s — not unbounded, but not chosen either. The point of this test
    is that the choice stays explicit and visible at the call site.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    unbounded: list[str] = []
    for path in root.rglob("*.py"):
        if any(p in path.parts for p in ("tests", ".venv", "scripts")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"httpx\.(?:Async)?Client\(", text):
            # The construction may span lines; look at the balanced call.
            tail = text[match.end():match.end() + 400]
            depth, chunk = 1, ""
            for ch in tail:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                chunk += ch
            if "timeout" not in chunk:
                line = text[:match.start()].count("\n") + 1
                unbounded.append(f"{path.relative_to(root)}:{line}")

    assert unbounded == [], (
        f"outbound HTTP clients with no explicit timeout: {unbounded}. httpx's "
        f"default is 5s, so this is not an unbounded wait — but an unstated "
        f"timeout is an unreviewed one, and this sweep exists so the choice "
        f"stays at the call site.")


def test_every_async_subprocess_bounds_its_communicate():
    """The other half of the sweep. `communicate()` has no default timeout at
    all, so an unwrapped one really is unbounded."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if any(p in path.parts for p in ("tests", ".venv", "scripts")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"await\s+(\w+)\.communicate\(\)", text):
            start = text.rfind("\n", 0, match.start())
            window = text[max(0, start - 200):match.end()]
            if "wait_for" not in window:
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}")

    assert offenders == [], (
        f"`communicate()` without a bound: {offenders}. It has no default "
        f"timeout, so this waits forever on a child that never closes its "
        f"pipes.")


# ══ FINDING AE — a rotated log silently ended security monitoring ═══════════
#
# The third P14-04 thread: checkpoint durability. P14-01b's finding T fixed the
# business monitor's checkpoint; `worker/security_monitor.py` had two more of
# the same family and they had never been examined.
#
# `offset = int(row["value"]) if row else 0` followed by `f.seek(offset)`.
# Seeking past EOF is legal and does not error — `readline()` just returns
# nothing — and the offset is only written back `if new_offset > offset`, which
# after a rotation is never true again. So the monitor read ZERO LINES FOREVER,
# with no error, no exception and no warning.
#
# Brute-force detection, the 401 sweep and the SSH failure alerts all stop, and
# a dashboard of zeroes reads as "no attacks". Logs rotate daily on any ordinary
# Linux host, so this was not a corner case; it was the second day.


def _stub_psutil(monkeypatch):
    """`security_monitor` imports psutil at module scope and it is not in the
    unit environment. Same helper as P13-01's suite, same reason."""
    import sys
    import types

    if "psutil" not in sys.modules:
        monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))


def test_a_rotated_log_resumes_from_the_start_instead_of_reading_nothing(
        tmp_path, monkeypatch, caplog):
    """The finding, against real files and a real rotation.

    Not simulated with a mock `os.stat`: the file is genuinely replaced, which
    is what `logrotate` does, and the assertion is about what the real
    `_resolve_offset` decides from a real `st_ino` and `st_size`.
    """
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _encode_offset, _resolve_offset

    log = tmp_path / "auth.log"
    log.write_text("x" * 5000, encoding="utf-8")
    stored = _encode_offset(str(log), 5000)

    # Steady state: nothing has moved, so the stored offset stands.
    offset, origin = _resolve_offset(str(log), stored, key="ssh_log_offset")
    assert (offset, origin) == (5000, OffsetOrigin.STORED)

    # logrotate: the old file is moved aside and a new, smaller one appears.
    log.unlink()
    log.write_text("Failed password for root from 1.2.3.4\n" * 3, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="ira.security"):
        offset, origin = _resolve_offset(str(log), stored, key="ssh_log_offset")

    assert origin is OffsetOrigin.ROTATED, (
        "a rotated log was treated as the same file, so the monitor will seek "
        "past its end and read nothing on every scan from now on")
    assert offset == 0

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "ROTATED" in messages
    assert "read NOTHING" in messages, (
        "the log does not say what the consequence of not doing this would be")


def test_a_truncate_in_place_rotation_is_also_caught(tmp_path, monkeypatch):
    """`logrotate copytruncate` keeps the inode and resets the size to zero.

    The inode check alone would miss this entirely, which is why there are two
    signals rather than one. Worth its own test because it is the configuration
    a lot of deployments actually use.
    """
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _encode_offset, _resolve_offset

    log = tmp_path / "access.log"
    log.write_text("y" * 9000, encoding="utf-8")
    stored = _encode_offset(str(log), 9000)
    inode_before = os.stat(log).st_ino

    with open(log, "w") as handle:       # truncate in place: same inode
        handle.write("short\n")
    assert os.stat(log).st_ino == inode_before, (
        "the fixture replaced the file instead of truncating it, so this test "
        "is checking the inode path and not the size path")

    offset, origin = _resolve_offset(str(log), stored, key="nginx_log_offset")
    assert (offset, origin) == (0, OffsetOrigin.ROTATED)


def test_a_bare_integer_offset_still_works(tmp_path, monkeypatch):
    """Every existing deployment stores a bare integer.

    Rejecting it would restart every log from the beginning on upgrade — a
    self-inflicted alert storm, and the kind of migration that gets reverted.
    The size check still protects it; only the inode check is unavailable.
    """
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _resolve_offset

    log = tmp_path / "access.log"
    log.write_text("z" * 4000, encoding="utf-8")

    assert _resolve_offset(str(log), "1500", key="k") == (1500, OffsetOrigin.STORED)
    # …and a bare integer past the end of the file is still caught by size.
    assert _resolve_offset(str(log), "9999", key="k") == (0, OffsetOrigin.ROTATED)


def test_a_corrupt_offset_restarts_rather_than_wedging(tmp_path, monkeypatch,
                                                       caplog):
    """Finding T's direction, applied to the same shape in a different module.

    `int("garbage")` used to raise straight out of the function. Restarting from
    zero re-reads the log and may re-alert; that is the owner's stated
    preference — *duplicate alerts beat silently losing the window* — and the
    log says so rather than leaving it to be inferred.
    """
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _resolve_offset

    log = tmp_path / "access.log"
    log.write_text("data\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="ira.security"):
        offset, origin = _resolve_offset(str(log), "not-a-number", key="nginx_log_offset")

    assert (offset, origin) == (0, OffsetOrigin.CORRUPT)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "UNPARSEABLE" in messages
    assert "not-a-number" in messages, (
        "the value that would not parse is the only evidence and was not logged")


def test_a_cold_start_is_not_reported_as_corruption(tmp_path, monkeypatch,
                                                    caplog):
    """No row is a first run, not a fault. Same distinction as P14-01b's finding T."""
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _resolve_offset

    log = tmp_path / "access.log"
    log.write_text("data\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="ira.security"):
        offset, origin = _resolve_offset(str(log), None, key="nginx_log_offset")

    assert (offset, origin) == (0, OffsetOrigin.COLD_START)
    assert not [r for r in caplog.records if r.name == "ira.security"], (
        "a cold start logged an error, so a real rotation would be lost in the "
        "noise")


def test_an_unrotated_log_keeps_its_offset(tmp_path, monkeypatch):
    """The control. Returning 0 always would satisfy every test above and would
    re-read the whole log on every single scan — an alert storm every minute."""
    _stub_psutil(monkeypatch)
    from worker.security_monitor import OffsetOrigin, _encode_offset, _resolve_offset

    log = tmp_path / "access.log"
    log.write_text("a" * 100, encoding="utf-8")
    stored = _encode_offset(str(log), 100)

    with open(log, "a") as handle:       # the ordinary case: the log grew
        handle.write("b" * 50)

    assert _resolve_offset(str(log), stored, key="k") == (100, OffsetOrigin.STORED)


def test_the_stored_form_carries_the_inode(tmp_path, monkeypatch):
    """Without the inode, rotation is only detectable while the new file is
    still smaller than the old offset — which on a busy log is a short window
    and on a quiet one is a long one, but on neither is it reliable."""
    _stub_psutil(monkeypatch)
    from worker.security_monitor import _encode_offset, _parse_offset

    log = tmp_path / "access.log"
    log.write_text("a" * 10, encoding="utf-8")

    encoded = _encode_offset(str(log), 10)
    offset, inode = _parse_offset(encoded)
    assert offset == 10
    assert inode == os.stat(log).st_ino, (
        "the stored offset does not carry the inode it belongs to")


def test_both_security_monitor_offsets_go_through_the_resolver(monkeypatch):
    """Two call sites, one fix. The SSH one is easy to forget.

    `auth.log` rotates on the same schedule as the nginx log, so leaving either
    on the old `int(row["value"])` path leaves half the monitoring blind.
    """
    _stub_psutil(monkeypatch)
    import inspect

    from worker import security_monitor

    # `_check_ssh_failures_file`, not `_check_ssh_failures`: the latter
    # delegates to a journald reader or this one, and the offset lives in the
    # file reader. Naming the wrong function is how this test would pass while
    # asserting nothing — the first draft did exactly that and said so loudly,
    # which is the behaviour a tree-scanning test has to have.
    for func in (security_monitor._read_new_log_lines,
                 security_monitor._check_ssh_failures_file):
        source = inspect.getsource(func)
        assert "_resolve_offset(" in source, (
            f"{func.__name__} does not resolve its offset, so a rotation there "
            f"still ends monitoring silently")
        assert "_encode_offset(" in source, (
            f"{func.__name__} writes a bare offset back, so the inode is lost "
            f"and the next rotation is undetectable")
        assert 'int(row["value"])' not in source, (
            f"{func.__name__} is back on the raw parse that finding AE names")
