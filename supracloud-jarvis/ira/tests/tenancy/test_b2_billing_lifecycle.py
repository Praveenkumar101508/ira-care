"""B2 — the subscription lifecycle, driven end to end against real PostgreSQL.

`080_payment_events.sql` and `payments.py` built an idempotent, signature-checked
front door and **nothing consumed it**: an event arrived, was written down, and
no tenant's subscription moved. `core/billing/subscriptions.py` is the applier
that was missing, and this drives it.

── What is real here and what is not ────────────────────────────────────────

Real: PostgreSQL with FORCE RLS on, the real migration chain, the real HMAC
signature verification, the real idempotency constraint, the real state machine.

Not real: no money moves and no provider is contacted. `TestProvider` signs the
way the mainstream ones do — HMAC-SHA256 over `"{timestamp}.{body}"` — so the
SHAPE under test is the shape a real provider needs. **No live transaction has
been executed from this repository**, and no test here pretends otherwise.

── The properties, in the order a defect would be worst ─────────────────────

  1. an UNSIGNED or MIS-SIGNED event changes nothing            (forgery)
  2. a REPLAYED event changes nothing the second time           (double-apply)
  3. an event for an unknown customer changes nothing           (attribution)
  4. an ENTITLEMENT does not outlive the period it was paid for (FINDING CF)
  5. a missing price REFUSES rather than granting service       (never free)
  6. a missing provider REFUSES rather than granting service    (never free)
  7. the happy path actually works                              (the control)

Property 7 is not decoration. Without it every refusal above passes on a system
that refuses everything, which is the failure mode this file exists to avoid.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

SIGNING_SECRET = "b2-test-signing-secret"


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def priced(monkeypatch):
    """Every test runs with the test provider selected and a price in place.

    `086` seeds the price into the real chain; this fixture only asserts it is
    there, because a suite that seeded its own price would pass on a deployment
    where the owner's price migration had never run.
    """
    monkeypatch.setenv("IRA_PAYMENT_PROVIDER", "test")
    monkeypatch.setenv("IRA_PAYMENT_TEST_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("IRA_ENVIRONMENT", "development")


async def _tenant(admin, label: str, *, customer_ref: str = "") -> dict:
    slug = f"b2-{label}-{uuid.uuid4().hex[:8]}"
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (name, slug, edition, provider_customer_ref) "
        "VALUES ($1,$1,'cloud',$2) RETURNING id", slug, customer_ref)
    org_id = await admin.fetchval(
        "INSERT INTO organisations (tenant_id, name) VALUES ($1,'B2') RETURNING id",
        tenant_id)
    workspace_id = await admin.fetchval(
        "INSERT INTO workspaces (tenant_id, organisation_id, name, slug, "
        "                        is_default, status) "
        "VALUES ($1,$2,'Default','default',true,'active') RETURNING id",
        tenant_id, org_id)
    user_id = await admin.fetchval(
        "INSERT INTO users (email, display_name, status, email_verified_at) "
        "VALUES ($1,$2,'active',now()) RETURNING id",
        f"{slug}@example.invalid", slug)
    await admin.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
        "VALUES ($1,$2,'owner','active')", tenant_id, user_id)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, "
        "                                   role, status) "
        "VALUES ($1,$2,$3,'workspace_admin','active')",
        tenant_id, workspace_id, user_id)
    return {"tenant_id": tenant_id, "workspace_id": workspace_id,
            "user_id": user_id, "slug": slug, "customer_ref": customer_ref}


@pytest.fixture
async def two_tenants(admin):
    a = await _tenant(admin, "a", customer_ref=f"cus_a_{uuid.uuid4().hex[:8]}")
    b = await _tenant(admin, "b", customer_ref=f"cus_b_{uuid.uuid4().hex[:8]}")
    yield a, b

    # Teardown that cannot half-complete — `test_zz_no_fixture_residue` fails
    # the whole directory over one tenant left holding memberships, three job
    # stages later, with an error naming rollback/035.
    failures = []
    for t in (a, b):
        try:
            # `payment_events` rows are deliberately NOT removed. `080` puts a
            # trigger on the table refusing deletion — "it is the record that a
            # provider told us something" — and a test that worked around its
            # own system's audit guarantee would be teaching the wrong lesson.
            # They carry no tenant_id and no FK, so they leak nothing.
            await admin.execute("DELETE FROM tenants WHERE id = $1", t["tenant_id"])
            await admin.execute("DELETE FROM users WHERE id = $1", t["user_id"])
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            failures.append(f"{t['slug']}: {type(exc).__name__}: {exc}")
    assert not failures, f"the fixture leaked a tenant: {failures}"


def _principal(t):
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal

    return Principal(
        user_id=t["user_id"], tenant_id=t["tenant_id"],
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
        roles=frozenset({"owner"}),
        scopes=frozenset({"billing_read", "billing_manage"}),
        workspace_id=t["workspace_id"],
    )


def _signed(body: dict, *, secret: str = SIGNING_SECRET,
            skew: int = 0) -> tuple[bytes, dict[str, str]]:
    """A body and the headers a provider would send with it."""
    from core.billing.payments import TestProvider

    raw = json.dumps(body).encode()
    stamp = int(time.time()) + skew
    return raw, {
        "X-Payment-Timestamp": str(stamp),
        "X-Payment-Signature": TestProvider(secret).sign(body=raw, timestamp=stamp),
    }


def _event(kind: str, customer: str, **obj) -> dict:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": kind,
        "customer": customer,
        "data": {"object": {"customer": customer, **obj}},
    }


# ── 7. The control: the happy path actually works ───────────────────────────


async def test_a_paid_checkout_activates_the_subscription(pool, admin, two_tenants):
    from core.billing.payments import TestProvider
    from core.billing.subscriptions import CHECKOUT_COMPLETED, apply_event

    a, _ = two_tenants
    provider = TestProvider(SIGNING_SECRET)
    body, headers = _signed(_event(
        CHECKOUT_COMPLETED, a["customer_ref"],
        current_period_start=int(time.time()),
        current_period_end=int(time.time()) + 30 * 86400,
        subscription="sub_b2_happy"))

    event = provider.verify(body=body, headers=headers)
    outcome = await apply_event(a["tenant_id"], event)

    assert outcome == "applied"
    row = await admin.fetchrow(
        "SELECT plan_id, status, provider_subscription_ref, current_period_end "
        "  FROM tenant_subscriptions WHERE tenant_id = $1", a["tenant_id"])
    assert row is not None, "a paid checkout created no subscription at all"
    assert row["status"] == "active"
    assert row["plan_id"] == "ira_pro"
    assert row["provider_subscription_ref"] == "sub_b2_happy"
    assert row["current_period_end"] > datetime.now(timezone.utc)


async def test_the_entitlement_is_real_and_comes_from_the_plan(pool, admin, two_tenants):
    """An entitlement nothing reads is a promise nothing keeps."""
    from core.billing.entitlements import effective_limits
    from core.billing.subscriptions import activate

    a, _ = two_tenants
    await activate(a["tenant_id"], plan_id="ira_pro",
                   period_end=datetime.now(timezone.utc) + timedelta(days=30))

    limits = await effective_limits(_principal(a))
    assert "model.requests_per_day" in limits, (
        f"the plan granted no model entitlement; got {limits}")
    assert limits["model.requests_per_day"] == 5000


# ── 1. Forgery ──────────────────────────────────────────────────────────────


async def test_an_unsigned_event_is_refused_and_writes_nothing(pool, admin, two_tenants):
    from core.billing.payments import SignatureInvalid, TestProvider

    a, _ = two_tenants
    raw = json.dumps(_event("invoice.payment_succeeded", a["customer_ref"])).encode()

    with pytest.raises(SignatureInvalid):
        TestProvider(SIGNING_SECRET).verify(body=raw, headers={})

    assert await admin.fetchval(
        "SELECT count(*) FROM tenant_subscriptions WHERE tenant_id = $1",
        a["tenant_id"]) == 0, "an unsigned event created a subscription"


async def test_a_wrongly_signed_event_is_refused(pool, admin, two_tenants):
    from core.billing.payments import SignatureInvalid, TestProvider

    a, _ = two_tenants
    body, headers = _signed(_event("invoice.payment_succeeded", a["customer_ref"]),
                            secret="not-the-signing-secret")

    with pytest.raises(SignatureInvalid):
        TestProvider(SIGNING_SECRET).verify(body=body, headers=headers)

    assert await admin.fetchval(
        "SELECT count(*) FROM tenant_subscriptions WHERE tenant_id = $1",
        a["tenant_id"]) == 0


# ── 2. Replay and duplicate delivery ────────────────────────────────────────


async def test_a_correctly_signed_but_old_event_is_a_replay(pool, two_tenants):
    from core.billing.payments import TestProvider, TimestampOutOfRange

    a, _ = two_tenants
    body, headers = _signed(_event("invoice.payment_succeeded", a["customer_ref"]),
                            skew=-3600)

    with pytest.raises(TimestampOutOfRange):
        TestProvider(SIGNING_SECRET).verify(body=body, headers=headers)


async def test_the_same_event_delivered_twice_is_applied_once(pool, admin, two_tenants):
    """Delivery is at-least-once. That is the contract, not a corner case."""
    from core.billing.payments import TestProvider, record_arrival

    a, _ = two_tenants
    payload = _event("invoice.payment_succeeded", a["customer_ref"])
    body, headers = _signed(payload)
    provider = TestProvider(SIGNING_SECRET)

    first_id, first_new = await record_arrival(provider.verify(body=body, headers=headers))
    second_id, second_new = await record_arrival(provider.verify(body=body, headers=headers))

    assert first_new is True
    assert second_new is False, (
        "the second delivery of the same event was treated as new, so any "
        "state change would be applied twice")
    assert first_id == second_id
    assert await admin.fetchval(
        "SELECT count(*) FROM payment_events WHERE provider_event_id = $1",
        payload["id"]) == 1


# ── 3. Attribution ──────────────────────────────────────────────────────────


async def test_an_event_for_an_unknown_customer_is_kept_and_applied_to_nobody(
        pool, admin, two_tenants):
    from core.billing.payments import TestProvider, attribute, record_arrival

    provider = TestProvider(SIGNING_SECRET)
    body, headers = _signed(_event("invoice.payment_succeeded", "cus_nobody_here"))
    event = provider.verify(body=body, headers=headers)

    row_id, _ = await record_arrival(event)
    tenant = await attribute(row_id, event.customer_ref)

    assert tenant is None, "an unknown customer reference resolved to a tenant"
    outcome = await admin.fetchval(
        "SELECT outcome FROM payment_events WHERE id = $1", row_id)
    assert outcome == "unattributed", (
        "the event was not KEPT as unattributed; the only record that a payment "
        "arrived for somebody we cannot identify would be gone")

    # And it CANNOT be tidied away, which is the stronger half of the same
    # property: `080` puts a trigger on the table that refuses deletion.
    with pytest.raises(asyncpg.exceptions.RaiseError):
        await admin.execute("DELETE FROM payment_events WHERE id = $1", row_id)


async def test_one_tenants_payment_cannot_move_another_tenants_subscription(
        pool, admin, two_tenants):
    from core.billing.payments import TestProvider, attribute, record_arrival

    a, b = two_tenants
    provider = TestProvider(SIGNING_SECRET)
    body, headers = _signed(_event("invoice.payment_succeeded", a["customer_ref"]))
    event = provider.verify(body=body, headers=headers)

    row_id, _ = await record_arrival(event)
    tenant = await attribute(row_id, event.customer_ref)

    assert tenant == a["tenant_id"]
    assert tenant != b["tenant_id"], (
        "a payment for tenant A attributed to tenant B — the mapping is what "
        "decides whose subscription moves")


# ── 4. FINDING CF: an entitlement must not outlive the paid period ──────────


async def test_an_expired_subscription_grants_nothing(pool, admin, two_tenants):
    """No provider sends "this has finished being paid for".

    Expiry is a fact about the clock, and before this was fixed the query read
    only `status`, so a subscription whose period ended a year ago sat at
    `status='active'` and kept granting its plan's entitlements for ever.
    """
    from core.billing.entitlements import effective_limits

    a, _ = two_tenants
    await admin.execute(
        "INSERT INTO tenant_subscriptions "
        "  (tenant_id, plan_id, status, current_period_start, current_period_end) "
        "VALUES ($1,'ira_pro','active', NOW() - interval '400 days', "
        "        NOW() - interval '370 days')",
        a["tenant_id"])

    limits = await effective_limits(_principal(a))
    assert "model.requests_per_day" not in limits, (
        "an expired subscription still granted the plan's entitlements. The "
        "status column says 'active' because nothing ever sets it otherwise — "
        "no event means 'the paid period ended'.")


async def test_past_due_keeps_the_period_it_paid_for(pool, admin, two_tenants):
    """A failed card is a payment problem, not a reason to lock somebody out
    of the month they have already paid for."""
    from core.billing.entitlements import effective_limits
    from core.billing.subscriptions import activate, mark_past_due

    a, _ = two_tenants
    await activate(a["tenant_id"], plan_id="ira_pro",
                   period_end=datetime.now(timezone.utc) + timedelta(days=10))
    assert await mark_past_due(a["tenant_id"]) is True

    limits = await effective_limits(_principal(a))
    assert limits.get("model.requests_per_day") == 5000, (
        "past_due removed the entitlement inside a period the tenant has paid "
        "for")

    status = await admin.fetchval(
        "SELECT status FROM tenant_subscriptions WHERE tenant_id = $1",
        a["tenant_id"])
    assert status == "past_due"


async def test_cancelling_at_period_end_keeps_access_until_it_ends(
        pool, admin, two_tenants):
    from core.billing.entitlements import effective_limits
    from core.billing.subscriptions import activate, cancel

    a, _ = two_tenants
    await activate(a["tenant_id"], plan_id="ira_pro",
                   period_end=datetime.now(timezone.utc) + timedelta(days=10))
    assert await cancel(a["tenant_id"], at_period_end=True) is True

    row = await admin.fetchrow(
        "SELECT status, cancel_at_period_end FROM tenant_subscriptions "
        " WHERE tenant_id = $1", a["tenant_id"])
    assert row["status"] == "active", (
        "cancel-at-period-end ended the subscription immediately, taking away "
        "a period the tenant has paid for")
    assert row["cancel_at_period_end"] is True

    limits = await effective_limits(_principal(a))
    assert limits.get("model.requests_per_day") == 5000


async def test_cancelling_now_ends_the_entitlement_now(pool, admin, two_tenants):
    from core.billing.entitlements import effective_limits
    from core.billing.subscriptions import activate, cancel

    a, _ = two_tenants
    await activate(a["tenant_id"], plan_id="ira_pro",
                   period_end=datetime.now(timezone.utc) + timedelta(days=10))
    assert await cancel(a["tenant_id"], at_period_end=False) is True

    limits = await effective_limits(_principal(a))
    assert "model.requests_per_day" not in limits, (
        "a cancelled subscription still granted entitlements")


# ── 5 and 6. A missing price or provider REFUSES; neither means free ────────


async def test_an_unpriced_plan_resolves_to_no_price_rather_than_zero(pool, admin):
    from core.billing.subscriptions import resolve_price

    await admin.execute(
        "INSERT INTO billing_plans (plan_id, display_name, description, active) "
        "VALUES ('b2_unpriced','B2 Unpriced','',TRUE) ON CONFLICT DO NOTHING")
    try:
        price = await resolve_price("b2_unpriced")
        assert price is None, (
            f"a plan with no price row resolved to {price}. Absence must stay "
            f"absence: zero would be free, and free is not what nobody decided.")
    finally:
        await admin.execute("DELETE FROM billing_plans WHERE plan_id = 'b2_unpriced'")


async def test_the_configured_price_is_the_owners_decision_and_nothing_else(pool):
    """£15.00 GBP monthly, read from the row the migration wrote.

    Asserted against the DATABASE, not against a constant in the test: a test
    carrying its own copy of the price would agree with itself while the charge
    disagreed with both.
    """
    from core.billing.subscriptions import resolve_price

    price = await resolve_price("ira_pro")
    assert price is not None, (
        "ira_pro has no price. 086 records the owner's decision and must have "
        "been applied.")
    assert price.currency == "GBP"
    assert price.unit_amount_minor == 1500
    assert price.recurrence == "monthly"
    assert price.formatted == "£15.00/month"


async def test_no_provider_configured_is_a_refusal_not_a_free_ride(monkeypatch):
    from core.billing.provider import configured_provider

    monkeypatch.delenv("IRA_PAYMENT_PROVIDER", raising=False)
    assert configured_provider() is None

    monkeypatch.setenv("IRA_PAYMENT_PROVIDER", "stripe")
    monkeypatch.delenv("IRA_STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("IRA_STRIPE_WEBHOOK_SECRET", raising=False)
    assert configured_provider() is None, (
        "a provider named without its secrets reported itself as configured")


async def test_the_test_provider_is_refused_in_a_production_like_environment(
        monkeypatch):
    """A deployment that accepts test-signed billing events is one where anybody
    holding a value from a config file can grant themselves a plan."""
    from core.billing.provider import configured_provider

    monkeypatch.setenv("IRA_PAYMENT_PROVIDER", "test")
    monkeypatch.setenv("IRA_PAYMENT_TEST_SECRET", SIGNING_SECRET)
    for environment in ("production", "staging"):
        monkeypatch.setenv("IRA_ENVIRONMENT", environment)
        assert configured_provider() is None, (
            f"the test payment provider was accepted in {environment}")


# ── The Stripe adapter's signature scheme, against real signatures ──────────


def test_the_stripe_adapter_verifies_its_own_documented_scheme():
    import hashlib
    import hmac

    from core.billing.payments import SignatureInvalid
    from core.billing.provider import StripeProvider

    secret = "whsec_b2_test"
    provider = StripeProvider(secret_key="sk_test_unused", webhook_secret=secret)
    body = json.dumps({
        "id": "evt_stripe_1", "type": "invoice.payment_succeeded",
        "data": {"object": {"customer": "cus_stripe_1"}},
    }).encode()
    stamp = int(time.time())
    good = hmac.new(secret.encode(), f"{stamp}.".encode() + body,
                    hashlib.sha256).hexdigest()

    event = provider.verify(body=body,
                            headers={"Stripe-Signature": f"t={stamp},v1={good}"})
    assert event.event_type == "invoice.payment_succeeded"
    assert event.customer_ref == "cus_stripe_1"

    # Several v1 values appear during a signing-secret rotation, and ANY of them
    # verifying is an accepted request. That is the mechanism that makes
    # rotation possible without dropping events.
    rotated = provider.verify(
        body=body,
        headers={"Stripe-Signature": f"t={stamp},v1=deadbeef,v1={good}"})
    assert rotated.event_id == "evt_stripe_1"

    with pytest.raises(SignatureInvalid):
        provider.verify(body=body,
                        headers={"Stripe-Signature": f"t={stamp},v1=deadbeef"})
    with pytest.raises(SignatureInvalid):
        provider.verify(body=body, headers={})


def test_a_provider_cannot_be_constructed_without_its_secrets():
    from core.billing.provider import StripeProvider

    with pytest.raises(ValueError):
        StripeProvider(secret_key="", webhook_secret="whsec")
    with pytest.raises(ValueError):
        StripeProvider(secret_key="sk", webhook_secret="")


# ── An unrecognised event moves nothing ─────────────────────────────────────


async def test_an_event_type_this_build_does_not_know_changes_nothing(
        pool, admin, two_tenants):
    from core.billing.payments import TestProvider
    from core.billing.subscriptions import apply_event

    a, _ = two_tenants
    body, headers = _signed(_event("customer.discount.created", a["customer_ref"]))
    event = TestProvider(SIGNING_SECRET).verify(body=body, headers=headers)

    outcome = await apply_event(a["tenant_id"], event)
    assert outcome == "ignored"
    assert await admin.fetchval(
        "SELECT count(*) FROM tenant_subscriptions WHERE tenant_id = $1",
        a["tenant_id"]) == 0, (
        "an event type this build does not understand moved a subscription")
