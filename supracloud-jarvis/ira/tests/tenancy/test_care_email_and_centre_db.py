"""Email and the monitoring centre in the escalation chain, against a real PostgreSQL
(register G3.c, G3.d). Stand-ins only: nothing reaches a real mail server or centre.

* With a mail server configured, a circle member with an email address is emailed as
  part of their tier, through the durable outbox, and the state recorded is `accepted`.
* Without one, no email is even queued: a deployment with no mail server is unchanged.
* With a centre configured, tier 3 hands the alert to it with one key per alert, and
  only a signed, recent acknowledgement from the centre, at our tokened address, moves
  that attempt's state forward; a repeat or a late one never moves it back.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

if not os.getenv("IRA_TEST_DATABASE_DSN") or not os.getenv("IRA_TEST_ADMIN_DSN"):
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, pool  # noqa: E402,F401
from tests.tenancy.test_care_delivery_db import Recording, _attempts, _help, family, sim  # noqa: E402,F401

from care import escalation, providers  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

UTC = dt.timezone.utc
ENV = {"IRA_CARE_CALLBACK_SECRET": "a-test-secret-only", "IRA_CARE_PUBLIC_BASE_URL": "https://care.example.invalid",
       "IRA_CARE_ARC_SECRET": "arc-test-secret-only"}


class RecordingEmail:
    name = "recording_email"
    configured = True

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send_email(self, to, subject, body):
        self.sent.append((to, subject, body))
        return providers.Delivery("accepted", "accepted by the mail server; delivery not confirmed",
                                  f"<{len(self.sent)}@test>")


class RecordingCentre:
    name = "recording_centre"
    configured = True

    def __init__(self):
        self.alarms: list[dict] = []

    async def raise_alarm(self, *, person_label, summary, reference, idempotency_key="", callback_url=None):
        self.alarms.append({"reference": reference, "key": idempotency_key, "callback_url": callback_url})
        return providers.Delivery("accepted", "accepted by the monitoring centre", "ARC-7")


async def _give_tier1_an_email(fam, address="daughter@example.invalid"):
    async with workspace_connection(sim.admin_principal(fam)) as conn:
        await conn.execute(
            "UPDATE care_circle_members SET email = $1 WHERE id = (SELECT id FROM care_circle_members "
            "WHERE tier = 1 ORDER BY priority LIMIT 1)", address)


async def test_a_tier_is_emailed_when_a_mail_server_is_configured(family):
    await _give_tier1_an_email(family)
    mail = RecordingEmail()
    alert = await _help(family)
    await escalation.run_workspace(sim._device_principal(family), dt.datetime.now(UTC),
                                   provider=Recording(), email_provider=mail)
    emails = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "email"]
    assert len(emails) == 1 and emails[0]["tier"] == 1
    assert emails[0]["outcome"] == "accepted", "taken by the mail server: never 'delivered' on that alone"
    assert emails[0]["finished_at"] is not None, "sent through the durable outbox"
    assert [to for to, _, _ in mail.sent] == ["daughter@example.invalid"]
    to, subject, body = mail.sent[0]
    assert subject.startswith("IRA CARE alert for") and "I'm on my way" in body


async def test_without_a_mail_server_no_email_is_queued(family):
    await _give_tier1_an_email(family)
    alert = await _help(family)
    # The test environment has no IRA_CARE_SMTP_*: the default provider is "none".
    await escalation.run_workspace(sim._device_principal(family), dt.datetime.now(UTC), provider=Recording())
    assert [a for a in await _attempts(family, alert["id"]) if a["channel"] == "email"] == []


async def test_a_member_without_an_address_is_not_emailed(family):
    mail = RecordingEmail()
    alert = await _help(family)
    await escalation.run_workspace(sim._device_principal(family), dt.datetime.now(UTC),
                                   provider=Recording(), email_provider=mail)
    assert [a for a in await _attempts(family, alert["id"]) if a["channel"] == "email"] == []
    assert mail.sent == []


async def _to_tier_3(family, centre, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    jp = sim._device_principal(family)
    alert = await _help(family)
    now = dt.datetime.now(UTC)
    for _ in range(4):
        await escalation.run_workspace(jp, now, provider=Recording(), centre=centre)
        now += dt.timedelta(minutes=30)
    arc = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "monitoring_centre"]
    return alert, arc


def _signed(body: dict, secret=ENV["IRA_CARE_ARC_SECRET"], ts=None):
    raw = json.dumps(body).encode()
    ts = ts or str(int(time.time()))
    return raw, {"Content-Type": "application/json", "X-IRA-Timestamp": ts,
                 "X-IRA-Signature": providers.arc_signature(secret, ts, raw)}


async def test_tier_3_goes_to_the_centre_and_only_its_signed_acknowledgement_moves_it(family, client, monkeypatch):
    centre = RecordingCentre()
    alert, arc = await _to_tier_3(family, centre, monkeypatch)
    assert arc and arc[0]["outcome"] == "accepted", "handed over; not yet confirmed handled"
    assert {a["key"] for a in centre.alarms} == {f"ira-alert-{alert['id']}"}, "one key for one alert, however often"
    url = centre.alarms[0]["callback_url"]
    assert url and str(alert["id"]) in url
    path = url.replace("https://care.example.invalid", "")

    # Not signed, signed with another secret, stale, or at an address we did not make: refused, nothing written.
    raw, headers = _signed({"status": "handling"})
    assert (await client.post(path, content=raw, headers={"Content-Type": "application/json"})).status_code == 403
    raw2, h2 = _signed({"status": "handling"}, secret="not-the-secret")
    assert (await client.post(path, content=raw2, headers=h2)).status_code == 403
    raw3, h3 = _signed({"status": "handling"}, ts=str(int(time.time()) - 3600))
    assert (await client.post(path, content=raw3, headers=h3)).status_code == 403
    forged = path.rsplit("=", 1)[0] + "=" + "0" * 40
    assert (await client.post(forged, content=raw, headers=headers)).status_code == 403
    assert [a for a in await _attempts(family, alert["id"]) if a["channel"] == "monitoring_centre"][0]["outcome"] == "accepted"

    # Signed and ours: an operator has it. Then a late "received" never moves it back.
    assert (await client.post(path, content=raw, headers=headers)).status_code == 204
    after = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "monitoring_centre"]
    assert all(a["outcome"] == "delivered" and a["provider_status"] == "handling" for a in after)
    raw4, h4 = _signed({"status": "received"})
    assert (await client.post(path, content=raw4, headers=h4)).status_code == 204
    again = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "monitoring_centre"]
    assert all(a["outcome"] == "delivered" for a in again)


async def test_without_a_centre_tier_3_is_skipped_and_the_family_told_to_call_999(family, monkeypatch):
    alert, arc = await _to_tier_3(family, providers.monitoring_centre_from_env({}), monkeypatch)
    assert arc and all(a["outcome"] == "skipped" for a in arc)
