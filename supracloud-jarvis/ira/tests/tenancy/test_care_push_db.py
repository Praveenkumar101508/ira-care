"""Care push isolation (round 3, phase 2): a phone is pushed to only for the
alerts of the people whose circle its account belongs to.

Real HTTP stack, real PostgreSQL as `ira_app`, a fake push provider that
records what it was handed and answers receipts the test chooses.
"""
from __future__ import annotations

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

from care import checkin_cycle as cycle  # noqa: E402
from care import escalation, providers, push  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402

C = "/api/v1/care"
UTC = dt.timezone.utc
TOKEN_A = "ExponentPushToken[family-a-phone-0001]"
TOKEN_A2 = "ExponentPushToken[family-a-second-phone]"
TOKEN_B = "ExponentPushToken[family-b-phone-0001]"


class FakePush:
    """Takes every message, hands back a ticket per token, answers receipts as told."""
    name = "fake"

    def __init__(self):
        self.messages: list[dict] = []
        self.receipt_plan: dict[str, push.Receipt] = {}
        self._n = 0

    async def send(self, messages):
        out = []
        for m in messages:
            self._n += 1
            self.messages.append(m)
            out.append(push.Ticket(m["to"], f"ticket-{self._n}", "sent"))
        return out

    async def receipts(self, ticket_ids):
        return [self.receipt_plan[t] for t in ticket_ids if t in self.receipt_plan]

    @property
    def tokens(self):
        return [m["to"] for m in self.messages]


async def _setup(client, fam, *, name="Tess") -> dict:
    client.act_as(fam, "admin", "workspace_admin")
    r = await client.post(f"{C}/people", json={"display_name": f"Test Person {name}", "preferred_name": name, "timezone": "UTC"})
    person = r.json()
    r = await client.post(f"{C}/people/{person['id']}/circle", json={
        "display_name": f"{name}'s family", "role": "family", "user_id": str(fam["member"]), "phone": "+440000000001"})
    person["member"] = r.json()
    r = await client.post(f"{C}/people/{person['id']}/devices", json={"platform": "wearos"})
    paired = (await client.post(f"{C}/devices/pair", json={"pairing_code": r.json()["pairing_code"]})).json()
    person["h"] = {"X-Device-Token": paired["device_token"]}
    person["jp"] = cycle.job_principal(fam["tenant_id"], fam["workspace_id"])
    return person


async def _help(client, p) -> str:
    r = await client.post(f"{C}/devices/me/help", json={"via": "button"}, headers=p["h"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _register(client, fam, token, who="member"):
    client.act_as(fam, who, "member")
    r = await client.post(f"{C}/push/register", json={"token": token, "platform": "android"})
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_second_family_never_receives_the_first_familys_alert(client, families):
    a, b = families["a"], families["b"]
    pa = await _setup(client, a, name="Tess")
    pb = await _setup(client, b, name="Bert")
    assert (await _register(client, a, TOKEN_A))["memberships"] == 1
    assert (await _register(client, b, TOKEN_B))["memberships"] == 1
    alert_id = await _help(client, pa)
    fake = FakePush()
    out = await escalation.run_workspace(pa["jp"], provider=providers.LogOnlyProvider(), push_provider=fake)
    assert out.tiers_run == 1
    assert fake.tokens == [TOKEN_A], fake.tokens
    # Family B's own pass finds nothing to push for its own alerts either, and never saw A's.
    fake_b = FakePush()
    await escalation.run_workspace(pb["jp"], provider=providers.LogOnlyProvider(), push_provider=fake_b)
    assert fake_b.tokens == []
    client.act_as(a, "member", "member")
    alert = (await client.get(f"{C}/alerts/{alert_id}")).json()
    pushes = [x for x in alert["attempts"] if x["channel"] == "push"]
    assert len(pushes) == 1 and pushes[0]["outcome"] == "sent"
    assert pushes[0]["delivery"] == "sent, delivery not yet confirmed"
    # Family B cannot even see the alert, let alone its delivery.
    client.act_as(b, "admin", "workspace_admin")
    assert (await client.get(f"{C}/alerts/{alert_id}")).status_code == 404


async def test_a_token_registered_in_the_wrong_workspace_is_refused(client, families):
    """An account with no circle membership here has nothing to register for."""
    b = families["b"]
    await _setup(client, families["a"])
    client.act_as(b, "member", "member")
    r = await client.post(f"{C}/push/register", json={"token": TOKEN_B})
    assert r.status_code == 404
    r = await client.post(f"{C}/push/register", json={"token": "not-a-token"})
    assert r.status_code == 422


async def test_a_removed_member_stops_receiving(client, families):
    a = families["a"]
    p = await _setup(client, a)
    await _register(client, a, TOKEN_A)
    client.act_as(a, "admin", "workspace_admin")
    r = await client.patch(f"{C}/circle/{p['member']['id']}", json={"active": False})
    assert r.status_code == 200, r.text
    await _help(client, p)
    fake = FakePush()
    out = await escalation.run_workspace(p["jp"], provider=providers.LogOnlyProvider(), push_provider=fake)
    assert out.tiers_run == 1 and fake.tokens == []
    client.act_as(a, "member", "member")
    regs = (await client.get(f"{C}/push/registrations")).json()["registrations"]
    assert regs and all(x["status"] == "revoked" for x in regs)


async def test_a_deleted_member_takes_their_tokens_with_them(client, families):
    a = families["a"]
    p = await _setup(client, a)
    await _register(client, a, TOKEN_A)
    client.act_as(a, "admin", "workspace_admin")
    assert (await client.delete(f"{C}/circle/{p['member']['id']}")).status_code == 204
    async with workspace_connection(p["jp"]) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_push_tokens") == 0


async def test_a_revoked_token_is_dropped_and_the_rest_still_go(client, families):
    a = families["a"]
    p = await _setup(client, a)
    await _register(client, a, TOKEN_A)
    await _register(client, a, TOKEN_A2)
    client.act_as(a, "member", "member")
    r = await client.request("DELETE", f"{C}/push/register", json={"token": TOKEN_A})
    assert r.status_code == 200 and r.json()["revoked"] == 1
    await _help(client, p)
    fake = FakePush()
    await escalation.run_workspace(p["jp"], provider=providers.LogOnlyProvider(), push_provider=fake)
    assert fake.tokens == [TOKEN_A2]
    # Re-registering the revoked token brings it back.
    await _register(client, a, TOKEN_A)
    async with workspace_connection(p["jp"]) as conn:
        assert await conn.fetchval("SELECT count(*) FROM care_push_tokens WHERE status = 'active'") == 2


async def test_receipts_mark_delivery_and_a_dead_token_is_revoked(client, families):
    a = families["a"]
    p = await _setup(client, a)
    await _register(client, a, TOKEN_A)
    await _register(client, a, TOKEN_A2)
    alert_id = await _help(client, p)
    fake = FakePush()
    now = dt.datetime.now(UTC)
    await escalation.run_workspace(p["jp"], now, provider=providers.LogOnlyProvider(), push_provider=fake)
    assert sorted(fake.tokens) == sorted([TOKEN_A, TOKEN_A2])
    fake.receipt_plan = {"ticket-1": push.Receipt("ticket-1", "delivered"),
                         "ticket-2": push.Receipt("ticket-2", "failed", "DeviceNotRegistered", dead_token=True)}
    async with workspace_connection(p["jp"]) as conn:
        out = await push.fetch_receipts(conn, fake, now=now + dt.timedelta(seconds=30))
    assert (out["delivered"], out["failed"], out["revoked"]) == (1, 1, 1)
    client.act_as(a, "member", "member")
    alert = (await client.get(f"{C}/alerts/{alert_id}")).json()
    pushes = [x for x in alert["attempts"] if x["channel"] == "push"]
    assert pushes[0]["delivery"] == "delivered" and pushes[0]["delivered_at"] is not None
    regs = (await client.get(f"{C}/push/registrations")).json()["registrations"]
    assert sorted(x["status"] for x in regs) == ["active", "revoked"]
    # The next push to this member reaches the one phone that is left.
    fake2 = FakePush()
    async with workspace_connection(p["jp"]) as conn:
        outcome, _detail, tickets = await push.send_to_member(conn, p["member"], {"id": alert_id, "kind": "help_requested", "severity": "sos"},
                                                             "t", "b", fake2)
    assert outcome == "sent" and len(tickets) == 1 and fake2.tokens == [TOKEN_A]


async def test_without_a_registered_phone_the_attempt_says_so(client, families):
    a = families["a"]
    p = await _setup(client, a)
    alert_id = await _help(client, p)
    fake = FakePush()
    await escalation.run_workspace(p["jp"], provider=providers.LogOnlyProvider(), push_provider=fake)
    client.act_as(a, "member", "member")
    alert = (await client.get(f"{C}/alerts/{alert_id}")).json()
    pushes = [x for x in alert["attempts"] if x["channel"] == "push"]
    assert pushes[0]["outcome"] == "skipped" and pushes[0]["delivery"] == "no phone registered"


async def test_account_erasure_removes_the_accounts_push_tokens_only(client, families):
    a = families["a"]
    p = await _setup(client, a)
    await _register(client, a, TOKEN_A)
    # A second member (the viewer account) with a phone of their own.
    client.act_as(a, "admin", "workspace_admin")
    r = await client.post(f"{C}/people/{p['id']}/circle", json={"display_name": "Other", "role": "carer", "user_id": str(a["viewer"])})
    assert r.status_code == 201
    client.act_as(a, "viewer", "member")
    assert (await client.post(f"{C}/push/register", json={"token": TOKEN_A2})).status_code == 201
    from core.privacy.erasure import _delete_own_care_push_tokens
    from tests.tenancy._care_fixtures import _principal
    async with workspace_connection(_principal(a, "member", "member")) as conn:
        assert await _delete_own_care_push_tokens(conn, _principal(a, "member", "member")) == 1
        left = await conn.fetch("SELECT user_id FROM care_push_tokens")
    assert [r["user_id"] for r in left] == [a["viewer"]]
