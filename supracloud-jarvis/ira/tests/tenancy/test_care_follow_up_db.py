""""I'm on my way" is a promise with a deadline (round 3, phase 3; care/followup.py).

Real HTTP stack, real PostgreSQL as `ira_app`, a fake push provider for the
responder's phone, the clock moved by hand.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")
pytest.importorskip("httpx")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import client, families, pool  # noqa: E402,F401
from tests.tenancy.test_care_push_db import TOKEN_A, FakePush  # noqa: E402

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 23, 14, 0, tzinfo=UTC)


async def _setup(client, fam) -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"})
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Sravani", "role": "family", "tier": 1, "user_id": str(fam["member"]), "phone": "+440000000001"})
    person["member"] = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": "Care desk", "role": "care_organisation", "tier": 2, "phone": "+440000000003"})
    person["desk"] = r.json()
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    client.act_as(fam, "member", "member")
    assert (await client.post(f"{C}/push/register", json={"token": TOKEN_A})).status_code == 201
    return person


async def _open_and_acknowledge(client, fam, p) -> dict:
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    alert_id = r.json()["id"]
    fake = FakePush()
    # Tier 1 runs on the wall clock (the alert opened now); the test's own clock takes over after this.
    await escalation.run_workspace(p["jp"], dt.datetime.now(UTC), provider=providers.LogOnlyProvider(), push_provider=fake)
    assert fake.tokens == [TOKEN_A]
    client.act_as(fam, "member", "member")
    r = await client.post(f"{C}/alerts/{alert_id}/acknowledge", json={"kind": "on_my_way"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "acknowledged" and a["follow_up_due_at"] is not None
    return a


async def _set_follow_up_clock(p, alert_id):
    """The acknowledgement used the wall clock; move the deadline onto the test's clock."""
    async with workspace_connection(p["jp"]) as conn:
        await conn.execute("UPDATE care_alerts SET acknowledged_at = $2::timestamptz, follow_up_due_at = $2::timestamptz + interval '30 minutes' WHERE id = $1",
                           uuid.UUID(alert_id), T0 + dt.timedelta(minutes=1))


async def _run(p, at, fake=None):
    fake = fake or FakePush()
    out = await escalation.run_workspace(p["jp"], at, provider=providers.LogOnlyProvider(), push_provider=fake)
    return out, fake


async def test_the_deadline_asks_the_responder_then_re_escalates(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a = await _open_and_acknowledge(client, fam, p)
    await _set_follow_up_clock(p, a["id"])
    # 29 minutes after the promise: nothing happens, the responder is trusted.
    out, fake = await _run(p, T0 + dt.timedelta(minutes=30))
    assert (out.follow_ups_asked, out.re_escalated, fake.tokens) == (0, 0, [])
    # 31 minutes: the responder's phone is asked; the alert stays acknowledged.
    out, fake = await _run(p, T0 + dt.timedelta(minutes=32))
    assert out.follow_ups_asked == 1 and fake.tokens == [TOKEN_A]
    assert "reached Tess" in fake.messages[0]["title"]
    alert = (await client.get(f"{C}/alerts/{a['id']}")).json()
    assert alert["status"] == "acknowledged" and alert["follow_up_asked_at"] is not None
    assert any(x["channel"] == "push" and x["detail"].startswith("follow-up") for x in alert["attempts"])
    # The grace passes with no arrival and no outcome: the chain restarts at the next tier.
    out, fake = await _run(p, T0 + dt.timedelta(minutes=32 + 9))
    assert out.re_escalated == 0
    out, fake = await _run(p, T0 + dt.timedelta(minutes=32 + 11))
    assert out.re_escalated == 1 and out.tiers_run == 1
    alert = (await client.get(f"{C}/alerts/{a['id']}")).json()
    assert alert["status"] == "escalating" and alert["re_escalations"] == 1 and alert["current_tier"] == 2
    assert "raised again" in alert["details"]["summary"]
    tiers = sorted({x["tier"] for x in alert["attempts"]})
    assert tiers == [1, 2], tiers


async def test_an_arrival_meets_the_deadline_but_does_not_close_the_alert(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a = await _open_and_acknowledge(client, fam, p)
    await _set_follow_up_clock(p, a["id"])
    r = await client.post(f"{C}/alerts/{a['id']}/acknowledge", json={"kind": "arrived"})
    assert r.status_code == 200 and r.json()["follow_up_due_at"] is None
    out, fake = await _run(p, T0 + dt.timedelta(hours=3))
    assert (out.follow_ups_asked, out.re_escalated, fake.tokens) == (0, 0, [])
    alert = (await client.get(f"{C}/alerts/{a['id']}")).json()
    assert alert["status"] == "acknowledged", "open until an outcome is recorded"
    open_alerts = (await client.get(f"{C}/alerts", params={"open_only": "false"})).json()["alerts"]
    assert open_alerts[0]["status"] == "acknowledged"


async def test_an_outcome_is_required_to_close_and_is_kept(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    a = await _open_and_acknowledge(client, fam, p)
    r = await client.post(f"{C}/alerts/{a['id']}/resolve", json={"resolution": "Visited."})
    assert r.status_code == 422, "no outcome, no closing"
    r = await client.post(f"{C}/alerts/{a['id']}/resolve", json={"resolution": "Visited.", "outcome": "something_else"})
    assert r.status_code == 422
    r = await client.post(f"{C}/alerts/{a['id']}/resolve", json={"resolution": "Visited; had dozed off.", "outcome": "all_well"})
    assert r.status_code == 200 and r.json()["status"] == "resolved" and r.json()["outcome"] == "all_well"
    assert r.json()["follow_up_due_at"] is None


async def test_two_responders_acknowledging_at_once_change_the_alert_once(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/circle", json={
        "display_name": "Praveen", "role": "carer", "tier": 1, "user_id": str(fam["viewer"])})
    assert r.status_code == 201
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    alert_id = r.json()["id"]
    from tests.tenancy._care_fixtures import _principal
    from care import repository as repo
    first, second = _principal(fam, "member", "member"), _principal(fam, "viewer", "member")
    results = await asyncio.gather(
        repo.acknowledge_alert(first, uuid.UUID(alert_id), {"kind": "on_my_way"}),
        repo.acknowledge_alert(second, uuid.UUID(alert_id), {"kind": "on_my_way"}),
    )
    assert all(x["status"] == "acknowledged" for x in results)
    client.act_as(fam, "member", "member")
    alert = (await client.get(f"{C}/alerts/{alert_id}")).json()
    assert len(alert["acknowledgements"]) == 2 and alert["acknowledged_at"] is not None
    # Both closing at once: one outcome stands, both callers get the closed alert back.
    closes = await asyncio.gather(
        repo.resolve_alert(first, uuid.UUID(alert_id), "Sravani: all well.", outcome="all_well"),
        repo.resolve_alert(second, uuid.UUID(alert_id), "Praveen: needed help.", outcome="needed_help"),
    )
    assert all(x["status"] == "resolved" for x in closes)
    alert = (await client.get(f"{C}/alerts/{alert_id}")).json()
    assert alert["resolved_at"] is not None and alert["outcome"] in ("all_well", "needed_help")
    assert closes[0]["outcome"] == closes[1]["outcome"] == alert["outcome"], "the first close stands for both"
    r = await client.post(f"{C}/alerts/{alert_id}/resolve", json={"resolution": "again", "outcome": "other"})
    assert r.status_code == 200 and r.json()["outcome"] == alert["outcome"], "closing twice changes nothing"


async def test_the_follow_up_setting_is_per_person(client, families):
    fam = families["a"]
    p = await _setup(client, fam)
    client.act_as(fam, "admin", "workspace_admin")
    s = (await client.get(f"{C}/people/{p['id']}/settings")).json()
    assert (s["follow_up_minutes"], s["follow_up_grace_minutes"]) == (30, 10)
    body = {k: v for k, v in s.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    body["follow_up_minutes"] = 5
    assert (await client.put(f"{C}/people/{p['id']}/settings", json=body)).status_code == 200
    a = await _open_and_acknowledge(client, fam, p)
    due = dt.datetime.fromisoformat(a["follow_up_due_at"]); acked = dt.datetime.fromisoformat(a["acknowledged_at"])
    assert abs((due - acked).total_seconds() - 300) < 2
