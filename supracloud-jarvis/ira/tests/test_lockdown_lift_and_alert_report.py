"""Lifting lockdown lifts it, and the lockdown alert says where it actually went.

Two defects, found by reading the code on 24 October 2026:

1. **"Lift lockdown" engaged lockdown.** `security_guardian` tested the lockdown
   pattern before the lift pattern, and every lift phrase also matches the lockdown
   pattern ("lift lockdown" contains "lockdown"). So the phrase the lockdown alert
   itself tells the owner to say, "IRA, lift lockdown", locked again.
2. **The alert claimed a delivery that may not have happened.** `initiate_lockdown`
   answered "Telegram alert sent to your phone." whether or not Telegram was
   configured, or the send failed.

Negative controls (recorded in the PR): with the old order, the lift tests fail;
with the old message, the "no alert left this computer" test fails; with Telegram
counted before the send, the notifier test fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import agents.security as sec
import utils.security_tools as st

IRA = Path(__file__).resolve().parents[1]


def _real_notifier():
    """The real `worker/notifier.py`: the test conftest stubs `worker.notifier`
    because its delivery side sends real Telegram messages and email."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "worker._notifier_under_test", IRA / "worker" / "notifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier = _real_notifier()

LIFT_PHRASES = [
    "IRA, lift lockdown",
    "lift lockdown",
    "Lift the lockdown please",
    "please remove the lockdown",
    "end the lockdown",
    "disengage lockdown",
    "stop lockdown",
    "remove the lock",
]

LOCK_PHRASES = [
    "lockdown now",
    "IRA, lockdown",
    "initiate lockdown",
    "emergency lock",
    "panic",
    "secure mode please",
    "stop, lockdown now!",
    "Stop! Lockdown now",
    "lockdown, someone is at the door",
]


@pytest.mark.parametrize("phrase", LIFT_PHRASES)
def test_a_lift_phrase_lifts(phrase):
    assert sec._security_intent(phrase) == "lift", (
        f"{phrase!r} must lift lockdown; routing it to lockdown leaves the owner "
        f"unable to lift it from chat or voice")


@pytest.mark.parametrize("phrase", LOCK_PHRASES)
def test_a_panic_phrase_still_locks(phrase):
    assert sec._security_intent(phrase) == "lockdown", (
        f"{phrase!r} must engage lockdown; checking lift first must not swallow it")


def test_the_phrase_the_alert_tells_the_owner_to_say_lifts():
    """Read the phrase out of the alert text itself, so the two cannot drift apart.

    Since 1 November 2026 the alert says to TYPE it: a spoken lift is refused
    (THREAT_MODEL T8), so telling the owner to say it would send them to the one
    channel that cannot lift.
    """
    source = (IRA / "utils" / "security_tools.py").read_text(encoding="utf-8")
    found = re.search(r'Type _\\"(.+?)\\"_ in IRA\'s chat to restore normal operations', source)
    assert found, "the lockdown alert no longer names the phrase that lifts it"
    assert sec._security_intent(found.group(1)) == "lift"


@pytest.mark.parametrize("query, intent", [
    ("scan the network for threats", "scan"),
    ("text my phone: dinner at eight", "dispatch"),
    ("what is the weather", None),
])
def test_other_requests_route_as_before(query, intent):
    assert sec._security_intent(query) == intent


# ── The alert's claim ────────────────────────────────────────────────────────


@pytest.fixture
def quiet_lockdown(monkeypatch):
    """No Redis, no database: only the alert and its wording are under test."""
    async def _noop(*a, **k):
        return None

    class _NoDB:
        async def __aenter__(self):
            raise RuntimeError("no database in this test")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(st, "_set_lockdown_db", _noop)
    monkeypatch.setattr(st, "acquire", lambda: _NoDB())


def _delivered(channels):
    async def _report(*a, **k):
        return "notif-id", list(channels)
    return _report


async def test_no_off_device_alert_is_reported_as_none(quiet_lockdown, monkeypatch):
    monkeypatch.setattr(st, "notify_report", _delivered(["websocket"]))
    result = await st.initiate_lockdown(reason="test", is_owner=True)
    assert result["status"] == "lockdown_active"
    assert result["alert_channels"] == ["websocket"]
    assert "alert sent" not in result["message"].lower()
    assert "No alert left this computer" in result["message"]


async def test_a_telegram_delivery_is_reported(quiet_lockdown, monkeypatch):
    monkeypatch.setattr(st, "notify_report", _delivered(["websocket", "telegram"]))
    result = await st.initiate_lockdown(reason="test", is_owner=True)
    assert "Alert sent by Telegram" in result["message"]


async def test_lifting_reports_where_its_alert_went(quiet_lockdown, monkeypatch):
    monkeypatch.setattr(st, "notify_report", _delivered(["websocket"]))
    result = await st.lift_lockdown(is_owner=True)
    assert result["status"] == "normal"
    assert result["alert_channels"] == ["websocket"]
    assert "No alert left this computer" in result["message"]


async def test_a_non_owner_still_cannot_lock_or_lift(quiet_lockdown, monkeypatch):
    calls = []

    async def _report(*a, **k):
        calls.append(a)
        return "x", ["websocket"]

    monkeypatch.setattr(st, "notify_report", _report)
    assert (await st.initiate_lockdown(reason="t", is_owner=False))["status"] == "forbidden"
    assert (await st.lift_lockdown(is_owner=False))["status"] == "forbidden"
    assert calls == []


# ── The notifier only counts a channel that took the message ────────────────


class _Cfg:
    telegram_bot_token = "t"
    telegram_chat_id = "c"
    smtp_host = ""
    smtp_user = ""


@pytest.fixture
def offline_notifier(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notifier, "_publish_redis", _noop)
    monkeypatch.setattr(notifier, "_persist", _noop)
    monkeypatch.setattr(notifier, "get_settings", lambda: _Cfg())


async def test_a_failed_telegram_send_is_not_counted(offline_notifier, monkeypatch):
    async def _fails(*a, **k):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(notifier, "_send_telegram", _fails)
    _id, channels = await notifier.notify_report("t", "b", priority="critical")
    assert "telegram" not in channels
    assert channels[0] == "websocket"


async def test_a_telegram_send_that_works_is_counted(offline_notifier, monkeypatch):
    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(notifier, "_send_telegram", _ok)
    _id, channels = await notifier.notify_report("t", "b", priority="critical")
    assert "telegram" in channels


async def test_notify_still_returns_only_the_id(offline_notifier, monkeypatch):
    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(notifier, "_send_telegram", _ok)
    notif_id = await notifier.notify("t", "b")
    assert isinstance(notif_id, str) and len(notif_id) == 36
