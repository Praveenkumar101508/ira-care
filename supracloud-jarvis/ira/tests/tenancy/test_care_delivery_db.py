"""Remediation group A2 against a real PostgreSQL: an alert message is queued
durably, sent once, retried when it fails, never held up by another person's
slow provider, and its state is what the provider said, moved only forward by
a callback that is ours and signed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, pool  # noqa: E402,F401

from care import delivery, escalation, providers  # noqa: E402
from care import repository as repo  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("simulate_patient", ROOT / "scripts" / "simulate_patient.py")
sim = importlib.util.module_from_spec(_spec)
sys.modules["simulate_patient"] = sim
_spec.loader.exec_module(sim)

UTC = dt.timezone.utc
SECRET_ENV = {"IRA_CARE_CALLBACK_SECRET": "a-test-secret-only", "IRA_CARE_PUBLIC_BASE_URL": "https://care.example.invalid",
              "IRA_CARE_TWILIO_AUTH_TOKEN": "test-auth-token-not-real"}


@pytest.fixture
async def family(pool):
    fam = await sim.create_family(ADMIN_DSN, pool, label="dlv")
    try:
        await sim.setup_person(fam)
        yield fam
    finally:
        await sim.remove_family(ADMIN_DSN, fam)


class Recording(providers.LogOnlyProvider):
    """Accepts like a real provider, counts every send, can fail or be slow on cue."""
    name = "recording"
    configured = True

    def __init__(self, *, fail_first: int = 0, slow_to: str = "", delay: float = 0.0):
        super().__init__()
        self.sent: list[tuple[str, str]] = []
        self.fail_first, self.slow_to, self.delay = fail_first, slow_to, delay
        self.callbacks: list[str] = []

    async def _go(self, kind, to, status_callback):
        if to == self.slow_to:
            await asyncio.sleep(self.delay)
        if self.fail_first > 0:
            self.fail_first -= 1
            return providers.Delivery("failed", "provider answered 503")
        self.sent.append((kind, to))
        self.callbacks.append(status_callback or "")
        return providers.Delivery("accepted", "accepted by the provider", f"SM{len(self.sent):032d}")

    async def send_sms(self, to, text, *, status_callback=None):
        providers.assert_not_emergency_number(to)
        return await self._go("sms", to, status_callback)

    async def call(self, to, text, *, status_callback=None):
        providers.assert_not_emergency_number(to)
        return await self._go("call", to, status_callback)


async def _attempts(fam, alert_id):
    """Each message with its CURRENT state (furthest event) and its outbox row."""
    async with workspace_connection(sim.admin_principal(fam)) as conn:
        rows = [dict(r) for r in await conn.fetch(
            "SELECT a.*, d.tries, d.next_try_at, d.finished_at FROM care_alert_attempts a "
            "LEFT JOIN care_alert_dispatch d ON d.attempt_id = a.id WHERE a.alert_id = $1 "
            "ORDER BY a.attempted_at, a.channel", alert_id)]
        states = await delivery.current_states(conn, [r["id"] for r in rows])
    for r in rows:
        r.update(states[r["id"]])
    return rows


async def _help(fam):
    return await repo.record_help_request(sim._device_principal(fam), fam.device, {"via": "button"})


async def test_tier_one_is_queued_then_sent_and_recorded_as_accepted_not_delivered(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    p = Recording()
    out = await escalation.run_workspace(jp, dt.datetime.now(UTC), provider=p)
    assert out.dispatched > 0
    rows = [a for a in await _attempts(family, alert["id"]) if a["channel"] in ("sms", "call")]
    assert rows and all(a["outcome"] == "accepted" and a["provider_ref"].startswith("SM") for a in rows)
    assert all(a["tries"] == 1 and a["status_rank"] == delivery.rank("accepted") and a["finished_at"] for a in rows)


async def test_two_workers_at_once_send_each_message_exactly_once(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    p = Recording()
    now = dt.datetime.now(UTC)
    await asyncio.gather(escalation.run_workspace(jp, now, provider=p), escalation.run_workspace(jp, now, provider=p),
                         escalation.run_workspace(jp, now, provider=p))
    rows = await _attempts(family, alert["id"])
    sends = [a for a in rows if a["channel"] in ("sms", "call")]
    assert len(p.sent) == len(sends) == len(set(p.sent)), "every message once, none twice"
    assert {a["tier"] for a in rows} == {1}, "the tier was run once, not once per worker"


async def test_a_queued_message_survives_a_crash_before_it_is_sent(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    now = dt.datetime.now(UTC)
    # The chain runs and commits, and the process dies before sending anything.
    async with workspace_connection(jp) as conn:
        await escalation.run_due_escalations(conn, jp, now, provider=Recording())
    queued = [a for a in await _attempts(family, alert["id"]) if a["outcome"] == "queued"]
    assert queued, "written down before anything was sent"
    p = Recording()
    out = await delivery.dispatch(jp, now, provider=p)
    assert out.claimed == len(queued) and len(p.sent) == len([a for a in queued if a["channel"] in ("sms", "call")])
    assert not [a for a in await _attempts(family, alert["id"]) if a["outcome"] == "queued"]


async def test_a_failed_send_is_retried_then_recorded_as_failed(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    now = dt.datetime.now(UTC)
    p = Recording(fail_first=1000)
    await escalation.run_workspace(jp, now, provider=p)
    sms = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "sms"]
    assert sms and all(a["outcome"] == "queued" and a["tries"] == 1 and a["next_try_at"] > now
                       and a["finished_at"] is None for a in sms)
    for minute in (1, 3, 10):
        await delivery.dispatch(jp, now + dt.timedelta(minutes=minute), provider=p)
    sms = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "sms"]
    assert all(a["outcome"] == "failed" and a["tries"] == delivery.MAX_TRIES for a in sms)


async def test_a_provider_that_recovers_is_sent_on_the_retry(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    now = dt.datetime.now(UTC)
    p = Recording(fail_first=1)
    await escalation.run_workspace(jp, now, provider=p)
    await delivery.dispatch(jp, now + dt.timedelta(minutes=2), provider=p)
    rows = [a for a in await _attempts(family, alert["id"]) if a["channel"] in ("sms", "call")]
    assert all(a["outcome"] == "accepted" for a in rows)
    assert max(a["tries"] for a in rows) == 2


async def test_one_slow_provider_does_not_hold_up_the_others(family, monkeypatch):
    jp = sim._device_principal(family)
    alert = await _help(family)
    monkeypatch.setattr(delivery, "SEND_TIMEOUT_S", 0.5)
    slow_phone = "+440000000001"
    p = Recording(slow_to=slow_phone, delay=5)
    started = dt.datetime.now(UTC)
    await escalation.run_workspace(jp, started, provider=p)
    took = (dt.datetime.now(UTC) - started).total_seconds()
    assert took < 4, f"one slow send held the pass for {took:.1f} s"
    assert any(to != slow_phone for _kind, to in p.sent), "the other people were still told"
    slow = [a for a in await _attempts(family, alert["id"]) if a["outcome"] == "queued" and a["finished_at"] is None]
    assert slow and all(a["tries"] == 1 for a in slow), "the slow one is kept to try again"


async def test_a_message_for_an_alert_resolved_before_sending_is_not_sent(family):
    jp = sim._device_principal(family)
    alert = await _help(family)
    now = dt.datetime.now(UTC)
    async with workspace_connection(jp) as conn:
        await escalation.run_due_escalations(conn, jp, now, provider=Recording())
    await repo.resolve_alert(sim.admin_principal(family), alert["id"], "All well.", outcome="all_well")
    p = Recording()
    await delivery.dispatch(jp, now, provider=p)
    assert p.sent == []
    rows = await _attempts(family, alert["id"])
    assert all(a["outcome"] != "queued" for a in rows)
    assert all(a["outcome"] == "skipped" and "resolved" in a["detail"] for a in rows if a["channel"] in ("sms", "call", "push"))


# ── Signed status callbacks ──────────────────────────────────────────────────

async def _sent_attempt(family, monkeypatch):
    for k, v in SECRET_ENV.items():
        monkeypatch.setenv(k, v)
    jp = sim._device_principal(family)
    alert = await _help(family)
    p = Recording()
    await escalation.run_workspace(jp, dt.datetime.now(UTC), provider=p)
    sms = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "sms"][0]
    url = next(c for c in p.callbacks if str(sms["id"]) in c)
    return sms, url


async def _post(client, url, form, *, sign=True):
    path = url.replace("https://care.example.invalid", "")
    headers = {"X-Twilio-Signature": delivery.twilio_signature(url, form, SECRET_ENV["IRA_CARE_TWILIO_AUTH_TOKEN"])} if sign else {}
    return await client.post(path, data=form, headers=headers)


async def _attempt(family, attempt_id):
    async with workspace_connection(sim.admin_principal(family)) as conn:
        row = dict(await conn.fetchrow("SELECT * FROM care_alert_attempts WHERE id = $1", attempt_id))
        row.update((await delivery.current_states(conn, [attempt_id]))[attempt_id])
    return row


async def test_a_signed_callback_moves_the_state_forward_and_only_forward(family, client, monkeypatch):
    sms, url = await _sent_attempt(family, monkeypatch)
    form = {"MessageSid": sms["provider_ref"], "MessageStatus": "delivered", "AccountSid": "ACtest"}
    assert (await _post(client, url, form)).status_code == 204
    assert (await _attempt(family, sms["id"]))["outcome"] == "delivered"
    # The same callback again, and a late "sent" after "delivered": nothing moves back.
    assert (await _post(client, url, form)).status_code == 204
    assert (await _post(client, url, dict(form, MessageStatus="sent"))).status_code == 204
    row = await _attempt(family, sms["id"])
    assert row["outcome"] == "delivered" and row["provider_status"] == "delivered"


async def test_a_callback_that_is_not_ours_or_not_signed_changes_nothing(family, client, monkeypatch):
    sms, url = await _sent_attempt(family, monkeypatch)
    form = {"MessageSid": sms["provider_ref"], "MessageStatus": "undelivered"}
    assert (await _post(client, url, form, sign=False)).status_code == 403
    bad = dict(form)
    r = await client.post(url.replace("https://care.example.invalid", ""), data=bad,
                          headers={"X-Twilio-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="})
    assert r.status_code == 403
    forged = url.rsplit("=", 1)[0] + "=" + "0" * 40
    assert (await _post(client, forged, form)).status_code == 403, "our token is checked as well as Twilio's"
    assert (await _attempt(family, sms["id"]))["outcome"] == "accepted"
    # Signed, but for a different message reference: not this row.
    assert (await _post(client, url, dict(form, MessageSid="SMnot-this-one"))).status_code == 204
    assert (await _attempt(family, sms["id"]))["outcome"] == "accepted"


async def test_a_callback_before_the_send_was_recorded_is_kept_and_not_undone(family, client, monkeypatch):
    for k, v in SECRET_ENV.items():
        monkeypatch.setenv(k, v)
    jp = sim._device_principal(family)
    alert = await _help(family)
    async with workspace_connection(jp) as conn:
        await escalation.run_due_escalations(conn, jp, dt.datetime.now(UTC), provider=Recording())
    sms = [a for a in await _attempts(family, alert["id"]) if a["channel"] == "sms"][0]
    url = delivery.status_callback_url(family.tenant_id, family.workspace_id, sms["id"])
    # Twilio answers before our worker has written down "accepted".
    assert (await _post(client, url, {"MessageSid": "SMrace", "MessageStatus": "delivered"})).status_code == 204
    async with workspace_connection(jp) as conn:
        await delivery.record_state(conn, sms["id"], "accepted", provider_ref="SMrace", provider_status="accepted")
    row = await _attempt(family, sms["id"])
    assert row["outcome"] == "delivered" and row["provider_ref"] == "SMrace"


async def test_the_attempt_itself_is_never_rewritten_and_every_report_is_kept(family, client, monkeypatch):
    sms, url = await _sent_attempt(family, monkeypatch)
    for status in ("sent", "delivered", "delivered"):
        await _post(client, url, {"MessageSid": sms["provider_ref"], "MessageStatus": status})
    async with workspace_connection(sim.admin_principal(family)) as conn:
        raw = await conn.fetchrow("SELECT outcome FROM care_alert_attempts WHERE id = $1", sms["id"])
        events = await conn.fetch("SELECT outcome, provider_status, source FROM care_alert_attempt_events "
                                  "WHERE attempt_id = $1 ORDER BY status_rank, recorded_at", sms["id"])
    # The app role still cannot rewrite an attempt, or an event.
    for sql in ("UPDATE care_alert_attempts SET outcome = 'delivered'", "UPDATE care_alert_attempt_events SET detail = 'x'",
                "DELETE FROM care_alert_attempt_events"):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with workspace_connection(sim._device_principal(family)) as c2:
                await c2.execute(sql)
    assert raw["outcome"] == "queued", "the row written when it was queued is unchanged"
    assert [(e["outcome"], e["source"]) for e in events] == [
        ("accepted", "dispatcher"), ("accepted", "callback"), ("delivered", "callback")], "a repeat is one row"
