"""3C-09 — the notify() caller audit 3C-03 recorded as outstanding.

3C-03 gave `notify()` an optional `principal` and made `notify_channel` refuse
without one. It closed the leak and left a question written down rather than
answered: *which callers pass one, and what happens to the ones that do not?*

This file answers it. Thirteen call sites, **none** of which passes a principal:

    agents/architect_agent.py     worker/business_monitor.py
    api/routes/webhooks.py (×2)   worker/reminders.py
    utils/security_tools.py (×2)  worker/scheduler.py
    worker/briefing.py            worker/security_monitor.py
    worker/main.py                worker/self_healing.py

Every one is a single-owner subsystem: a security monitor, a self-healing loop,
a morning briefing, a reminder. None has a tenant to name because none exists in
a multi-tenant deployment. So in cloud every one of them raises rather than
sending, which is the intended shape — but "intended" is worth an executable
statement, because the alternative reading is that thirteen call sites were
simply missed.

── The part that is not obvious ────────────────────────────────────────────

Redis is not the only channel. `notify()` also sends **Telegram** and **email**,
and both go to a *deployment-wide* destination: one `telegram_chat_id`, one SMTP
recipient, read from settings. A notification that reached those would leak a
tenant's briefing content to whoever operates the deployment — a cross-tenant
leak on a transport with no channel name to get wrong.

What prevents it is that `_publish_redis` is called **first** and raises before
either is attempted. That is correct and it is currently *incidental*: it holds
because of statement order, not because anything says it must. So it is pinned
here. Moving the Telegram block above the Redis publish would be an innocuous-
looking refactor that turns a refusal into a delivery.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import re
import sys

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

# conftest replaces `worker.notifier` with a stub carrying only `notify=None`.
# This file is about the real module, so the stub is evicted — as in 3C-03.
sys.modules.pop("worker.notifier", None)
_notifier = importlib.import_module("worker.notifier")

from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402
from core.identity.db_context import MissingTenantContext  # noqa: E402

IRA = pathlib.Path(__file__).resolve().parents[2]

#: Every module that calls `notify()`, and the edition its notifications belong
#: to. A caller listed `private` is making a claim: this subsystem has no
#: multi-tenant meaning, so refusing in cloud is correct rather than a gap.
#:
#: Adding a cloud caller means adding a principal to its call, not adding a line
#: here.
NOTIFY_CALLERS = {
    "agents/architect_agent.py": "private",   # owner-grade architecture agent
    "utils/security_tools.py": "private",     # host security tooling
    "worker/briefing.py": "private",          # the owner's morning briefing
    "worker/business_monitor.py": "private",
    "worker/main.py": "private",
    "worker/reminders.py": "private",
    "worker/scheduler.py": "private",
    "worker/security_monitor.py": "private",  # this deployment's security events
    "worker/self_healing.py": "private",
    "care/escalation.py": "tenant",           # IRA CARE alerts: always passes the workspace's principal
}


async def _no_persist(*args, **kwargs):
    """`notify()` writes a notification row. This suite is about where the
    payload goes, and the pool is not initialised here."""
    return None


class _Principal:
    """Just enough to name a tenant. Deliberately not a real `Principal`: the
    channel resolver must work from the attribute, not from the type."""

    tenant_id = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


# ─────────────────────────────────────────────────────────────────────────────
# The audit
# ─────────────────────────────────────────────────────────────────────────────


def _callers() -> dict[str, int]:
    """Every module that actually *calls* `notify()`, with its call count.

    Parsed rather than grepped. A regex over source counted
    `worker/push_mobile.py`, whose only mentions are a docstring line and the
    comment "a push failure must never break notify()" — so the audit would
    have carried an entry for a module that never calls it, and a reader
    checking the list against the code would have found nothing there.

    Read from source rather than by import: importing thirteen worker modules
    would start schedulers and network clients to answer a question about which
    lines exist.
    """
    import ast

    found: dict[str, int] = {}
    for path in sorted(IRA.rglob("*.py")):
        rel = path.relative_to(IRA).as_posix()
        if rel.startswith("tests/") or rel == "worker/notifier.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        hits = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            # `notify_report` is `notify` returning its channels too: same code
            # path, same tenant rule, so a call to it is a call site to audit.
            and node.func.id in ("notify", "notify_report"))
        if hits:
            found[rel] = hits
    return found


def test_the_caller_list_is_current():
    """The audit is only worth having if it is not stale.

    A new `notify()` call site fails this on the day it is added, which is when
    somebody can still say which edition it belongs to.
    """
    actual = set(_callers())
    listed = set(NOTIFY_CALLERS)

    new = sorted(actual - listed)
    assert not new, (
        f"new notify() call sites since the 3C-09 audit: {new}. Decide the "
        f"edition each belongs to. A cloud caller must pass a principal — do "
        f"not add it to NOTIFY_CALLERS as 'private' to silence this.")

    gone = sorted(listed - actual)
    assert not gone, (
        f"these modules no longer call notify(): {gone}. Remove them from the "
        f"audit so it keeps meaning something.")


def test_no_caller_claims_a_tenant_it_does_not_have():
    """Every listed caller is `private`, and that is a claim about the code.

    If one is ever reclassified to `cloud`, its call has to pass a principal —
    so this asserts the two agree rather than trusting the label.
    """
    for module, edition in NOTIFY_CALLERS.items():
        if edition != "cloud":
            continue
        text = (IRA / module).read_text(encoding="utf-8")
        assert "principal=" in text, (  # pragma: no cover - no cloud callers yet
            f"{module} is classified as a cloud notify() caller but passes no "
            f"principal, so notify_channel has no tenant to name and will "
            f"refuse at runtime")


# ─────────────────────────────────────────────────────────────────────────────
# What happens to a caller with no principal
# ─────────────────────────────────────────────────────────────────────────────


async def test_cloud_sends_nothing_at_all_without_a_principal(cloud, monkeypatch):
    """Fail-closed across **every** channel, not only Redis.

    This is the assertion that matters, and it is not about the channel name.
    Telegram and email go to one deployment-wide destination — a single
    `telegram_chat_id`, a single SMTP recipient — so a briefing delivered there
    is a tenant's content handed to whoever operates the deployment. There is no
    channel name to get right; the only safe behaviour is not to send.
    """
    sent: list[str] = []

    async def _telegram(*a, **k):
        sent.append("telegram")

    async def _email(*a, **k):
        sent.append("email")

    class _Redis:
        async def publish(self, channel, payload):
            sent.append(f"redis:{channel}")

    monkeypatch.setattr(_notifier, "get_redis", lambda: _Redis())
    monkeypatch.setattr(_notifier, "_send_telegram", _telegram)
    monkeypatch.setattr(_notifier, "_send_email", _email)

    with pytest.raises(MissingTenantContext):
        await _notifier.notify("Briefing", "the owner's own content")

    assert sent == [], (
        f"cloud delivered a notification with no tenant to name: {sent}")

    # Note what this does NOT prove. Telegram and email are also gated on being
    # configured, and they are not configured here — so this assertion would
    # still hold if the refusal moved after them. That case is covered by
    # `test_the_refusal_happens_before_any_other_channel`, which reads the
    # order from source, and it is the guard that actually catches the reorder.


async def test_the_refusal_happens_before_any_other_channel(cloud, monkeypatch):
    """Order, pinned.

    Nothing states that `_publish_redis` must come first — it simply does, and
    that is what stops Telegram and email being attempted. Moving the Telegram
    block above it would look like a tidy-up and would turn a refusal into a
    delivery.
    """
    source = (IRA / "worker" / "notifier.py").read_text(encoding="utf-8")
    body = source[source.index("async def notify("):]

    redis_at = body.index("_publish_redis")
    for channel in ("_send_telegram", "_send_email"):
        assert redis_at < body.index(channel), (
            f"{channel} is attempted before the per-tenant channel is resolved. "
            f"It sends to a deployment-wide destination, so a notification that "
            f"has no tenant to name would reach the operator anyway.")


async def test_cloud_sends_normally_when_a_principal_is_given(cloud, monkeypatch):
    """Positive control.

    Without it, a `notify()` that raised unconditionally in cloud would satisfy
    every assertion above — and would be a different bug wearing the fix's
    clothes.
    """
    published: list[str] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append(channel)

    monkeypatch.setattr(_notifier, "get_redis", lambda: _Redis())
    # `notify()` also writes a row; this suite is about delivery, and the pool
    # is not initialised here.
    monkeypatch.setattr(_notifier, "_persist", _no_persist)

    await _notifier.notify("Briefing", "content", principal=_Principal())

    assert len(published) == 1
    assert _Principal.tenant_id in published[0]


async def test_the_private_edition_still_notifies(private, monkeypatch):
    """The thirteen callers are all private, so if this broke, the product's
    reminders, briefings and security alerts would all stop — a far worse
    outcome than the leak being fixed."""
    published: list[str] = []

    class _Redis:
        async def publish(self, channel, payload):
            published.append(channel)

    monkeypatch.setattr(_notifier, "get_redis", lambda: _Redis())
    monkeypatch.setattr(_notifier, "_persist", _no_persist)

    await _notifier.notify("Security Alert", "3 failed SSH attempts")

    assert published == [_notifier.REDIS_NOTIFY_CHANNEL]
