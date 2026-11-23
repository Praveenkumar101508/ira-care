"""Care push goes only to the members it is for (care/push.py)."""
from __future__ import annotations

import pathlib
import re

from care import push

CARE = pathlib.Path(__file__).resolve().parents[2] / "care"


def test_the_care_package_never_uses_the_deployment_wide_sender():
    """The inherited notifier's mobile channel sends to every phone registered
    with the deployment. No module in the care package may reach it."""
    forbidden = re.compile(r"worker\.notifier|worker\.push_mobile|notify_report\(|\bnotify\(|send_push\(|push_mobile\.")
    for path in sorted(CARE.glob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert not forbidden.search(line), f"{path.name}:{n}: {line.strip()}"


def test_the_default_provider_sends_nothing_and_expo_needs_an_explicit_choice():
    assert isinstance(push.provider_from_env({}), push.LogOnlyPushProvider)
    assert isinstance(push.provider_from_env({"IRA_CARE_PUSH_PROVIDER": "expo"}), push.ExpoPushProvider)
    assert isinstance(push.provider_from_env({"IRA_MOBILE_PUSH_ENABLED": "true"}), push.LogOnlyPushProvider), (
        "the inherited flag does not turn care push on")


def test_token_shape():
    assert push.is_valid_token("ExponentPushToken[abc123_-]") and push.is_valid_token("ExpoPushToken[XYZ789]")
    assert not push.is_valid_token("garbage") and not push.is_valid_token("") and not push.is_valid_token("ExponentPushToken[bad token]")


async def test_the_log_only_provider_records_a_ticket_per_phone_and_no_receipts():
    p = push.LogOnlyPushProvider()
    tickets = await p.send([{"to": "ExponentPushToken[a]", "title": "t", "body": "b"}, {"to": "ExponentPushToken[b]", "title": "t", "body": "b"}])
    assert [t.status for t in tickets] == ["logged", "logged"] and len({t.ticket_id for t in tickets}) == 2
    assert await p.receipts([t.ticket_id for t in tickets]) == []


def test_delivery_state_words():
    assert push.delivery_state([]) == "no phone registered"
    assert push.delivery_state([{"status": "logged"}]) == "logged only"
    assert push.delivery_state([{"status": "sent"}, {"status": "failed"}]) == "sent, delivery not yet confirmed"
    assert push.delivery_state([{"status": "sent"}, {"status": "delivered"}]) == "delivered"
    assert push.delivery_state([{"status": "failed"}]) == "failed"


def test_a_message_names_the_alert_and_nothing_private():
    msg = push._message("ExponentPushToken[a]", "IRA CARE alert for Tess", "Please check on Tess now.",
                        {"id": "a1", "kind": "help_requested", "severity": "sos"})
    assert msg["to"] == "ExponentPushToken[a]" and msg["priority"] == "high"
    assert msg["data"] == {"care_alert_id": "a1", "kind": "help_requested", "severity": "sos"}
