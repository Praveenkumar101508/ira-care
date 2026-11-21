"""P14-01 — finding P resolved, and FINDING AB found while resolving it.

Phase 13 recorded finding P as a **direction** for the owner: when neither Redis
nor the database can be read, `get_lockdown_state()` returned `False`, and
`False` here does not mean "could not tell" — it means NOT IN LOCKDOWN. Failing
closed would turn a database outage into a full lockdown; failing open lets a
lockdown lift silently. Phase 13 made the silence audible and left the choice.

The owner's Phase 14 instruction settles it by refusing the question's premise:

    Do not convert "could not determine lockdown state" into ordinary `False`.
    Represent uncertainty explicitly. Security-sensitive operations should not
    silently interpret UNKNOWN as "not locked down."

So the boolean is gone. `LockdownState` has three members, and a caller that
wants to fail open and one that wants to fail closed must each say so in their
own code, where the choice is visible.

── FINDING AB — lockdown is an alarm, not a control ────────────────────────

Resolving finding P meant finding its callers. There are none.

`get_lockdown_state()` is defined once in `utils/security_tools.py` and called
by **nothing in product code** — only by tests. `initiate_lockdown()` persists
`LOCKDOWN=1`, writes a CRITICAL row to `security_events` and sends the owner a
Telegram alert. `lift_lockdown()` reverses it. Between those two, **no code path
consults the state to restrict anything.**

The one other place the word appears is `security/owner_gate.py`, and it is a
regex matching the word "lockdown" *in a user's query* — it gates who may
**request** lockdown, not what lockdown does.

So the direction the owner was asked to choose governs a signal nothing reads.
That is recurring failure class 1 (a component with no caller) meeting class 29
(dead code counted as protected), on a control the product presents as an
emergency measure. The tri-state is still the right shape — it is what an
enforcement point will need — but the enforcement point does not exist, and
this file asserts that rather than leaving it implied.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


# ── Finding P: the three states ─────────────────────────────────────────────


def test_unreadable_is_unknown_not_inactive(caplog, monkeypatch):
    """The whole point. Both stores down must NOT report "not locked down"."""
    import utils.security_tools as st
    from utils.security_tools import LockdownState

    def no_redis():
        raise RuntimeError("redis is down")

    def no_db():
        raise RuntimeError("database is down")

    # `get_redis` is imported INSIDE the function body, so patching it on
    # `security_tools` does nothing — the first draft did exactly that and the
    # test passed anyway, because the real `get_redis` raises "not initialised"
    # here and both stores were down for a different reason than the fixture
    # intended. Patched on its own module now, so the failure is the one the
    # test names.
    import utils.redis_client as rc
    monkeypatch.setattr(rc, "get_redis", no_redis, raising=False)
    monkeypatch.setattr(st, "acquire", no_db, raising=False)

    with caplog.at_level(logging.ERROR, logger=st.logger.name):
        state = asyncio.run(st.get_lockdown_state())

    assert state is LockdownState.UNKNOWN, (
        f"both stores were unreadable and the answer was {state!r} — a "
        f"measurement nobody took")
    assert state is not LockdownState.INACTIVE

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "UNREADABLE" in messages
    assert "NOT 'not locked down'" in messages, (
        "the log does not say what the value must not be read as")


def test_unknown_is_not_falsy_by_accident():
    """`UNKNOWN` must not quietly behave like the `False` it replaced.

    A `str` Enum is truthy, so `if await get_lockdown_state():` now reads
    UNKNOWN as engaged rather than as absent — which is the safe direction for
    an accident, and is asserted so it stays deliberate.
    """
    from utils.security_tools import LockdownState

    assert bool(LockdownState.UNKNOWN) is True
    assert bool(LockdownState.INACTIVE) is True, (
        "a member became falsy, so `if state:` silently means something new")
    assert LockdownState.UNKNOWN != LockdownState.INACTIVE
    assert LockdownState.UNKNOWN != False  # noqa: E712 - the point of the test


def test_a_store_that_answers_still_answers(monkeypatch):
    """The control. Returning UNKNOWN always would satisfy the test above."""
    import utils.security_tools as st
    from utils.security_tools import LockdownState

    class _Redis:
        def __init__(self, value):
            self._v = value

        async def get(self, _key):
            return self._v

    import utils.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: _Redis("1"), raising=False)
    assert asyncio.run(st.get_lockdown_state()) is LockdownState.ACTIVE

    monkeypatch.setattr(rc, "get_redis", lambda: _Redis("0"), raising=False)
    assert asyncio.run(st.get_lockdown_state()) is LockdownState.INACTIVE


def test_the_database_fallback_still_answers(monkeypatch):
    """Redis down but the database up is INACTIVE/ACTIVE, never UNKNOWN."""
    import utils.security_tools as st
    from utils.security_tools import LockdownState

    def no_redis():
        raise RuntimeError("redis is down")

    class _Conn:
        async def fetchrow(self, *a, **k):
            return {"value": "1"}

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    import utils.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", no_redis, raising=False)
    monkeypatch.setattr(st, "acquire", lambda: _Acquire(), raising=False)

    assert asyncio.run(st.get_lockdown_state()) is LockdownState.ACTIVE, (
        "a working database fallback reported UNKNOWN")


# ── FINDING AB: CLOSED by P14-R1 ────────────────────────────────────────────


def test_lockdown_state_has_a_product_caller():
    """FINDING AB, closed — and this assertion inverted to match.

    It used to read `assert callers == []`, asserting the emptiness: lockdown
    could be engaged and lifted, and the engaged state restricted nothing. Its
    docstring said it existed to FAIL the day somebody wired an enforcement
    point, "at which moment the finding is closed and this assertion should be
    replaced by one that drives that enforcement".

    P14-R1 wired it, this test fired, and both halves of that instruction were
    carried out. The behavioural half lives in
    `test_p14r1_lockdown_enforcement.py`, which drives `guard_action` itself.
    What remains HERE is the tree property, inverted: a product caller exists.

    Keeping it as a tree scan is deliberate. The behavioural tests prove the
    enforcement works; only this proves it is REACHED FROM PRODUCT CODE. A
    `lockdown_refusal` that passes every behavioural test and is imported by
    nothing is FINDING AB with extra steps, and no amount of mocking notices.

    ── Why it changed on 31 October 2026 ───────────────────────────────────

    It used to accept `trust/lockdown_enforcement.py` as the caller. That file
    is the policy itself, and its only caller, `guard_action`, had no product
    caller either, so this test was green while lockdown refused nothing a
    person could trigger (F65 in docs/product/FEATURE_COMPLETION_MATRIX.md).
    The enforcement module and the state module no longer count. What counts is
    the chokepoint the action routes actually pass through,
    `actions/gated_execution.py`, AWAITING the policy, and both of its public
    halves reaching that await: `stage_action` and `confirm_action`.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    not_a_caller = {"utils/security_tools.py",          # defines the state
                    "trust/lockdown_enforcement.py"}    # defines the policy

    def _awaited_calls(node) -> set[str]:
        names = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
                f = n.value.func
                names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
        return names

    callers = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if "tests" in path.parts or ".venv" in path.parts or rel in not_a_caller:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "lockdown_refusal" not in text:
            continue
        if "lockdown_refusal" in _awaited_calls(ast.parse(text)):
            callers.append(rel)

    assert "actions/gated_execution.py" in callers, (
        f"the action chokepoint does not await lockdown_refusal (product callers "
        f"found: {sorted(callers)}). FINDING AB is RE-OPENED: initiate_lockdown() "
        f"still writes a CRITICAL event and alerts the owner, and the routes "
        f"that send email, delete notes and create events do not consult it.")

    tree = ast.parse((root / "actions" / "gated_execution.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "lockdown_refusal" in _awaited_calls(functions["_lockdown_refuses"])
    for half in ("stage_action", "confirm_action"):
        assert "_lockdown_refuses" in _awaited_calls(functions[half]), (
            f"gated_execution.{half} no longer awaits the lockdown check. "
            + ("A lockdown-time action would stage, mint a token and produce a "
               "Proof Card." if half == "stage_action" else
               "A token staged before the lockdown would spend during it."))


def test_lockdown_still_records_and_alerts():
    """The half that DOES work must not be mistaken for the half that does not.

    `initiate_lockdown` persists the flag, writes a CRITICAL security event and
    notifies the owner. Finding AB is not "lockdown does nothing" — it is
    "lockdown alarms, and nothing consults the alarm".
    """
    import inspect

    import utils.security_tools as st

    source = inspect.getsource(st.initiate_lockdown)
    assert "_set_lockdown_db(True)" in source
    assert "security_events" in source
    assert "critical" in source.lower()
    assert "is_owner" in source, "the owner gate on a destructive control is gone"
