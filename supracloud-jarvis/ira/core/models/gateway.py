"""The Model Gateway — the only thing that may call a provider.

Implements the `ModelGateway` protocol from `core/interfaces/model_provider.py`,
whose contract is the one this module is organised around:

    routing must consider the principal's privacy policy and the content's
    sensitivity BEFORE availability or cost, and must refuse rather than
    downgrade privacy.

So `complete()` runs in a fixed order and each step can only narrow:

    1. POLICY      which providers may carry this at all      policy.py
    2. QUOTA       has this tenant any budget left            here
    3. AVAILABILITY which of the survivors is actually up     here
    4. COST        cheapest of what remains                   here
    5. CALL        the provider                               the provider
    6. METER       tokens, cost, and where it went            store.py

Availability and cost are steps 3 and 4 on purpose. A gateway that consults them
first and then checks policy has already decided, and the policy check becomes a
veto it is tempted to soften. Here the policy produces the candidate set and the
later steps can only pick from it.

── Why a refusal is not a fallback ─────────────────────────────────────────

When step 1 leaves nothing, `complete()` returns a `RoutingRefusal`. It does not
try the next-best provider, and there is no parameter that lets a caller ask it
to. That is the single most important line in this module, because every
operational pressure argues the other way: the local model is down, the user is
waiting, and an external provider would answer immediately.

── Quotas are checked here, and metered here ───────────────────────────────

Both at the chokepoint, for the same reason execution requires a ticket in
Phase 4: a limit enforced at each call site is a limit that one new call site
forgets. `usage_before` is consulted before the call and `record_usage` after,
and a refusal is metered too — a tenant that hit its ceiling ten thousand times
is a fact worth having.

`on_exceeded='local_only'` degrades CAPABILITY, never privacy: it narrows the
candidate set to local providers, which is strictly stricter than what the
policy already allowed. Nothing here can make a request go somewhere the policy
refused.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from core.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    Locality,
    ModelProvider,
    ProviderDisclosure,
    ProviderTimeout,
    Tier,
)
from core.models.policy import (
    PrivacyMode,
    RoutingRefusal,
    admissible,
    normalise_mode,
    normalise_sensitivity,
    refusal_for,
)

logger = logging.getLogger("ira.models.gateway")


@dataclass(frozen=True, slots=True)
class Quota:
    """A tenant's ceilings. `None` on a limit means unmetered.

    Unmetered and zero are deliberately different values. Conflating them would
    make a build that either bills nothing or refuses everything, and from
    outside those look like the same bug.
    """

    monthly_token_limit: Optional[int] = None
    monthly_cost_limit: Optional[Decimal] = None
    daily_request_limit: Optional[int] = None
    on_exceeded: str = "refuse"

    @property
    def unmetered(self) -> bool:
        return (
            self.monthly_token_limit is None
            and self.monthly_cost_limit is None
            and self.daily_request_limit is None
        )


@dataclass(frozen=True, slots=True)
class UsageSoFar:
    """What this tenant has already spent in the current period."""

    monthly_tokens: int = 0
    monthly_cost: Decimal = Decimal("0")
    daily_requests: int = 0


@dataclass(frozen=True, slots=True)
class QuotaVerdict:
    """Whether the quota permits a call, and what it permits instead."""

    allowed: bool
    reason: str = ""
    #: True when the quota is exhausted but the tenant may still use local
    #: models. Narrower than what the policy allowed — never wider.
    local_only: bool = False

    @property
    def which_limit(self) -> str:
        return self.reason.split(" ", 1)[0] if self.reason else ""


def check_quota(quota: Optional[Quota], usage: UsageSoFar) -> QuotaVerdict:
    """Compare usage against the ceilings. Named limits, in a fixed order.

    Each limit produces its own message, because "quota exceeded" leaves an
    operator unable to tell whether to raise a token limit, a spend limit, or a
    request limit.
    """
    if quota is None or quota.unmetered:
        return QuotaVerdict(allowed=True)

    exceeded = ""
    if (
        quota.monthly_token_limit is not None
        and usage.monthly_tokens >= quota.monthly_token_limit
    ):
        exceeded = (
            f"tokens: {usage.monthly_tokens} of {quota.monthly_token_limit} used "
            f"this month"
        )
    elif (
        quota.monthly_cost_limit is not None
        and usage.monthly_cost >= quota.monthly_cost_limit
    ):
        exceeded = (
            f"cost: {usage.monthly_cost} of {quota.monthly_cost_limit} used this "
            f"month"
        )
    elif (
        quota.daily_request_limit is not None
        and usage.daily_requests >= quota.daily_request_limit
    ):
        exceeded = (
            f"requests: {usage.daily_requests} of {quota.daily_request_limit} "
            f"used today"
        )

    if not exceeded:
        return QuotaVerdict(allowed=True)

    if quota.on_exceeded == "local_only":
        # Degrades CAPABILITY, not privacy: local is strictly stricter than
        # whatever the policy already permitted.
        return QuotaVerdict(
            allowed=True, local_only=True,
            reason=f"{exceeded}; continuing with local models only",
        )
    return QuotaVerdict(allowed=False, reason=exceeded)


@dataclass(frozen=True, slots=True)
class QuotaRefusal:
    """The quota said no. Distinct from a `RoutingRefusal` on purpose.

    A user who is told "your privacy settings prevented this" when they have
    actually run out of budget will change the wrong setting — and may loosen
    their privacy to fix a billing problem, which is the worst possible outcome
    of a confusing message.
    """

    reason: str
    limit: str = ""

    @property
    def ok(self) -> bool:
        return False

    def public_dict(self) -> dict[str, object]:
        return {
            "refused": True,
            "kind": "quota",
            "reason": (
                f"This request was not sent because your usage limit has been "
                f"reached ({self.reason}). This is a billing limit, not a "
                f"privacy refusal — nothing about your privacy settings caused "
                f"it."
            ),
            "limit": self.limit,
        }


def estimate_cost(
    disclosure_costs: Mapping[str, Decimal],
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal:
    """Cost in whole currency units. Decimal throughout.

    Floats here produce off-by-a-penny ceilings that present as a quota that
    triggers one request early or late, which is very hard to diagnose from a
    bug report.
    """
    prompt_rate = Decimal(str(disclosure_costs.get("prompt", 0)))
    completion_rate = Decimal(str(disclosure_costs.get("completion", 0)))
    return (
        (Decimal(prompt_tokens) / 1000) * prompt_rate
        + (Decimal(completion_tokens) / 1000) * completion_rate
    )


@dataclass(frozen=True, slots=True)
class Admission:
    """A request that survived policy, quota, availability, cost and registry.

    Phase 8 introduced this so `complete()` and `stream()` could share one
    admission sequence. It is deliberately a record of DECISIONS ALREADY MADE
    and carries no method that could make another: `request` already has the
    approved model attached, `chosen` is already the cheapest reachable
    admissible provider, and `reserved` already records whether a quota slot was
    taken. A caller holding one cannot widen anything, only spend it.
    """

    request: CompletionRequest
    chosen: ProviderDisclosure
    approved: Optional[Any]
    reserved: bool
    mode: Any
    sensitivity: str
    rejections: tuple[tuple[str, str], ...]
    started: float


@dataclass
class IRAModelGateway:
    """The gateway. Collaborators injected so the ordering can be observed.

    `providers` maps provider_id → a `ModelProvider`. `costs` maps provider_id →
    {"prompt": rate, "completion": rate}. Both come from the catalogue in
    `029_commercial.sql` in production, and from fixtures in tests.
    """

    providers: dict[str, ModelProvider] = field(default_factory=dict)
    costs: dict[str, Mapping[str, Decimal]] = field(default_factory=dict)
    #: Returns (Quota | None, UsageSoFar). Injected; None means unmetered.
    quota_reader: Optional[Any] = None
    #: Records a usage row. Injected; None means do not meter.
    usage_writer: Optional[Any] = None
    #: Atomically takes one slot from today's request ceiling; returns bool.
    #:
    #: `quota_reader` + `check_quota` is check-then-act, and under concurrency
    #: every caller reads the same under-limit snapshot before any of them
    #: writes — 24 of 24 concurrent requests went through a ceiling of 5 in
    #: `tests/readiness/test_behaviour_under_load.py`. A count cannot fix that;
    #: it describes the past. `core.models.store.reserve_daily_request` makes
    #: admission a single conditional UPDATE instead.
    #:
    #: None means the daily ceiling is advisory only, which is what every
    #: existing unit test that injects its own collaborators gets.
    quota_reserver: Optional[Any] = None
    #: Hands a reserved slot back when the request is refused for a reason that
    #: is not the tenant's doing. Injected alongside `quota_reserver`.
    quota_releaser: Optional[Any] = None
    #: Returns True when a provider is reachable. Injected so "the local model
    #: is down" is expressible in a test without breaking anything.
    health_check: Optional[Any] = None
    #: MOD-03. When True, the registry decides the exact model, version and
    #: revision BEFORE any provider is called, and a provider that runs
    #: something else has its answer discarded.
    #:
    #: Default False so that every deployment which has not yet registered its
    #: models keeps working. Off means the registry is NOT CONSULTED — not
    #: consulted and ignored, which is the shape that passes silently.
    registry_required: bool = False
    #: Tier → registry `model_key`. Injected, and server-side by construction:
    #: a request names a tier, never a model. Defaults to the tier's own name,
    #: which is what a deployment registering `fast`/`deep` would use.
    tier_model_keys: Mapping[str, str] = field(default_factory=dict)

    def register(self, provider: ModelProvider) -> None:
        self.providers[provider.provider_id] = provider

    def available_providers(self) -> Sequence[ProviderDisclosure]:
        """Every registered provider's disclosure, unfiltered.

        Unfiltered on purpose: this is what a settings screen shows, and a user
        deciding whether to loosen their privacy mode needs to see the providers
        their current mode is excluding.
        """
        return tuple(p.disclosure() for p in self.providers.values())

    # ── The single entry point ───────────────────────────────────────────────

    async def complete(
        self,
        request: CompletionRequest,
        *,
        principal: Optional[Any] = None,
        privacy_mode: Optional[str] = None,
    ) -> CompletionResult | RoutingRefusal | QuotaRefusal:
        """Route, check, call, meter. In that order, always.

        Returns a `CompletionResult`, a `RoutingRefusal` or a `QuotaRefusal`.
        Three outcomes rather than an exception, because all three are things a
        user must be shown and two of them are not errors.

        The one exception that leaves is `ProviderTimeout`: the provider had the
        request and did not answer in time. That is neither an answer nor a
        refusal, and it is never retried here.
        """
        admission, refusal = await self._admit(
            request, principal=principal, privacy_mode=privacy_mode)
        if refusal is not None:
            return refusal
        request = admission.request
        chosen = admission.chosen
        approved = admission.approved
        reserved = admission.reserved
        mode = admission.mode
        sensitivity = admission.sensitivity
        rejections = admission.rejections
        started = admission.started


        # ── 5. CALL. ────────────────────────────────────────────────────────
        provider = self.providers[chosen.provider_id]
        try:
            result = await provider.complete(request)
        except asyncio.CancelledError:
            # The caller stopped waiting: the chat route's deadline, or a
            # shutdown. The provider still had the request, so the call is
            # metered and the slot handed back, the way `_metered_stream`
            # treats a stream nobody finished reading.
            await self._meter(
                principal, provider_id=chosen.provider_id,
                locality=chosen.locality.value, tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="abandoned",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            await self._release(principal, reserved)
            raise
        except ProviderTimeout as exc:
            logger.warning("provider %s timed out: %s", chosen.provider_id, exc)
            await self._meter(
                principal, provider_id=chosen.provider_id,
                locality=chosen.locality.value, tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="error",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            await self._release(principal, reserved)
            # Raised, not turned into a refusal. The request reached the
            # provider, so "IRA did not send this anywhere" would be false, and
            # a refusal's reason comes back to the caller as if it were the
            # model's answer. A timeout is something the caller must report.
            raise
        except Exception as exc:  # noqa: BLE001 - metered, then re-raised shape
            logger.warning("provider %s failed: %s", chosen.provider_id, exc)
            await self._meter(
                principal, provider_id=chosen.provider_id,
                locality=chosen.locality.value, tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="error",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            # A provider failing is NOT a reason to try one the policy refused,
            # so this returns a refusal rather than retrying elsewhere.
            await self._release(principal, reserved)
            return refusal_for(
                mode=mode, sensitivity=sensitivity,
                rejections=tuple(rejections) + (
                    (chosen.provider_id, f"the call failed ({type(exc).__name__})"),
                ),
            )

        # ── 5b. REGISTRY. The model that answered must be one we registered. ─
        #
        # MOD-03. Placed HERE, after the call, and it is worth being exact about
        # why rather than letting a reader assume this is the natural place.
        #
        # The gateway chooses a PROVIDER, not a model. Which model a provider
        # uses for a tier is the provider's business — `utils/llm.py` reads it
        # from configuration, an Ollama deployment can change it by pulling a
        # different tag — and `ModelProvider` has no method that would let the
        # gateway ask beforehand. Adding one would let a provider answer
        # truthfully and then call something else, which is a check that
        # observes an intention rather than an act.
        #
        # `CompletionResult.model` is the act. So the enforcement is: a
        # completion produced by an unregistered model is never returned to the
        # caller, and never metered as `ok`.
        #
        # What this does NOT do, stated plainly because the difference matters:
        # it does not prevent the inference. The prompt has already reached the
        # provider by the time this runs. It prevents the *output* of an
        # unregistered model from being used, which is the property MOD-03 is
        # about — no model serves a tenant without a recorded artefact hash and
        # a recorded commercial licence. Preventing the call itself needs
        # providers to declare their models, and that belongs with whatever
        # phase makes providers declarable.
        refusal = self._identity_refusal(
            result, approved, mode=mode, sensitivity=sensitivity)
        if refusal is not None:
            await self._meter(
                principal, provider_id=chosen.provider_id,
                locality=chosen.locality.value, tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="refused_policy",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            # The tenant is not charged for a model we should not have called.
            await self._release(principal, reserved)
            return refusal

        # ── 6. METER. ───────────────────────────────────────────────────────
        cost = estimate_cost(
            self.costs.get(chosen.provider_id, {}),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        await self._meter(
            principal, provider_id=chosen.provider_id,
            locality=chosen.locality.value, tier=request.tier,
            sensitivity=sensitivity, mode=mode, outcome="ok",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost=cost, model=result.model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    async def _admit(
        self,
        request: CompletionRequest,
        *,
        principal: Optional[Any],
        privacy_mode: Optional[str],
    ) -> tuple[Optional["Admission"], Any]:
        """Steps 1 to 4b: policy, quota, availability, cost, registry.

        Extracted in Phase 8 so that `complete()` and `stream()` share one
        admission sequence rather than two that agree today. A streaming request
        must be refused for the same reasons and in the same ORDER as a
        non-streaming one, and the cheapest way to guarantee that is for there
        to be only one copy of the order.

        Returns `(admission, None)` when the request may proceed, and
        `(None, refusal)` when it may not. Everything that refuses in here has
        already metered the refusal and released any reserved slot, exactly as
        it did when this was inline.
        """
        started = time.monotonic()
        mode = normalise_mode(privacy_mode)
        sensitivity = normalise_sensitivity(request.sensitivity)

        # ── 1. POLICY. Before availability, before cost. ─────────────────────
        disclosures = [p.disclosure() for p in self.providers.values()]
        admitted, rejections = admissible(
            disclosures, mode=mode, sensitivity=sensitivity
        )

        if not admitted:
            refusal = refusal_for(
                mode=mode, sensitivity=sensitivity, rejections=rejections
            )
            await self._meter(
                principal, provider_id="", locality="local", tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="refused_privacy",
            )
            return None, refusal

        # ── 2. QUOTA. Before the call, so a ceiling is a ceiling. ────────────
        quota, usage = await self._quota_for(principal)
        verdict = check_quota(quota, usage)
        if not verdict.allowed:
            await self._meter(
                principal, provider_id="", locality="local", tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="refused_quota",
            )
            return None, QuotaRefusal(reason=verdict.reason, limit=verdict.which_limit)

        # The ceiling, enforced atomically rather than advisorily.
        #
        # `check_quota` above decided against a snapshot; this takes the slot.
        # Both are kept: the check produces the specific, useful refusal message
        # ("requests: 5 of 5 used today"), and the reservation is what actually
        # holds when 24 requests arrive together.
        if not await self._reserve(principal):
            await self._meter(
                principal, provider_id="", locality="local", tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="refused_quota",
            )
            return None, QuotaRefusal(
                reason=(
                    verdict.reason
                    or "the daily request limit for this tenant has been reached"
                ),
                limit=verdict.which_limit or "requests",
            )
        reserved = True

        if verdict.local_only:
            # Narrows to local. Strictly stricter than the policy's answer —
            # this cannot widen anything.
            admitted = tuple(
                d for d in admitted if d.locality is Locality.LOCAL
            )
            if not admitted:
                # Reserved above, and this request will not happen. Give it back.
                await self._release(principal, reserved)
                return None, QuotaRefusal(
                    reason=verdict.reason, limit=verdict.which_limit
                )

        # ── 3. AVAILABILITY, among the policy's survivors only. ─────────────
        reachable = [d for d in admitted if self._is_up(d.provider_id)]
        if not reachable:
            # Note what this does NOT do: it does not reach back to a provider
            # the policy refused. "Nothing available" is a refusal, not a
            # licence to widen the candidate set.
            await self._meter(
                principal, provider_id="", locality="local", tier=request.tier,
                sensitivity=sensitivity, mode=mode, outcome="error",
            )
            # An outage is not the tenant spending a request.
            await self._release(principal, reserved)
            return None, refusal_for(
                mode=mode, sensitivity=sensitivity,
                rejections=tuple(rejections) + tuple(
                    (d.provider_id, "the provider is not reachable")
                    for d in admitted
                ),
            )

        # ── 4. COST, cheapest of what survived. ─────────────────────────────
        chosen = self._cheapest(reachable, tier=request.tier)
        if chosen is None:
            await self._release(principal, reserved)
            return None, refusal_for(
                mode=mode, sensitivity=sensitivity,
                rejections=tuple(rejections) + tuple(
                    (d.provider_id, f"the provider does not support the "
                                    f"{request.tier.value} tier")
                    for d in reachable
                ),
            )

        # ── 4b. REGISTRY. Which model, exactly, BEFORE anything runs. ───────
        #
        # P5-07 moved this. It used to sit after the call and inspect
        # `result.model`, which meant an unregistered model still ran — the
        # prompt reached it and only the answer was discarded. "IRA does not use
        # unapproved models" was not true; "IRA does not return their output"
        # was. And a name cannot express a version, so `067`'s rollout and
        # rollback could not influence which weights answered.
        #
        # Now the registry resolves the exact model, version and revision first.
        # An unapproved model produces ZERO provider calls.
        approved = None
        if self.registry_required:
            approved, refusal = await self._approve(
                request, principal, mode=mode, sensitivity=sensitivity)
            if refusal is not None:
                await self._meter(
                    principal, provider_id=chosen.provider_id,
                    locality=chosen.locality.value, tier=request.tier,
                    sensitivity=sensitivity, mode=mode, outcome="refused_policy",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                await self._release(principal, reserved)
                return None, refusal
            request = replace(request, approved_model=approved)
        return Admission(
            request=request, chosen=chosen, approved=approved,
            reserved=reserved, mode=mode, sensitivity=sensitivity,
            rejections=tuple(rejections), started=started,
        ), None

    async def stream(
        self,
        request: CompletionRequest,
        *,
        principal: Optional[Any] = None,
        privacy_mode: Optional[str] = None,
    ):
        """The same route, yielding tokens. Phase 8.

        Returns either an async iterator of token strings, or a refusal — the
        same three outcomes `complete()` has, because a streaming request that
        the policy refuses must be refused, not started and then cut off.

        ── Everything is decided before the first token ────────────────────

        `_admit` runs in full before the provider is asked for a stream. That is
        the streaming version of what P5-07 fixed for completions: checking
        after the call meant the prompt had already reached the model and only
        the answer was discarded. A stream checked mid-flight is worse still,
        because some of the answer has already reached the user.

        ── Metering happens when the stream ends, and on every ending ──────

        A completion meters once, after one call. A stream can end three ways —
        exhausted, failed mid-flight, or abandoned by the caller — and only the
        first is success. All three are metered, in a `finally`, so a tenant who
        starts a thousand streams and abandons them all is visible rather than
        free.

        ── What is NOT counted, stated plainly ────────────────────────────

        Token counts are zero for streams. An OpenAI-compatible stream does not
        carry a usage block, and the honest options are to count zero or to
        estimate. Estimating would put a number nobody measured into a tenant's
        usage table and into any bill computed from it, so the row records the
        call, its provider, its latency and its outcome, and reports no tokens.
        The daily REQUEST ceiling therefore still applies to streams; the
        monthly TOKEN ceiling does not see them. That is a real limitation and
        it is recorded rather than papered over.
        """
        admission, refusal = await self._admit(
            request, principal=principal, privacy_mode=privacy_mode)
        if refusal is not None:
            return refusal

        provider = self.providers[admission.chosen.provider_id]
        return self._metered_stream(provider, admission, principal)

    async def _metered_stream(self, provider, admission: "Admission", principal):
        """Yield the provider's tokens, then meter exactly once."""
        outcome = "ok"
        try:
            async for token in provider.stream(admission.request):
                yield token
        except GeneratorExit:
            # The caller stopped reading — a closed browser tab, a cancelled
            # request. The provider still did work, so this is metered, and it
            # is metered as its own outcome rather than as success: a stream
            # nobody read is not an answer that was delivered.
            outcome = "abandoned"
            raise
        except Exception as exc:  # noqa: BLE001
            outcome = "error"
            logger.warning("provider %s stream failed: %s",
                           admission.chosen.provider_id, exc)
            raise
        finally:
            await self._meter(
                principal, provider_id=admission.chosen.provider_id,
                locality=admission.chosen.locality.value,
                tier=admission.request.tier,
                sensitivity=admission.sensitivity, mode=admission.mode,
                outcome=outcome,
                # Deliberately no tokens and no cost. See the docstring: an
                # estimate here would become a number in a bill.
                latency_ms=int((time.monotonic() - admission.started) * 1000),
            )
            if outcome != "ok":
                await self._release(principal, admission.reserved)

    # ── Steps, separated so the ordering test can observe them ──────────────

    async def _approve(self, request, principal, *, mode, sensitivity):
        """`(ApprovedModel, None)` if this request may run, `(None, refusal)`.

        Everything about WHICH model is decided here, from the server's own
        state: the tier names the model key, the registry names the serving
        version, the rollout names which version this tenant gets, and the
        allowlist confirms the artefact may exist at that revision. Nothing in
        the request participates.

        A request that arrives carrying `approved_model` is refused outright. It
        is not a caller being helpful — it is the shape of a tenant choosing
        which model approves it, and the same shape as `X-Workspace-ID` being
        trusted.
        """
        from core.models import registry as registry_module

        if getattr(request, "approved_model", None) is not None:
            logger.warning("a request arrived with approved_model already set")
            return None, RoutingRefusal(
                reason=("This request specified which model must serve it. IRA "
                        "decides that from its own registry."),
                privacy_mode=mode, sensitivity=sensitivity,
                considered=(("request", "carried an approved model"),))

        model_key = self.model_key_for(request.tier)
        if not model_key:
            return None, RoutingRefusal(
                reason=(f"No model is configured for the "
                        f"{request.tier.value} tier, so IRA cannot establish "
                        f"which model would be permitted to answer."),
                privacy_mode=mode, sensitivity=sensitivity,
                considered=((request.tier.value, "no model key configured"),))

        tenant_id = getattr(principal, "tenant_id", None)
        try:
            approved = await registry_module.resolve_for_request(
                model_key, tenant_id)
        except registry_module.ModelNotRegistered as exc:
            logger.warning("no approved model for %s: %s", model_key, exc)
            return None, RoutingRefusal(
                reason=("No model is currently approved to answer this "
                        "request, so nothing was sent to a model at all."),
                privacy_mode=mode, sensitivity=sensitivity,
                considered=((model_key, "not registered as serving"),))
        except registry_module.ArtefactNotAllowlisted as exc:
            logger.warning("approved model is not allowlisted: %s", exc)
            return None, RoutingRefusal(
                reason=("The model that would answer is registered, but its "
                        "artefact is not approved for use at that revision."),
                privacy_mode=mode, sensitivity=sensitivity,
                considered=((model_key, "artefact not allowlisted"),))
        except Exception as exc:  # noqa: BLE001
            # Fail closed. Treating an unreadable registry as "allow" turns a
            # database blip into an unapproved model answering a paying tenant,
            # and it is the direction an exception handler drifts because
            # allowing keeps the product working.
            logger.warning("model registry unreadable: %s", type(exc).__name__)
            return None, RoutingRefusal(
                reason=("The model registry could not be read, so IRA cannot "
                        "confirm which model is permitted to answer."),
                privacy_mode=mode, sensitivity=sensitivity,
                considered=((model_key, "the registry could not be read"),))
        return approved, None

    def _identity_refusal(self, result, approved, *, mode, sensitivity):
        """Did the provider run what it was told to run?

        The approval above decides; this verifies. Both are needed, because a
        provider that ignores `approved_model` and one that honours it are
        indistinguishable until it says what it did — and a provider that says
        nothing is treated as a failure rather than as "probably fine".
        """
        if approved is None:
            return None

        actual = getattr(result, "model_identity", "") or ""
        if actual == approved.identity:
            return None

        logger.warning(
            "provider ran %r, approved %r", actual, approved.identity)
        return RoutingRefusal(
            reason=("The model that answered was not the one IRA approved, so "
                    "its answer was discarded."),
            privacy_mode=mode, sensitivity=sensitivity,
            considered=((actual or "unreported",
                         f"approved {approved.identity}"),))

    def model_key_for(self, tier) -> str:
        """Which registry key answers this tier.

        A separate method so a deployment can override it, and so the mapping is
        observable in a test. It reads the tier, never the request body — the
        moment a caller can name a model key, it can name one whose rollout it
        prefers.
        """
        name = getattr(tier, "value", str(tier))
        return self.tier_model_keys.get(name, name)

    def _is_up(self, provider_id: str) -> bool:
        """Availability. Absent health check means assume reachable.

        Assuming reachable is safe here in a way it would not be elsewhere: a
        provider that is actually down fails at step 5 and is metered as an
        error, whereas assuming DOWN would make a working deployment with no
        health check refuse everything.
        """
        if self.health_check is None:
            return True
        try:
            return bool(self.health_check(provider_id))
        except Exception:  # noqa: BLE001
            return False

    def _cheapest(
        self, disclosures: Sequence[ProviderDisclosure], *, tier: Tier
    ) -> Optional[ProviderDisclosure]:
        """Cheapest provider that supports the tier.

        Local sorts first at equal cost, because local is free AND private and a
        tie should not be broken towards a third party.
        """
        supported = [
            d for d in disclosures
            if self.providers[d.provider_id].supports(tier)
        ]
        if not supported:
            return None

        def key(d: ProviderDisclosure):
            rates = self.costs.get(d.provider_id, {})
            total = Decimal(str(rates.get("prompt", 0))) + Decimal(
                str(rates.get("completion", 0))
            )
            return (total, 0 if d.locality is Locality.LOCAL else 1, d.provider_id)

        return sorted(supported, key=key)[0]

    async def _quota_for(self, principal) -> tuple[Optional[Quota], UsageSoFar]:
        if self.quota_reader is None or principal is None:
            return None, UsageSoFar()
        read = self.quota_reader(principal)
        if hasattr(read, "__await__"):
            read = await read
        quota, usage = read
        return quota, usage or UsageSoFar()

    async def _reserve(self, principal) -> bool:
        """Take one slot from today's ceiling. True when there is nothing to take.

        No reserver injected means the daily ceiling is advisory only — which is
        what every unit test that builds its own gateway gets, and what a
        deployment gets if it wires the gateway without a database. Production
        wires `core.models.store.reserve_daily_request`.
        """
        if self.quota_reserver is None or principal is None:
            return True
        taken = self.quota_reserver(principal)
        if hasattr(taken, "__await__"):
            taken = await taken
        return bool(taken)

    async def _release(self, principal, reserved: bool) -> None:
        """Hand a reserved slot back. Fail-soft.

        Called on every path that reserves and then refuses for a reason that is
        not the tenant spending a request — a provider being down, no provider
        supporting the tier, the call itself failing. Without it, a provider
        outage would quietly consume a tenant's daily allowance, and the tenant
        would be rate-limited by somebody else's fault.
        """
        if not reserved or self.quota_releaser is None or principal is None:
            return
        try:
            given = self.quota_releaser(principal)
            if hasattr(given, "__await__"):
                await given
        except Exception as exc:  # noqa: BLE001 - fail-soft, logged
            logger.warning("could not release a reserved request slot: %s", exc)

    async def _meter(
        self,
        principal,
        *,
        provider_id: str,
        locality: str,
        tier: Tier,
        sensitivity: str,
        mode: PrivacyMode,
        outcome: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal = Decimal("0"),
        model: str = "",
        latency_ms: Optional[int] = None,
    ) -> None:
        """Record the call. Fail-soft, and refusals are metered too.

        Fail-soft because a metering failure must not turn a completed answer
        into an error the user sees. Refusals are metered because a tenant that
        hit its ceiling ten thousand times, or whose privacy settings refused
        every request, is a fact worth having — and a usage table that only
        holds successes cannot show it.
        """
        if self.usage_writer is None or principal is None:
            return
        try:
            written = self.usage_writer(
                principal,
                {
                    "provider_id": provider_id or "ollama-local",
                    "locality": locality,
                    "tier": tier.value,
                    "sensitivity": sensitivity,
                    "privacy_mode": mode.value,
                    "outcome": outcome,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost": cost,
                    # Truncated to the column's bound rather than risking a
                    # constraint violation while recording a problem.
                    "model": (model or "")[:128],
                    "latency_ms": latency_ms,
                },
            )
            if hasattr(written, "__await__"):
                await written
        except Exception as exc:  # noqa: BLE001 - fail-soft, logged
            logger.warning("could not record model usage: %s", exc)


__all__ = [
    "IRAModelGateway",
    "Quota",
    "QuotaRefusal",
    "QuotaVerdict",
    "UsageSoFar",
    "check_quota",
    "estimate_cost",
]
