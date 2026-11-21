"""P11-04 — a payment webhook is verified before it exists, and acted on once.

The two properties, and why each is the batch rather than decoration:

    A WEBHOOK WHOSE SIGNATURE WAS NOT VERIFIED is an unauthenticated request
    from anyone on the internet. A billing endpoint missing that check is a URL
    that lets a stranger mark their own invoice paid.

    A WEBHOOK PROCESSED TWICE is a customer charged twice.

Both are tested by counting ROWS, not by asserting on exception types. A build
that raised `SignatureInvalid` *after* writing the event down would satisfy
`pytest.raises` and still have put a stranger's request into the billing schema.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid

import pytest

pytestmark = [pytest.mark.asyncio]

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.billing import payments  # noqa: E402
from core.billing.payments import (  # noqa: E402
    PaymentRefused, SignatureInvalid, TestProvider, TimestampOutOfRange,
)

#: Generated per run, so NO literal signing key exists in the repository.
#:
#: The first version hard-coded one and the Gitleaks gate refused the commit —
#: correctly. A checked-in test key is precisely the habit secret scanning
#: exists to stop being normal: it reads as harmless until somebody copies the
#: pattern into a file where the value is real, and by then the reviewer has
#: seen a dozen of them and stopped noticing. Generating it also means the
#: signature tests cannot accidentally depend on one particular key.
SIGNING_KEY = secrets.token_hex(16)


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    monkeypatch.delenv("IRA_EDITION", raising=False)


@pytest.fixture
def provider():
    return TestProvider(SIGNING_KEY)


@pytest.fixture
async def world():
    """One plan, one tenant, one subscription — torn down with the test."""
    plan_id = f"p1104_{uuid.uuid4().hex[:12]}"
    tenant = uuid.uuid4()
    customer = f"cus_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO billing_plans (plan_id, display_name) VALUES ($1,$2)",
            plan_id, "P11-04 plan")
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, provider_customer_ref) "
            "VALUES ($1,$2,$2,$3)",
            tenant, f"p1104-{tenant.hex[:10]}", customer)
        await conn.execute(
            "INSERT INTO tenant_subscriptions (tenant_id, plan_id, "
            "  current_period_end) VALUES ($1,$2, NOW() + INTERVAL '30 days')",
            tenant, plan_id)
    finally:
        await conn.close()

    yield {"tenant": tenant, "plan": plan_id, "customer": customer}

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        # `080`'s trigger refuses to DELETE an arrival, and it is right to —
        # that is a control this module tests. Cleaning up is therefore explicit
        # administrative surgery, scoped to THIS CONNECTION so the control is
        # never off for anybody else.
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("DELETE FROM payment_events WHERE customer_ref = $1", customer)
        await conn.execute("DELETE FROM tenant_subscriptions WHERE tenant_id = $1", tenant)
        await conn.execute("DELETE FROM billing_plans WHERE plan_id = $1", plan_id)
    finally:
        await conn.close()


def _signed(provider, body_dict, *, timestamp=None, tamper=False):
    body = json.dumps(body_dict).encode()
    ts = timestamp if timestamp is not None else int(time.time())
    sig = provider.sign(body=body, timestamp=ts)
    if tamper:
        sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    return body, {"X-Payment-Timestamp": str(ts), "X-Payment-Signature": sig}


def _event(customer, *, event_id=None, kind="payment.succeeded"):
    return {"id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
            "type": kind, "customer": customer, "amount_minor": 2500}


async def _count(where: str, *args) -> int:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return int(await conn.fetchval(
            f"SELECT count(*) FROM payment_events WHERE {where}", *args))
    finally:
        await conn.close()


# ── 1. Nothing unverified ever becomes a row ─────────────────────────────────

async def test_a_forged_signature_is_refused_and_writes_nothing(provider, world):
    """Counts ROWS. A build that raised *after* the insert would still have put
    a stranger's request into the billing schema."""
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload, tamper=True)

    with pytest.raises(SignatureInvalid):
        provider.verify(body=body, headers=headers)

    assert await _count("provider_event_id = $1", payload["id"]) == 0


async def test_a_missing_signature_is_refused(provider, world):
    body = json.dumps(_event(world["customer"])).encode()
    with pytest.raises(SignatureInvalid):
        provider.verify(body=body, headers={
            "X-Payment-Timestamp": str(int(time.time()))})


async def test_a_body_altered_after_signing_is_refused(provider, world):
    """The signature covers the BODY, not just the headers.

    A scheme that signed only a header would let anyone who observed one valid
    request substitute their own amount and keep the signature.
    """
    body, headers = _signed(provider, _event(world["customer"]))
    tampered = body.replace(b'"amount_minor": 2500', b'"amount_minor": 1')
    assert tampered != body, "the test did not actually alter the body"

    with pytest.raises(SignatureInvalid):
        provider.verify(body=tampered, headers=headers)


async def test_a_provider_cannot_be_built_without_a_secret():
    """An empty secret would verify every request against "" — accepting
    anything correctly shaped, while looking configured."""
    with pytest.raises(ValueError):
        TestProvider("")


# ── 2. A correctly signed OLD request is a replay ────────────────────────────

async def test_a_correctly_signed_but_stale_request_is_refused(provider, world):
    """The signature proves WHO sent it, never WHEN.

    Without a timestamp bound, an attacker who once observed a valid
    `payment.succeeded` can replay it forever — every replay verifies, because
    it IS the original request.
    """
    stale = int(time.time()) - (payments.TIMESTAMP_TOLERANCE_SECONDS + 60)
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload, timestamp=stale)

    with pytest.raises(TimestampOutOfRange):
        provider.verify(body=body, headers=headers)

    assert await _count("provider_event_id = $1", payload["id"]) == 0


async def test_a_request_from_the_future_is_refused_too(provider, world):
    """Only bounding the past would let a forged-clock request sit valid
    indefinitely once real time caught up to it."""
    ahead = int(time.time()) + (payments.TIMESTAMP_TOLERANCE_SECONDS + 60)
    body, headers = _signed(provider, _event(world["customer"]), timestamp=ahead)
    with pytest.raises(TimestampOutOfRange):
        provider.verify(body=body, headers=headers)


async def test_a_request_inside_the_tolerance_is_accepted(provider, world):
    """The bound must not be so tight that ordinary clock skew rejects real
    traffic — a check that refuses everything is not a working control."""
    recent = int(time.time()) - (payments.TIMESTAMP_TOLERANCE_SECONDS - 30)
    body, headers = _signed(provider, _event(world["customer"]), timestamp=recent)
    event = provider.verify(body=body, headers=headers)
    assert event.customer_ref == world["customer"]


# ── 3. Recorded exactly once ─────────────────────────────────────────────────

async def test_a_redelivered_event_is_recorded_once_and_not_acted_on_again(
        provider, world):
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)

    first_id, was_new = await payments.record_arrival(event)
    assert was_new is True

    second_id, was_new_again = await payments.record_arrival(event)
    assert was_new_again is False, (
        "a redelivered webhook was reported as new; the caller would act on it "
        "a second time and the customer would be charged twice")
    assert second_id == first_id, "the redelivery became a different row"
    assert await _count("provider_event_id = $1", payload["id"]) == 1


async def test_simultaneous_deliveries_still_produce_one_row(provider, world):
    """The control is a UNIQUE constraint, not a check-then-insert.

    Two deliveries arriving together both observe "not present" in application
    code. This asserts the property under the concurrency it exists for: exactly
    one caller may be told the event is new.
    """
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)

    results = await asyncio.gather(*(payments.record_arrival(event) for _ in range(5)))

    assert sum(1 for _id, was_new in results if was_new) == 1, (
        "more than one concurrent delivery was told the event was new")
    assert len({str(_id) for _id, _ in results}) == 1, "they disagree about the row"
    assert await _count("provider_event_id = $1", payload["id"]) == 1


async def test_two_providers_may_share_an_event_id(provider, world):
    """Uniqueness is (provider, event_id). `evt_1` at one provider is not
    `evt_1` at another, and collapsing them would drop a real event."""
    payload = _event(world["customer"], event_id="evt_shared")
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)
    await payments.record_arrival(event)

    from dataclasses import replace
    other = replace(event, provider="other_test")
    _id, was_new = await payments.record_arrival(other)
    assert was_new is True, "a different provider's event was swallowed as a duplicate"


# ── 4. Attribution is a result, never an input ───────────────────────────────

async def test_an_event_is_attributed_to_the_subscribed_tenant(provider, world):
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)
    row_id, _ = await payments.record_arrival(event)

    tenant = await payments.attribute(row_id, event.customer_ref)
    assert tenant == world["tenant"]


async def test_an_unattributable_event_is_kept_not_discarded(provider, world):
    """A payment nobody can attribute is a thing a human must look at.

    Deleting it to keep the table tidy destroys the only record that it
    happened — and `080`'s trigger refuses to delete it at all.
    """
    payload = _event("cus_belongs_to_nobody")
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)
    row_id, _ = await payments.record_arrival(event)

    assert await payments.attribute(row_id, event.customer_ref) is None

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT outcome, note FROM payment_events WHERE id = $1",
            row_id)
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.execute("DELETE FROM payment_events WHERE id = $1", row_id)
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("DELETE FROM payment_events WHERE id = $1", row_id)
    finally:
        await conn.close()

    assert row["outcome"] == "unattributed"
    assert row["note"], "the row does not say why it could not be attributed"


async def test_an_event_with_no_customer_is_not_attributed_to_anyone(provider, world):
    """The empty string is the column DEFAULT on every subscription that has no
    provider reference yet.

    A lookup that passed '' straight through would match one of them and credit
    a stranger's payment to whichever tenant the index returned first. Found by
    a defect plant: removing the empty-string guard changed nothing observable
    until a subscription with no provider reference existed to match.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        # A second subscription with NO provider reference — the row the empty
        # string would match.
        other = uuid.uuid4()
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           other, f"p1104b-{other.hex[:10]}")
        await conn.execute(
            "INSERT INTO tenant_subscriptions (tenant_id, plan_id, current_period_end) "
            "VALUES ($1,$2, NOW() + INTERVAL '30 days')", other, world["plan"])
    finally:
        await conn.close()

    body, headers = _signed(provider, {"id": f"evt_{uuid.uuid4().hex[:12]}",
                                       "type": "payment.succeeded", "customer": ""})
    event = provider.verify(body=body, headers=headers)
    row_id, _ = await payments.record_arrival(event)

    assert await payments.attribute(row_id, event.customer_ref) is None, (
        "an event naming no customer was attributed to a tenant anyway")

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("DELETE FROM payment_events WHERE id = $1", row_id)
        await conn.execute("DELETE FROM tenant_subscriptions WHERE tenant_id = $1", other)
    finally:
        await conn.close()


async def test_the_runtime_cannot_repoint_another_tenants_mapping(world):
    """The escalation `tenants` makes possible, refused explicitly.

    `tenants` is the tenancy ROOT: no RLS, no policies, and `ira_app` holds
    table-level UPDATE. It has to be readable before scope exists — that is
    what a root is for, and attribution depends on it. But that visibility must
    not become the ability to REWRITE somebody else's mapping.

    An earlier version of `080` put the column here with nothing guarding it,
    and this test is what caught it.
    """
    conn = await asyncpg.connect(DSN)          # the RUNTIME role
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE tenants SET provider_customer_ref = 'cus_hijacked' "
                " WHERE id = $1", world["tenant"])
    finally:
        await conn.close()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        still = await conn.fetchval(
            "SELECT provider_customer_ref FROM tenants WHERE id = $1",
            world["tenant"])
    finally:
        await conn.close()
    assert still == world["customer"], "the mapping moved despite the refusal"


async def test_the_runtime_cannot_claim_an_unused_provider_reference(world):
    """Squatting, which is the subtler half of the same attack.

    Refusing only to overwrite an EXISTING mapping would leave this open: claim
    `cus_notyet` before the provider assigns it, and the real customer's first
    payment arrives already pointed somewhere else. So the guard refuses ANY
    change by the runtime, not merely a change to somebody else's value.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        free = await conn.fetchval(
            "INSERT INTO tenants (name, slug) VALUES ($1,$1) RETURNING id",
            f"p1104free-{uuid.uuid4().hex[:8]}")
    finally:
        await conn.close()

    conn = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE tenants SET provider_customer_ref = 'cus_squatted' "
                " WHERE id = $1", free)
        # …nor by creating a tenant that arrives already carrying one.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO tenants (name, slug, provider_customer_ref) "
                "VALUES ($1,$1,'cus_squatted')", f"p1104s-{uuid.uuid4().hex[:8]}")
    finally:
        await conn.close()


async def test_the_guard_refuses_only_the_mapping_and_not_everything_else(world):
    """A guard that refused every write to `tenants` would pass the two tests
    above and break the product. The runtime legitimately renames a tenant."""
    conn = await asyncpg.connect(DSN)
    try:
        # A UNIQUE name, not a fixed string: `tenants.name` is unique, so a
        # constant would pass on the first run and collide on every one after.
        # The same order-and-history dependence P11-03 had to fix.
        await conn.execute("UPDATE tenants SET name = $2 WHERE id = $1",
                           world["tenant"], f"renamed-{uuid.uuid4().hex[:12]}")
        await conn.execute(
            "INSERT INTO tenants (name, slug) VALUES ($1,$1)",
            f"p1104ok-{uuid.uuid4().hex[:8]}")
    finally:
        await conn.close()


async def test_the_runtime_cannot_switch_the_guard_off(world):
    """A control the guarded party can disable is not a control.

    Both routes are checked: `DISABLE TRIGGER` needs table ownership, and
    `session_replication_role = replica` needs superuser. `ira_app` is neither,
    and this asserts that rather than trusting the role definition.
    """
    conn = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "ALTER TABLE tenants DISABLE TRIGGER tenants_provider_customer_guard")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("SET session_replication_role = replica")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "DROP TRIGGER tenants_provider_customer_guard ON tenants")
    finally:
        await conn.close()


async def test_the_guard_is_not_a_grant_so_042_cannot_undo_it(world):
    """The reason this is a trigger and not column-level privileges.

    042 GRANTs SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public,
    and a TABLE-level UPDATE grant supersedes column-level ones — so a
    column-privilege design would be silently unprotected the next time 042 ran,
    which the CI rollback cycle does on every run. This is the same 042-shaped
    hole P10-04 found the hard way.

    A GRANT cannot remove a trigger. Asserted by actually running the grant.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        # `tenants` ALONE, and only the privilege the runtime already holds.
        #
        # The first version issued 042's actual statement — GRANT … ON ALL
        # TABLES IN SCHEMA public — which proved the point and then left the
        # database WIDER than it found it, undoing every REVOKE in 076 through
        # 080 for the rest of the session. `test_role_separation` went red
        # immediately afterwards, on a database this test had damaged.
        #
        # A test that has to break the system to observe a property is not
        # entitled to leave it broken. This grants exactly what the claim needs:
        # a TABLE-level UPDATE, which is what would supersede a column-level
        # grant, on the one table in question. It is already held, so the
        # database is unchanged and there is nothing to restore.
        await conn.execute("GRANT UPDATE ON tenants TO ira_app")
    finally:
        await conn.close()

    conn = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE tenants SET provider_customer_ref = 'cus_after_042' "
                " WHERE id = $1", world["tenant"])
    finally:
        await conn.close()


async def test_two_tenants_cannot_share_one_provider_customer(world):
    """Ambiguity resolved arbitrarily credits a payment to the wrong company."""
    other = uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO tenants (id, name, slug, provider_customer_ref) "
                "VALUES ($1,$2,$2,$3)",
                other, f"p1104d-{other.hex[:10]}", world["customer"])
    finally:
        await conn.close()


async def test_the_unbound_intake_path_can_still_read_the_mapping(world):
    """The whole reason the mapping sits on the tenancy root.

    Attribution runs with NOTHING bound. If this read returned nothing — which
    is what a tenant-scoped table does when unbound, silently and without
    erroring — every arriving payment would become unattributable.
    """
    conn = await asyncpg.connect(DSN)
    try:
        found = await conn.fetchval(
            "SELECT id FROM tenants WHERE provider_customer_ref = $1",
            world["customer"])
    finally:
        await conn.close()
    assert found == world["tenant"]


async def test_what_arrived_cannot_be_rewritten(provider, world):
    """The decision may be recorded; the delivery may not be re-labelled.

    An event that could be re-pointed at a different provider, id or payload is
    not an audit trail of anything.
    """
    payload = _event(world["customer"])
    body, headers = _signed(provider, payload)
    event = provider.verify(body=body, headers=headers)
    row_id, _ = await payments.record_arrival(event)

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for column, value in (("provider_event_id", "'evt_rewritten'"),
                              ("event_type", "'payment.refunded'"),
                              ("payload", "'{}'::jsonb")):
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await conn.execute(
                    f"UPDATE payment_events SET {column} = {value} WHERE id = $1",
                    row_id)
        # …while recording the decision is exactly what the row is for.
        await conn.execute(
            "UPDATE payment_events SET outcome='ignored', processed_at=NOW(), "
            "  note='this deployment does not act on this type' WHERE id = $1",
            row_id)
        still = await conn.fetchval(
            "SELECT provider_event_id FROM payment_events WHERE id = $1", row_id)
    finally:
        await conn.close()

    assert still == payload["id"]


async def test_an_event_carrying_no_id_is_refused(provider, world):
    """Without an id there is no idempotency key, and an event that cannot be
    de-duplicated is one that may be applied twice."""
    body, headers = _signed(provider, {"type": "payment.succeeded",
                                       "customer": world["customer"]})
    with pytest.raises(PaymentRefused):
        provider.verify(body=body, headers=headers)
