"""Phase 8 — policy is consulted before availability and cost, and quota is a
ceiling rather than a suggestion.

Two properties, and the first is the one under pressure.

**Order.** A gateway that checks availability and cost first has already
decided, and the policy check becomes a veto it is tempted to soften. So a
recording double observes the sequence and these tests fail if availability is
consulted before the policy has produced the candidate set.

**No pressure widens the candidate set.** The local model being down, a provider
failing mid-call, and the quota being exhausted are the three moments a routing
layer wants to reach for something the policy refused. Each is tested
separately, because each is a different line of code that could get it wrong.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from core.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    Locality,
    ProviderDisclosure,
    Tier,
)
from core.models.gateway import (
    IRAModelGateway,
    Quota,
    QuotaRefusal,
    QuotaVerdict,
    UsageSoFar,
    check_quota,
    estimate_cost,
)
from core.models.policy import PrivacyMode, RoutingRefusal

pytestmark = pytest.mark.asyncio


def disclosure(pid, locality, retains=False, trains=False) -> ProviderDisclosure:
    return ProviderDisclosure(
        provider_id=pid, display_name=pid.title(), locality=locality,
        retains_prompts=retains, region="eu", trains_on_data=trains,
    )


@dataclass
class FakeProvider:
    """A provider that records that it was called, and can be made to fail."""

    pid: str
    locality: Locality
    trace: list[str]
    retains: bool = False
    trains: bool = False
    fails: bool = False
    tiers: tuple = (Tier.FAST, Tier.DEEP)
    prompt_tokens: int = 100
    completion_tokens: int = 200

    @property
    def provider_id(self) -> str:
        return self.pid

    def disclosure(self) -> ProviderDisclosure:
        return disclosure(self.pid, self.locality, self.retains, self.trains)

    def supports(self, tier: Tier) -> bool:
        return tier in self.tiers

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.trace.append(f"call:{self.pid}")
        if self.fails:
            raise RuntimeError("provider exploded")
        return CompletionResult(
            text="an answer", model=f"{self.pid}-model", provider_id=self.pid,
            disclosure=self.disclosure(),
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )

    async def stream(self, request):  # pragma: no cover - unused here
        yield "an answer"


class FakePrincipal:
    tenant_id = uuid.UUID("f0f0f0f0-0000-4000-8000-00000000000a")
    user_id = uuid.UUID("f0f0f0f0-0000-4000-8000-0000000000aa")
    username = "owner"


def build(
    *,
    trace: list[str],
    quota: Quota | None = None,
    usage: UsageSoFar | None = None,
    local_up: bool = True,
    local_fails: bool = False,
    with_external: bool = True,
    external_retains: bool = False,
):
    metered: list[dict] = []
    local = FakeProvider("ollama-local", Locality.LOCAL, trace, fails=local_fails)
    gateway = IRAModelGateway(
        costs={
            "ollama-local": {"prompt": Decimal("0"), "completion": Decimal("0")},
            "clean-cloud": {"prompt": Decimal("0.5"), "completion": Decimal("1.5")},
        },
    )
    gateway.register(local)
    if with_external:
        gateway.register(
            FakeProvider("clean-cloud", Locality.EXTERNAL, trace,
                         retains=external_retains)
        )

    def health(pid: str) -> bool:
        trace.append(f"health:{pid}")
        return local_up if pid == "ollama-local" else True

    def read_quota(_principal):
        trace.append("quota")
        return quota, usage or UsageSoFar()

    def write_usage(_principal, row):
        trace.append("meter")
        metered.append(row)

    gateway.health_check = health
    gateway.quota_reader = read_quota
    gateway.usage_writer = write_usage
    return gateway, metered


def request(sensitivity="normal", tier=Tier.FAST) -> CompletionRequest:
    return CompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        tier=tier, sensitivity=sensitivity,
    )


# ── Order ────────────────────────────────────────────────────────────────────


async def test_the_quota_is_checked_before_availability():
    """Policy → quota → availability → cost. Availability first would mean the
    gateway had already chosen before the policy was consulted."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="balanced"
    )
    assert isinstance(result, CompletionResult)
    assert trace.index("quota") < trace.index("health:ollama-local")
    assert trace.index("health:ollama-local") < trace.index("call:ollama-local")


async def test_nothing_is_called_when_the_policy_admits_nothing():
    """local_only with only an external provider registered."""
    trace: list[str] = []
    gateway, metered = build(trace=trace, with_external=True)
    gateway.providers.pop("ollama-local")
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="local_only"
    )
    assert isinstance(result, RoutingRefusal)
    assert not [t for t in trace if t.startswith("call:")]
    assert metered and metered[0]["outcome"] == "refused_privacy"


async def test_availability_is_never_consulted_for_a_refused_provider():
    """Checking health on a provider the policy already excluded is harmless in
    itself, but it is the shape of a gateway that will later 'fall back' to it."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    await gateway.complete(
        request(sensitivity="health"), principal=FakePrincipal(),
        privacy_mode="balanced",
    )
    assert "health:clean-cloud" not in trace, (
        "the gateway checked whether a policy-refused provider was up"
    )


# ── No pressure widens the candidate set ─────────────────────────────────────


async def test_the_local_model_being_down_does_not_produce_an_external_call():
    """The single most important test in this file.

    `local_first` with sensitive content admits only local. If local is down the
    answer is a refusal — not the external provider that was sitting right there.
    """
    trace: list[str] = []
    gateway, metered = build(trace=trace, local_up=False)
    result = await gateway.complete(
        request(sensitivity="health"), principal=FakePrincipal(),
        privacy_mode="local_first",
    )
    assert isinstance(result, RoutingRefusal)
    assert "call:clean-cloud" not in trace, (
        "sensitive content was sent to a third party because the local model "
        "was down — this is the leak the whole phase exists to prevent"
    )
    assert "not reachable" in result.reason or "did not send" in result.reason


async def test_a_provider_failing_mid_call_does_not_retry_elsewhere():
    """A failed call is not a licence to widen the candidate set. Retrying
    elsewhere is exactly how a request the policy confined to local ends up at a
    third party."""
    trace: list[str] = []
    gateway, metered = build(trace=trace, local_fails=True)
    result = await gateway.complete(
        request(sensitivity="financial"), principal=FakePrincipal(),
        privacy_mode="local_first",
    )
    assert isinstance(result, RoutingRefusal)
    assert "call:ollama-local" in trace
    assert "call:clean-cloud" not in trace
    assert metered[-1]["outcome"] == "error"


async def test_an_exhausted_quota_never_widens_the_candidate_set():
    """`on_exceeded='local_only'` narrows to local. It must not be able to make
    a request go somewhere the policy refused — a billing rule making a privacy
    decision is the wrong place for one."""
    trace: list[str] = []
    gateway, _ = build(
        trace=trace,
        quota=Quota(monthly_token_limit=1000, on_exceeded="local_only"),
        usage=UsageSoFar(monthly_tokens=5000),
    )
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="external_ok"
    )
    assert isinstance(result, CompletionResult)
    assert result.provider_id == "ollama-local", (
        "an exhausted quota routed to an external provider"
    )
    assert "call:clean-cloud" not in trace


async def test_local_only_degradation_with_no_local_provider_refuses():
    """Narrowing to local when there is no local provider must refuse, not fall
    back to the external one the quota was trying to avoid."""
    trace: list[str] = []
    gateway, _ = build(
        trace=trace,
        quota=Quota(monthly_token_limit=10, on_exceeded="local_only"),
        usage=UsageSoFar(monthly_tokens=500),
    )
    gateway.providers.pop("ollama-local")
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="external_ok"
    )
    assert isinstance(result, QuotaRefusal)
    assert "call:clean-cloud" not in trace


# ── Quota is a ceiling ───────────────────────────────────────────────────────


async def test_an_exhausted_hard_quota_refuses_before_the_call():
    trace: list[str] = []
    gateway, metered = build(
        trace=trace,
        quota=Quota(monthly_token_limit=1000, on_exceeded="refuse"),
        usage=UsageSoFar(monthly_tokens=1000),
    )
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="balanced"
    )
    assert isinstance(result, QuotaRefusal)
    assert not [t for t in trace if t.startswith("call:")]
    assert metered[-1]["outcome"] == "refused_quota"


async def test_a_quota_refusal_does_not_blame_privacy():
    """A user told "your privacy settings prevented this" when they have run out
    of budget will change the wrong setting — and may loosen their privacy to fix
    a billing problem."""
    gateway, _ = build(
        trace=[],
        quota=Quota(monthly_cost_limit=Decimal("10"), on_exceeded="refuse"),
        usage=UsageSoFar(monthly_cost=Decimal("12")),
    )
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="balanced"
    )
    payload = result.public_dict()
    assert payload["kind"] == "quota"
    assert "billing limit, not a privacy refusal" in payload["reason"]


@pytest.mark.parametrize("quota,usage,expected", [
    (None, UsageSoFar(), True),
    (Quota(), UsageSoFar(monthly_tokens=10**9), True),          # unmetered
    (Quota(monthly_token_limit=100), UsageSoFar(monthly_tokens=99), True),
    (Quota(monthly_token_limit=100), UsageSoFar(monthly_tokens=100), False),
    (Quota(daily_request_limit=5), UsageSoFar(daily_requests=5), False),
    (Quota(monthly_cost_limit=Decimal("1")), UsageSoFar(monthly_cost=Decimal("1")), False),
])
async def test_the_ceiling_is_inclusive(quota, usage, expected):
    """At the limit means used up. `>` rather than `>=` would grant one extra
    request per period per tenant, which is a real billing discrepancy at scale."""
    assert check_quota(quota, usage).allowed is expected


async def test_unmetered_and_zero_are_different():
    """Conflating them makes a build that either bills nothing or refuses
    everything, and from outside those look like the same bug."""
    assert Quota().unmetered is True
    assert Quota(monthly_token_limit=0).unmetered is False
    assert check_quota(Quota(monthly_token_limit=0), UsageSoFar()).allowed is False


async def test_each_limit_names_itself():
    """"Quota exceeded" leaves an operator unable to tell which limit to raise."""
    reasons = set()
    for quota, usage in (
        (Quota(monthly_token_limit=1), UsageSoFar(monthly_tokens=2)),
        (Quota(monthly_cost_limit=Decimal("1")), UsageSoFar(monthly_cost=Decimal("2"))),
        (Quota(daily_request_limit=1), UsageSoFar(daily_requests=2)),
    ):
        verdict = check_quota(quota, usage)
        assert verdict.reason
        reasons.add(verdict.which_limit)
    assert reasons == {"tokens:", "cost:", "requests:"}


# ── Metering ─────────────────────────────────────────────────────────────────


async def test_a_successful_call_is_metered_with_where_it_went():
    trace: list[str] = []
    gateway, metered = build(trace=trace)
    await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="balanced"
    )
    row = metered[-1]
    assert row["outcome"] == "ok"
    assert row["locality"] in ("local", "self_hosted", "external")
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 200
    assert row["privacy_mode"] == "balanced"
    assert row["sensitivity"] == "normal"


async def test_no_metered_field_could_hold_a_prompt():
    """The usage row is the most-exported table in the system. Nothing in it may
    carry what the user said."""
    trace: list[str] = []
    gateway, metered = build(trace=trace)
    await gateway.complete(
        CompletionRequest(
            messages=[{"role": "user", "content": "my private health question"}],
            tier=Tier.FAST, sensitivity="normal",
        ),
        principal=FakePrincipal(), privacy_mode="balanced",
    )
    rendered = repr(metered[-1])
    assert "private health question" not in rendered
    assert "messages" not in metered[-1]


async def test_the_model_name_is_truncated_rather_than_violating_its_column():
    """The column is bounded at 128. Failing the metering write while recording a
    problem would lose the record of the problem."""
    trace: list[str] = []
    gateway, metered = build(trace=trace)
    gateway.providers["ollama-local"].pid = "ollama-local"
    provider = gateway.providers["ollama-local"]

    async def long_model(_request):
        return CompletionResult(
            text="x", model="m" * 500, provider_id="ollama-local",
            disclosure=provider.disclosure(), prompt_tokens=1, completion_tokens=1,
        )

    provider.complete = long_model  # type: ignore[assignment]
    await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="local_only"
    )
    assert len(metered[-1]["model"]) == 128


async def test_a_metering_failure_does_not_fail_a_completed_answer():
    """Fail-soft, narrowly: the answer already exists, and reporting it as failed
    would be the wrong lie."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)

    def explode(*_args, **_kwargs):
        raise RuntimeError("usage store down")

    gateway.usage_writer = explode
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="balanced"
    )
    assert isinstance(result, CompletionResult)


async def test_refusals_are_metered_too():
    """A tenant whose privacy settings refused every request is a fact worth
    having, and a usage table of successes cannot show it."""
    trace: list[str] = []
    gateway, metered = build(trace=trace, with_external=True)
    gateway.providers.pop("ollama-local")
    await gateway.complete(
        request(sensitivity="health"), principal=FakePrincipal(),
        privacy_mode="local_first",
    )
    assert metered[-1]["outcome"] == "refused_privacy"
    assert metered[-1]["prompt_tokens"] == 0
    assert metered[-1]["cost"] == Decimal("0")


# ── Cost ─────────────────────────────────────────────────────────────────────


async def test_cost_is_decimal_not_float():
    """Floats produce off-by-a-penny ceilings, which present as a quota that
    triggers one request early or late — very hard to diagnose from a report."""
    cost = estimate_cost(
        {"prompt": Decimal("0.5"), "completion": Decimal("1.5")},
        prompt_tokens=1000, completion_tokens=2000,
    )
    assert isinstance(cost, Decimal)
    assert cost == Decimal("3.5")


async def test_a_free_provider_costs_nothing():
    assert estimate_cost({}, prompt_tokens=10**6, completion_tokens=10**6) == Decimal("0")


async def test_the_cheapest_admissible_provider_wins():
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="external_ok"
    )
    # Local is free, so it wins on cost as well as on privacy.
    assert result.provider_id == "ollama-local"


async def test_a_tie_on_cost_is_broken_towards_local():
    """A tie should not be broken towards a third party."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    gateway.costs["clean-cloud"] = {"prompt": Decimal("0"), "completion": Decimal("0")}
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="external_ok"
    )
    assert result.provider_id == "ollama-local"


async def test_a_tier_nothing_supports_refuses_rather_than_calling_anyway():
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    result = await gateway.complete(
        request(tier=Tier.VISION), principal=FakePrincipal(),
        privacy_mode="external_ok",
    )
    assert isinstance(result, RoutingRefusal)
    assert "does not support" in result.reason or "did not send" in result.reason
    assert not [t for t in trace if t.startswith("call:")]


# ── The settings surface ─────────────────────────────────────────────────────


async def test_available_providers_is_unfiltered_so_a_user_can_see_what_is_excluded():
    """A user deciding whether to loosen their privacy mode needs to see the
    providers their current mode is excluding."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    ids = {d.provider_id for d in gateway.available_providers()}
    assert ids == {"ollama-local", "clean-cloud"}


async def test_an_absent_health_check_assumes_reachable_rather_than_down():
    """Assuming DOWN would make a working deployment with no health check refuse
    everything. A provider that is actually down fails at the call and is
    metered as an error."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    gateway.health_check = None
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="local_only"
    )
    assert isinstance(result, CompletionResult)


async def test_a_raising_health_check_is_treated_as_down():
    trace: list[str] = []
    gateway, _ = build(trace=trace)

    def explode(_pid):
        raise RuntimeError("health endpoint down")

    gateway.health_check = explode
    result = await gateway.complete(
        request(), principal=FakePrincipal(), privacy_mode="local_only"
    )
    assert isinstance(result, RoutingRefusal)


async def test_no_principal_means_unmetered_rather_than_refused():
    """Internal calls with no principal must still work — refusing them would
    break every background path that has no user attached."""
    trace: list[str] = []
    gateway, _ = build(trace=trace)
    result = await gateway.complete(request(), principal=None,
                                    privacy_mode="local_only")
    assert isinstance(result, CompletionResult)
