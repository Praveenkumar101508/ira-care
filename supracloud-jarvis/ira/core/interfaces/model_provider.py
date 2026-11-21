"""Model-provider interface.

Today `utils/llm.py` reaches for Ollama or vLLM directly and decides which by
inspecting `Settings.llm_backend` (and `dev_mode`). That is fine for one machine
with one inference stack; it does not survive an edition that routes per request
across hosted providers with different privacy postures.

This interface is the seam. Phase 8 builds the routing gateway behind it. Phase 1
only defines what a provider is and — importantly — what it must DISCLOSE, so
that a later Privacy Centre can tell a user the truth about where their words
went without retrofitting the information.

`utils/llm.py` is deliberately NOT rewritten in Phase 1: local model routing must
keep working exactly as it does, and swapping the live inference path is a
change that deserves its own phase.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Mapping, Optional, Protocol, Sequence, runtime_checkable


class Locality(str, Enum):
    """Where inference physically happens. Drives privacy claims, so it is not
    optional metadata — a provider that cannot state this cannot be routed to."""

    LOCAL = "local"            # same machine as IRA
    SELF_HOSTED = "self_hosted"  # operator's own infrastructure
    EXTERNAL = "external"      # a third party's infrastructure


class Tier(str, Enum):
    FAST = "fast"
    DEEP = "deep"
    REASONING = "reasoning"
    VISION = "vision"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class ProviderDisclosure:
    """What a user must be able to be told about a provider.

    Every field here exists because the Privacy Centre (Phase 5) or an Action
    Receipt (Phase 7) has to state it. If a value is not known, the honest
    answer is recorded — never an optimistic guess.
    """

    provider_id: str
    display_name: str
    locality: Locality
    #: Does the provider retain prompts? None = not known, which must be
    #: SHOWN as "not known" rather than rendered as "no".
    retains_prompts: Optional[bool]
    #: Data-processing region, e.g. "on-device", "eu-west-1". "" = not known.
    region: str = ""
    #: True when the provider is used to train models on submitted data.
    trains_on_data: Optional[bool] = None

    @property
    def leaves_the_machine(self) -> bool:
        return self.locality is not Locality.LOCAL

    @property
    def is_third_party(self) -> bool:
        return self.locality is Locality.EXTERNAL


@dataclass(frozen=True, slots=True)
class ApprovedModel:
    """Exactly which model a provider is permitted to run for one request.

    ── Why a NAME was not enough (P5-07) ───────────────────────────────────

    Phase 5 shipped a registry that decided which model versions may serve, and
    a gateway that checked `CompletionResult.model` — a name — AFTER the call.
    Two things were wrong with that, and both were recorded rather than hidden:

      * an unregistered model still ran. The prompt reached it; only the answer
        was discarded. "IRA does not use unapproved models" was not true, it was
        "IRA does not return their output".

      * a name cannot express a VERSION, so the rollout and the rollback in
        `067` could not influence which weights answered. `for_tenant` was
        implemented, tested, and had no consumer.

    This carries the identity the registry resolved, and the gateway puts it on
    the request BEFORE any provider is called. A provider that cannot honour it
    must raise rather than substitute something close.
    """

    model_key: str
    version: int
    #: The upstream weights, e.g. `Qwen/Qwen3-8B`.
    base_model: str
    #: Immutable. The whole point of MOD-02: a tag can move, a commit cannot.
    revision: str
    artifact_hash: str
    #: The licence identifier the publisher published for these exact weights —
    #: `Apache License`, `Gemma Terms of Use` — carried so that the thing which
    #: ran a request records what it ran under (Phase 6, MOD-01).
    #:
    #: **This is evidence, not permission.** Permission is the `known_approved`
    #: classification a named human recorded, which `require_approved_licence`
    #: has already enforced by the time this object exists. A consumer must not
    #: read this string and decide anything from it: the empty default is what a
    #: publisher declaring nothing looks like, and it must never be mistaken for
    #: "no restrictions".
    licence_identifier: str = ""

    @property
    def identity(self) -> str:
        """What a provider should echo back, and what the gateway compares."""
        return f"{self.model_key}@{self.version}:{self.revision}"


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    messages: Sequence[Mapping[str, Any]]
    tier: Tier = Tier.FAST
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    response_format: Optional[Mapping[str, Any]] = None
    #: How sensitive the content is. A gateway uses this to refuse to route
    #: sensitive content to an external provider.
    sensitivity: str = "normal"
    #: Set by the GATEWAY from the registry, never by a caller. A request that
    #: arrives with one already set is refused: it is the shape of a tenant
    #: choosing which model approves it.
    approved_model: Optional[ApprovedModel] = None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    text: str
    model: str
    provider_id: str
    #: The disclosure that applied to THIS call — captured at call time so a
    #: later configuration change cannot rewrite what a receipt says happened.
    disclosure: ProviderDisclosure
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: What the provider ACTUALLY ran, echoed from `ApprovedModel.identity`.
    #:
    #: Empty means the provider did not report it, which the gateway treats as a
    #: failure rather than as "probably fine" — a provider that ignores the
    #: approved model and one that forgets to say what it used are
    #: indistinguishable from the outside, and both must not serve a tenant.
    model_identity: str = ""


class ProviderTimeout(TimeoutError):
    """The provider had the request and did not answer within its read timeout.

    An exception rather than a refusal, for two reasons. The request reached
    the provider, so a refusal's "IRA did not send this anywhere" would be
    false. And a caller must be able to say "the model ran out of time"
    instead of presenting a sentence about a refusal as the model's answer.

    Never retried: a second attempt asks the model to write the whole answer
    again, behind the first one still running on a slow machine.
    `core/runtime/timeouts.py` lists model inference among the operations that
    are never retried automatically.
    """

    def __init__(self, provider_id: str, limit_s: Optional[float] = None) -> None:
        self.provider_id = provider_id
        self.limit_s = limit_s
        within = f" within {limit_s:g} s" if limit_s else " in time"
        super().__init__(f"{provider_id} did not answer{within}")


@runtime_checkable
class ModelProvider(Protocol):
    """One inference backend."""

    @property
    def provider_id(self) -> str:
        ...

    def disclosure(self) -> ProviderDisclosure:
        """What this provider does with data. Must never be optimistic."""
        ...

    def supports(self, tier: Tier) -> bool:
        ...

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run the completion.

        **The contract P5-07 adds.** When `request.approved_model` is set, the
        provider MUST run exactly that model at exactly that revision, and must
        set `CompletionResult.model_identity` to
        `request.approved_model.identity`.

        A provider that cannot — the weights are not present, the runtime pins a
        different revision, the backend does not expose revision selection —
        must RAISE. Substituting the closest thing it has is the failure this
        exists to prevent: the answer would be served, metered and shown to a
        tenant as though it came from the approved model.

        A provider whose backend took the request and did not answer within
        its read timeout raises `ProviderTimeout`, and does not send the
        request again.
        """
        ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        ...


@runtime_checkable
class ModelGateway(Protocol):
    """Chooses a provider per request and is the only thing that may call one.

    Phase 8 implements privacy-aware routing, cost ceilings, metering and
    fallback here. The contract that matters from Phase 1: routing must consider
    the principal's privacy policy and the content's sensitivity BEFORE
    availability or cost, and must refuse rather than downgrade privacy.
    """

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        ...

    def available_providers(self) -> Sequence[ProviderDisclosure]:
        ...


#: Disclosure for the current local Ollama path, stated honestly.
LOCAL_OLLAMA_DISCLOSURE = ProviderDisclosure(
    provider_id="ollama-local",
    display_name="Local Ollama",
    locality=Locality.LOCAL,
    retains_prompts=False,
    region="on-device",
    trains_on_data=False,
)


__all__ = [
    "Locality",
    "Tier",
    "ProviderDisclosure",
    "CompletionRequest",
    "CompletionResult",
    "ModelProvider",
    "ModelGateway",
    "LOCAL_OLLAMA_DISCLOSURE",
]
