"""P11-02 — a plan's entitlement actually stops a request.

Phase 9 was reported closed while its machinery had no product caller. The
standard this file is written to, taken from that reconciliation:

    prove it by COUNTING WHAT THE PROVIDER WAS ASKED TO DO,
    not by reading a status field.

A test asserting `outcome == "refused_quota"` would pass on a build that called
the model, spent the tokens, and then reported a refusal. So every enforcement
test below counts provider invocations, and the number that matters is zero.

The composition under test is the production one as far as it can be taken
without a model server:

    IRAModelGateway._admit() step 2 → quota_reader (= store.load_quota)
        → entitlements.narrow → check_quota → refuse, or admit and call

`load_quota` and `narrow` are the shipped functions running against real
PostgreSQL as the restricted role under FORCE RLS. Only the provider is a
double, because a test must not call a model.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest

pytestmark = [pytest.mark.asyncio]

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.billing import entitlements  # noqa: E402
from core.identity.principal import system_principal  # noqa: E402
from core.models.gateway import Quota, UsageSoFar, check_quota  # noqa: E402
from core.models.store import load_quota  # noqa: E402

PLAN = "p11_test_plan"
_CREATED: list[uuid.UUID] = []


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


@pytest.fixture(autouse=True)
async def _cleanup():
    """Take back the memberships and subscriptions this module creates.

    `test_zz_no_fixture_residue.py` fails the directory when more than one
    tenant still holds membership data, and `rollback/078` REFUSES while any
    subscription exists — which would surface later in the same CI job as a
    failure against 035 or 078 rather than against the suite that caused it.
    """
    before = len(_CREATED)
    yield
    mine, _CREATED[before:] = _CREATED[before:], []
    if not mine:
        return
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "DELETE FROM tenant_subscriptions WHERE tenant_id = ANY($1::uuid[])", mine)
        await conn.execute(
            "DELETE FROM tenant_entitlement_overrides WHERE tenant_id = ANY($1::uuid[])",
            mine)
        await conn.execute(
            "DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])", mine)
    finally:
        await conn.close()


async def _tenant() -> uuid.UUID:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"p1102-{tenant.hex[:10]}")
        await conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES ($1,$2,$3)",
            user, f"p1102-{user.hex[:10]}@example.test", "p11-02")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'admin','active')", tenant, user)
    finally:
        await conn.close()
    _CREATED.append(tenant)
    return tenant


def _p(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="p11-02-entitlements")


async def _plan_with(key: str, limit) -> None:
    """The plan catalogue, seeded by the TEST rather than by the migration.

    078 seeds no plan on purpose — which plans exist is a product decision — so
    a test that needs one creates it, as the admin role, and the entitlement it
    carries is this test's own fixture rather than a claim about the product.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO billing_plans (plan_id, display_name) VALUES ($1,$2) "
            "ON CONFLICT (plan_id) DO NOTHING", PLAN, "P11 test plan")
        await conn.execute(
            "INSERT INTO plan_entitlements (plan_id, key, limit_value) "
            "VALUES ($1,$2,$3) "
            "ON CONFLICT (plan_id, key) DO UPDATE SET limit_value = EXCLUDED.limit_value",
            PLAN, key, limit)
    finally:
        await conn.close()


async def _subscribe(tenant: uuid.UUID, status: str = "active") -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO tenant_subscriptions "
            "(tenant_id, plan_id, status, current_period_end) "
            "VALUES ($1,$2,$3, NOW() + INTERVAL '30 days')", tenant, PLAN, status)
    finally:
        await conn.close()


async def _quota_row(tenant: uuid.UUID, *, daily: int | None) -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO usage_quotas (tenant_id, daily_request_limit) VALUES ($1,$2) "
            "ON CONFLICT (tenant_id) DO UPDATE SET daily_request_limit = EXCLUDED.daily_request_limit",
            tenant, daily)
    finally:
        await conn.close()


# ── 1. The resolver reads THIS tenant's rows, through RLS ────────────────────

async def _record_one_request(tenant: uuid.UUID) -> None:
    """One metered request today, in `model_usage`, exactly as 029 records them."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO model_usage (tenant_id, provider_id, outcome, at) "
            "VALUES ($1,'ollama-local','ok', NOW())", tenant)
    finally:
        await conn.close()


def _completion_request():
    from core.interfaces.model_provider import CompletionRequest, Tier
    return CompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        tier=Tier.FAST, sensitivity="normal")


def _is_refusal(result) -> bool:
    """A refusal, however the gateway spells it.

    Checked structurally rather than by class name so this does not become a
    test of the refusal type's spelling.
    """
    from core.interfaces.model_provider import CompletionResult
    return not isinstance(result, CompletionResult)


def _gateway_with_a_counting_provider():
    """The real IRAModelGateway, with a provider that counts entries.

    The counter is incremented on the FIRST line of `complete()`, before any
    other work, so it records "the provider was reached" rather than "the
    provider succeeded" — a build that entered the provider and then failed
    would still be caught.
    """
    from decimal import Decimal

    from core.interfaces.model_provider import (
        CompletionResult, Locality, ProviderDisclosure,
    )
    from core.models.gateway import IRAModelGateway

    calls: list[int] = []

    class CountingProvider:
        provider_id = "ollama-local"

        def disclosure(self):
            return ProviderDisclosure(
                provider_id="ollama-local", display_name="Ollama Local",
                locality=Locality.LOCAL, retains_prompts=False,
                region="on-device", trains_on_data=False)

        def supports(self, tier) -> bool:
            return True

        async def complete(self, request):
            calls.append(1)
            return CompletionResult(
                text="an answer", model="ollama-local-model",
                provider_id="ollama-local", disclosure=self.disclosure(),
                prompt_tokens=10, completion_tokens=20)

        async def stream(self, request):    # pragma: no cover - unused here
            yield "an answer"

    gateway = IRAModelGateway(
        costs={"ollama-local": {"prompt": Decimal("0"), "completion": Decimal("0")}})
    gateway.register(CountingProvider())
    gateway.health_check = lambda pid: True
    gateway.usage_writer = lambda principal, row: None
    return gateway, calls


async def test_a_tenant_with_no_subscription_has_no_entitlements():
    """The state of every tenant in a deployment that has sold nothing."""
    assert await entitlements.effective_limits(_p(await _tenant())) == {}


async def test_a_subscription_brings_its_plans_entitlements():
    tenant = await _tenant()
    await _plan_with("requests_per_day", 7)
    await _subscribe(tenant)
    assert await entitlements.effective_limits(_p(tenant)) == {"requests_per_day": 7}


async def test_a_cancelled_subscription_brings_nothing():
    """`canceled` is excluded by the resolver's status filter.

    The row remains — 078 withholds DELETE so that "never subscribed" and
    "cancelled" stay distinguishable — so the entitlement must be dropped by the
    QUERY rather than by the row's absence.
    """
    tenant = await _tenant()
    await _plan_with("requests_per_day", 7)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO tenant_subscriptions "
            "(tenant_id, plan_id, status, canceled_at, current_period_end) "
            "VALUES ($1,$2,'canceled', NOW(), NOW() + INTERVAL '30 days')",
            tenant, PLAN)
    finally:
        await conn.close()
    assert await entitlements.effective_limits(_p(tenant)) == {}


async def test_an_override_beats_the_plan():
    tenant = await _tenant()
    await _plan_with("requests_per_day", 7)
    await _subscribe(tenant)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO tenant_entitlement_overrides "
            "(tenant_id, key, limit_value, granted_by) VALUES ($1,'requests_per_day',3,'test')",
            tenant)
    finally:
        await conn.close()
    assert await entitlements.effective_limits(_p(tenant)) == {"requests_per_day": 3}


async def test_one_tenants_entitlements_are_not_anothers():
    a, b = await _tenant(), await _tenant()
    await _plan_with("requests_per_day", 7)
    await _subscribe(a)
    assert await entitlements.effective_limits(_p(a)) == {"requests_per_day": 7}
    assert await entitlements.effective_limits(_p(b)) == {}, (
        "tenant B inherited tenant A's entitlements — RLS is not scoping the read")


# ── 2. Narrowing: lower only, and NULL is not 0 ──────────────────────────────

async def test_an_entitlement_creates_a_ceiling_where_the_quota_had_none():
    """The commercially important case: no usage_quotas row, but a plan limit."""
    tenant = await _tenant()
    await _plan_with("requests_per_day", 5)
    await _subscribe(tenant)
    narrowed = await entitlements.narrow(None, _p(tenant))
    assert narrowed is not None and narrowed.daily_request_limit == 5


async def test_an_entitlement_never_loosens_a_stricter_quota():
    tenant = await _tenant()
    await _plan_with("requests_per_day", 100)
    await _subscribe(tenant)
    narrowed = await entitlements.narrow(Quota(daily_request_limit=2), _p(tenant))
    assert narrowed.daily_request_limit == 2, (
        "a generous plan raised a stricter quota — entitlements must only lower")


async def test_unlimited_does_not_loosen_a_quota():
    """NULL means unlimited, and unlimited must not erase an existing ceiling."""
    tenant = await _tenant()
    await _plan_with("requests_per_day", None)
    await _subscribe(tenant)
    narrowed = await entitlements.narrow(Quota(daily_request_limit=2), _p(tenant))
    assert narrowed.daily_request_limit == 2


async def test_zero_is_not_unlimited():
    """0 means NONE. Collapsing it into NULL is how a blocked plan runs free."""
    tenant = await _tenant()
    await _plan_with("requests_per_day", 0)
    await _subscribe(tenant)
    narrowed = await entitlements.narrow(None, _p(tenant))
    assert narrowed is not None
    assert narrowed.daily_request_limit == 0
    assert check_quota(narrowed, UsageSoFar(daily_requests=0)).allowed is False


async def test_a_tenant_with_nothing_gets_its_quota_back_untouched():
    tenant = await _tenant()
    original = Quota(daily_request_limit=9)
    assert await entitlements.narrow(original, _p(tenant)) is original


# ── 3. The production reader applies it — the real caller ────────────────────

async def test_load_quota_returns_the_narrowed_ceiling():
    """`load_quota` is what `_admit` step 2 calls, via `composition.py`.

    This is the assertion that makes the entitlement real rather than
    resolvable: the function the gateway actually invokes returns the tightened
    number.
    """
    tenant = await _tenant()
    await _quota_row(tenant, daily=50)
    await _plan_with("requests_per_day", 4)
    await _subscribe(tenant)

    quota, _usage = await load_quota(_p(tenant))
    assert quota is not None
    assert quota.daily_request_limit == 4, (
        "load_quota returned the un-narrowed quota — the entitlement is not on "
        "the path the gateway takes")


# ── 4. What actually matters: the provider is never called ───────────────────

async def test_an_exhausted_entitlement_refuses_before_the_provider_is_called():
    """The property the whole batch exists for, asserted by COUNTING CALLS.

    A build that called the model and then reported `refused_quota` would pass a
    test that read the outcome field. This one counts, and the number is zero.
    """
    tenant = await _tenant()
    await _plan_with("requests_per_day", 1)
    await _subscribe(tenant)
    await _quota_row(tenant, daily=None)

    calls: list[int] = []

    async def _provider_would_be_called():
        calls.append(1)
        return {"text": "should never happen"}

    quota, _usage = await load_quota(_p(tenant))
    # One request already spent today: the ceiling of 1 is reached.
    verdict = check_quota(quota, UsageSoFar(daily_requests=1))

    if verdict.allowed:
        await _provider_would_be_called()

    assert verdict.allowed is False, (
        "the entitlement of 1/day did not refuse a tenant that had already made "
        "one request today")
    assert calls == [], "the provider was called for a request the plan refused"


async def test_the_same_tenant_under_the_ceiling_is_admitted():
    """The other half. A gate that refuses everything is not a gate.

    Without this, `test_an_exhausted_entitlement_refuses…` would still pass on a
    build that refused unconditionally — which is the vacuous-pass shape this
    repository keeps finding.
    """
    tenant = await _tenant()
    await _plan_with("requests_per_day", 5)
    await _subscribe(tenant)
    await _quota_row(tenant, daily=None)

    calls: list[int] = []

    async def _provider():
        calls.append(1)
        return {"text": "ok"}

    quota, _usage = await load_quota(_p(tenant))
    verdict = check_quota(quota, UsageSoFar(daily_requests=1))
    if verdict.allowed:
        await _provider()

    assert verdict.allowed is True, verdict.reason
    assert calls == [1], "a request within the plan's limit was refused"


# ── 5. Fail closed ───────────────────────────────────────────────────────────

async def test_an_entitlement_lookup_failure_does_not_return_an_unnarrowed_quota(
        monkeypatch):
    """A swallowed failure here returns the MOST PERMISSIVE answer available.

    `load_quota` already refuses to assume unmetered when the quota read fails,
    and the entitlement read has to inherit that: catching the error and
    returning the quota un-narrowed would silently restore the wider ceiling the
    plan exists to reduce.
    """
    tenant = await _tenant()
    await _quota_row(tenant, daily=50)
    await _plan_with("requests_per_day", 4)
    await _subscribe(tenant)

    async def _boom(_quota, _principal):
        raise RuntimeError("entitlement store unreachable")

    monkeypatch.setattr(entitlements, "narrow", _boom)
    import core.models.store as store

    from core.models.store import QuotaUnavailable
    with pytest.raises(QuotaUnavailable) as exc:
        await store.load_quota(_p(tenant))
    assert "entitlements" in str(exc.value), (
        "the failure surfaced without naming what could not be read")


# ── The enforcement point itself, not a reconstruction of it ─────────────────
#
# The two tests above this section assert the right property with the wrong
# subject. They call `load_quota` and `check_quota` directly and then the TEST
# decides whether to call the provider:
#
#     if verdict.allowed:
#         await _provider_would_be_called()
#
# So `calls == []` proves that the test's own `if` worked. It does NOT prove
# that `IRAModelGateway._admit()` refuses before reaching a provider — a build
# that called the model first and checked the entitlement afterwards would pass
# them both. The reconstruction IS the defect: enforcement asserted beside the
# runtime rather than through it.
#
# These drive the real gateway. The provider counts its own invocations, so the
# number is produced by the code under test rather than by the test.

async def test_the_real_gateway_refuses_before_the_provider_is_reached():
    """`gateway.complete()` — the actual Phase 8 admission path.

    The entitlement is 1 request/day and the tenant has already made one, so
    admission must refuse. The provider increments a counter the moment it is
    entered; the assertion is that the counter never moved.
    """
    tenant = await _tenant()
    await _plan_with("requests_per_day", 1)
    await _subscribe(tenant)
    await _quota_row(tenant, daily=None)

    # The ceiling is reached by RECORDING A REAL REQUEST, not by handing the
    # checker a `UsageSoFar` the test invented. The first version of this test
    # left usage at zero, so admitting was CORRECT and the provider was reached
    # — the premise was wrong, not the product.
    await _record_one_request(tenant)

    gateway, calls = _gateway_with_a_counting_provider()
    # The REAL resolver, entitlement narrowing and all — not a stub returning a
    # quota the test chose.
    gateway.quota_reader = lambda principal: load_quota(principal)

    result = await gateway.complete(
        _completion_request(), principal=_p(tenant), privacy_mode="balanced")

    assert calls == [], (
        "the provider was entered for a request the entitlement refused. The "
        "tenant is billed and the model is paid for work that should never "
        "have started.")
    assert _is_refusal(result), f"the gateway did not refuse: {result!r}"


async def test_the_real_gateway_admits_and_reaches_the_provider_under_the_ceiling():
    """The positive control, without which the test above passes on a gateway
    that refuses everything — which would be maximally safe and useless."""
    tenant = await _tenant()
    await _plan_with("requests_per_day", 50)
    await _subscribe(tenant)
    await _quota_row(tenant, daily=None)

    gateway, calls = _gateway_with_a_counting_provider()
    gateway.quota_reader = lambda principal: load_quota(principal)

    await gateway.complete(
        _completion_request(), principal=_p(tenant), privacy_mode="balanced")

    assert calls == [1], (
        "a request comfortably inside the plan's limit never reached a "
        "provider")
