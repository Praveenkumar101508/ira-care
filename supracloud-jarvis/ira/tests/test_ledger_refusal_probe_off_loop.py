"""A Trust Ledger refusal does not hold the event loop while the journal is probed
(#233), and the follow-ups of its review (#250).

#233: when a Class A action is refused because the ledger cannot take a record,
the refusal text asks the fallback journal's probe whether the journal is usable;
that probe ran on the event loop. `ledger_refusal` runs it in a worker thread.

#250: (1) the probe is advisory, so it waits `PROBE_LOCK_TIMEOUT_SECONDS` (0.5 s)
for the journal's shared lock and answers "busy", instead of the writer's 5 s;
(2) the REFUSED audit line is logged before any await, so a request cancelled
during the probe still leaves its trace; (3) the recovery branch, which only
formats text, takes no thread hop; (4) every `ledger_refusal(` call in the
service is awaited (a bare call would hand a coroutine to `is_ledger_refusal`,
which answers False); (5) the loop-stall assertion is relative to the planted
delay, not a wall-clock budget.

Negative controls (recorded in the pull requests): with the synchronous call
restored, the two stall tests fail; with the probe's timeout removed, the busy
test takes the writer's wait and fails; with the audit line moved after the
await, the cancellation test fails; with an un-awaited call planted, the guard
fails.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from trust import journal as J

PROBE_DELAY_S = 2.0
BUDGET_S = PROBE_DELAY_S / 2   # relative: the neighbour must not wait for the probe
_IRA = Path(__file__).resolve().parents[1]


def _entry(seq: int = 1) -> dict:
    return {"kind": "action", "user_id": "owner", "action_id": f"backup_restore-{seq:012x}",
            "task_id": None, "record": {"n": seq}, "tenant_id": str(uuid.UUID(int=1)),
            "reason": "ledger_unavailable"}


async def _refusal_and_a_neighbour() -> tuple[dict, float]:
    """Start a Class A refusal and, on the same loop, time a trivial request."""
    from actions.gated_execution import ledger_refusal

    async def neighbour() -> float:
        await asyncio.sleep(0)            # let the refusal start first
        t = time.perf_counter()
        await asyncio.sleep(0.01)         # a request that needs the loop a few times
        return time.perf_counter() - t

    refusal_task = asyncio.create_task(
        ledger_refusal("backup_restore", action_id=f"backup_restore-{uuid.uuid4().hex[:12]}",
                       recovery=False))
    took = await neighbour()
    outcome = await refusal_task
    return outcome, took


# ── #233: the probe runs off the loop ───────────────────────────────────────
@pytest.mark.asyncio
async def test_a_slow_journal_lock_does_not_stall_the_loop(monkeypatch):
    """The probe really runs, against a journal whose lock is slow."""
    J.open_journal().append(J.ENTRY_RECORD, _entry())
    real_lock = J._lock

    def slow_lock(fd, *, exclusive, timeout=None):
        time.sleep(PROBE_DELAY_S)
        return real_lock(fd, exclusive=exclusive, timeout=timeout)
    monkeypatch.setattr(J, "_lock", slow_lock)

    t0 = time.perf_counter()
    outcome, took = await _refusal_and_a_neighbour()
    assert time.perf_counter() - t0 >= PROBE_DELAY_S, "the slow lock was not on the probe's path"
    assert took < BUDGET_S, f"a request beside the refusal waited {took:.2f} s"
    assert outcome["status"] == "ledger_unavailable" and outcome["executed"] is False


@pytest.mark.asyncio
async def test_a_slow_probe_does_not_stall_the_loop(monkeypatch):
    """The same, with the probe itself replaced: independent of journal set-up."""
    def slow_probe():
        time.sleep(PROBE_DELAY_S)
        return "it could not be checked (Planted)"
    monkeypatch.setattr(J, "probe", slow_probe)

    outcome, took = await _refusal_and_a_neighbour()
    assert took < BUDGET_S, f"a request beside the refusal waited {took:.2f} s"
    assert "Planted" in outcome["detail"] and outcome["break_glass"], "the probe's answer is still in the refusal"
    assert outcome["confirmation_spent"] is False


# ── #250 (1): the probe waits briefly for a held lock ───────────────────────
def test_the_probe_answers_busy_within_its_own_timeout_when_a_writer_holds_the_lock():
    journal = J.open_journal()
    journal.append(J.ENTRY_RECORD, _entry())
    fcntl = J._fcntl
    assert fcntl is not None
    holder = os.open(journal.path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)            # another writer, for 2 s
    released = threading.Event()

    def release_later():
        time.sleep(PROBE_DELAY_S)
        fcntl.flock(holder, fcntl.LOCK_UN); os.close(holder); released.set()
    threading.Thread(target=release_later, daemon=True).start()
    try:
        t0 = time.perf_counter()
        answer = J.probe()
        elapsed = time.perf_counter() - t0
    finally:
        released.wait(PROBE_DELAY_S + 2)
    assert answer and "lock" in answer, answer
    assert elapsed < PROBE_DELAY_S / 2, f"the probe waited {elapsed:.2f} s for the writer"
    assert J.PROBE_LOCK_TIMEOUT_SECONDS < J.LOCK_TIMEOUT_SECONDS


def test_the_writer_still_waits_its_full_timeout(monkeypatch):
    """The probe's short wait is its own: `_lock` without a timeout keeps the
    writer's `LOCK_TIMEOUT_SECONDS`."""
    monkeypatch.setattr(J, "LOCK_TIMEOUT_SECONDS", 0.3)
    journal = J.open_journal()
    journal.append(J.ENTRY_RECORD, _entry())
    fcntl = J._fcntl
    holder = os.open(journal.path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        t0 = time.perf_counter()
        with pytest.raises(J.JournalUnavailable) as err:
            journal.read_state()
        elapsed = time.perf_counter() - t0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); os.close(holder)
    assert elapsed >= 0.3 and "0.3 s" in str(err.value)


# ── #250 (2): the audit line comes before the await ─────────────────────────
@pytest.mark.asyncio
async def test_the_refusal_is_logged_even_when_the_request_is_cancelled_during_the_probe(monkeypatch, caplog):
    from actions.gated_execution import ledger_refusal

    def cancelled_during_probe(**_k):
        raise asyncio.CancelledError
    monkeypatch.setattr(J, "refusal_detail", cancelled_during_probe)
    with caplog.at_level(logging.ERROR, logger="ira.actions.gated"):
        with pytest.raises(asyncio.CancelledError):
            await ledger_refusal("backup_restore", action_id="backup_restore-cancelled01", recovery=False)
    assert any("REFUSED backup_restore (backup_restore-cancelled01)" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ── #250 (3): the recovery branch takes no thread hop ───────────────────────
@pytest.mark.asyncio
async def test_a_recovery_refusal_formats_its_text_without_a_thread(monkeypatch):
    from actions.gated_execution import ledger_refusal

    def no_thread(*_a, **_k):
        raise AssertionError("to_thread was used on the recovery branch")
    monkeypatch.setattr(asyncio, "to_thread", no_thread)
    outcome = await ledger_refusal("backup_restore", action_id="backup_restore-recovery001",
                                   recovery=True, journal_error="disk full (planted)")
    assert outcome["break_glass"] and "disk full (planted)" in outcome["detail"]


# ── #250 (4): every call is awaited ─────────────────────────────────────────
def test_every_ledger_refusal_call_in_the_service_is_awaited():
    bare = []
    for path in sorted(_IRA.rglob("*.py")):
        rel = path.relative_to(_IRA).as_posix()
        if rel.startswith(("tests/", ".venv/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "ledger_refusal" and id(node) not in awaited):
                bare.append(f"{rel}:{node.lineno}")
    assert bare == [], f"ledger_refusal called without await: {bare}"
