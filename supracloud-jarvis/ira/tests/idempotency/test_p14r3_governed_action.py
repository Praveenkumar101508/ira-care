"""P14-R3 — the governed action path and the notifier, established.

P14-04's dropped third thread named three subjects. This file covers two of
them; `test_p14r3_webhook_intake.py` covers the intake that causes
notifications, and `test_p14r3_checkpoints.py` the third.

    the governed action path, the notifier and the checkpoint writers are not
    established either way.

── The governed action path: PROVED NOT A DEFECT ──────────────────────────

The one-shot confirmation token was already correct, and the reason is worth
recording because it is a property that could be broken by an edit that looks
harmless.

`ApprovalGuardrail.confirm` does:

    pending = self._pending.get(key)
    ...
    del self._pending[key]      # consume BEFORE executing
    result = pending.execute()

There is **no `await` between the `get` and the `del`**. asyncio only switches
tasks at a suspension point, so no second coroutine can observe the entry
between the lookup and its removal. Adding an `await` there — an audit log, a
metric, a permission re-check — would silently make the token spendable twice.

That is a claim about execution, not about source, so the tests below RUN it:
sequential replay, and two coroutines confirming the same token concurrently.

── The notifier: at-least-once, deliberately, and fixed at the source ──────

`notify()` mints a fresh uuid4 per call and fans out to WebSocket, Telegram,
email and Redis. Calling it twice delivers twice, and that is not a defect to
repair inside `notify()`:

* Its channels have no deduplication to offer. SMTP has no idempotency key;
  Telegram's `sendMessage` has none; a Redis publish is fire-and-forget.
  A dedupe cache inside the notifier would suppress the SECOND delivery of a
  genuinely repeated alert — "the disk is still full" — which is information.
* The duplicate is not created by the notifier. It is created by whatever
  called it twice, and only that caller knows whether the second call is a
  retry or a new event.

So the answer is the one P14-R3 implements at the intake: deduplicate where the
external identity is, and never alert on a duplicate. This file pins the
notifier's contract so that the reasoning above cannot quietly become false.
"""
from __future__ import annotations

import asyncio
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from utils.approval import ApprovalGuardrail  # noqa: E402


# ── The one-shot token ─────────────────────────────────────────────────────

async def test_a_confirm_token_is_spent_exactly_once():
    """Replay refused, and the side effect ran once."""
    guard = ApprovalGuardrail()
    runs: list[int] = []

    draft = guard.draft(owner="owner", action="delete", preview="rm f",
                        execute=lambda: runs.append(1))

    first = await guard.confirm(owner="owner", token=draft.token)
    second = await guard.confirm(owner="owner", token=draft.token)

    assert first.executed
    assert second.status == "not_found", (
        f"a spent token was accepted again ({second.status!r}). The approval "
        f"the owner gave once released the action twice.")
    assert len(runs) == 1, f"the side effect ran {len(runs)} times"


async def test_two_concurrent_confirmations_release_the_action_once():
    """THE TEST THIS FILE EXISTS FOR.

    The sequential case above is satisfied by any implementation that deletes
    the entry at some point. This one is not.

    `confirm` looks the token up and deletes it with no `await` in between, so
    the pair is atomic with respect to the event loop. Insert an `await`
    there — an audit write, a metric, a re-check — and both coroutines find the
    entry before either removes it, and a single approval executes twice.

    The executor below yields (`await asyncio.sleep(0)`) so that if such a
    suspension point existed, the interleaving would actually happen rather
    than depending on luck.
    """
    guard = ApprovalGuardrail()
    runs: list[int] = []

    async def _side_effect():
        await asyncio.sleep(0)
        runs.append(1)

    draft = guard.draft(owner="owner", action="transfer", preview="move funds",
                        execute=_side_effect)

    results = await asyncio.gather(
        guard.confirm(owner="owner", token=draft.token),
        guard.confirm(owner="owner", token=draft.token),
    )

    executed = [r for r in results if r.executed]
    assert len(executed) == 1, (
        f"{len(executed)} of 2 concurrent confirmations executed. The token "
        f"lookup and its consumption are no longer atomic — something now "
        f"awaits between them, and one approval releases two side effects.")
    assert len(runs) == 1, (
        f"the side effect ran {len(runs)} times for one approval")


async def test_the_token_is_consumed_even_when_the_action_raises():
    """A failed action must not leave a spendable token behind.

    Otherwise a caller that retries after an error re-spends an approval the
    owner gave once — and the retry is exactly when it would happen.
    """
    guard = ApprovalGuardrail()
    attempts: list[int] = []

    def _boom():
        attempts.append(1)
        raise RuntimeError("the side effect failed")

    draft = guard.draft(owner="owner", action="delete", preview="rm f",
                        execute=_boom)

    with pytest.raises(RuntimeError):
        await guard.confirm(owner="owner", token=draft.token)

    again = await guard.confirm(owner="owner", token=draft.token)
    assert again.status == "not_found", (
        "the token survived a failed action, so a retry re-spends an approval "
        "that was given once. Consuming BEFORE executing is what prevents "
        "this; consuming after would not.")
    assert len(attempts) == 1


async def test_another_owner_cannot_spend_the_token():
    """The negative control on identity, not on repetition."""
    guard = ApprovalGuardrail()
    runs: list[int] = []
    draft = guard.draft(owner="owner", action="delete", preview="rm f",
                        execute=lambda: runs.append(1))

    other = await guard.confirm(owner="someone-else", token=draft.token)

    assert other.status == "not_found"
    assert not runs
    assert (await guard.confirm(owner="owner", token=draft.token)).executed, (
        "the real owner's token stopped working — the guard is refusing "
        "everything, which passes the assertion above for the wrong reason")


# ── The notifier's contract, pinned ────────────────────────────────────────

async def test_notify_is_at_least_once_and_that_is_the_contract():
    """The notifier does NOT deduplicate, and this records why that is right.

    If somebody adds a dedupe cache inside `notify()`, this test fails and asks
    them to read the module docstring above first: the channels have no
    idempotency to offer, a repeated alert is often real information, and the
    duplicate is created by the caller — which is where P14-R3 fixes it.
    """
    from pathlib import Path

    # The FILE, not the imported module. `tests/conftest.py` replaces
    # `worker.notifier` in `sys.modules` with a stub — the real one sends
    # Telegram messages and email — and `inspect.getsource` on a stub raises
    # `TypeError: is a built-in module`. The claim here is about what the
    # repository contains, so read the repository.
    source = (Path(__file__).resolve().parents[2]
              / "worker" / "notifier.py").read_text(encoding="utf-8")
    assert "dedupe" not in source.lower(), (
        "worker/notifier.py now mentions deduplication. That may be right, but "
        "it changes a contract other code depends on: a repeated alert like "
        "'the disk is still full' is information, and suppressing it inside "
        "the notifier hides it from every channel at once. P14-R3 deduplicates "
        "at the INTAKE, where the external identity is. Update this test and "
        "the P14-R3 section of the closure plan together, or not at all.")
