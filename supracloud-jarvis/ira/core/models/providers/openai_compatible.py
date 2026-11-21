"""The OpenAI-compatible provider — Ollama, vLLM, and anything speaking `/v1`.

`utils/llm.py` has spoken this protocol since before the cloud work started.
What it never did was declare *where* it was speaking to, which is the one thing
a privacy-routing gateway cannot infer for itself. So this module's real subject
is not the HTTP call — that part is four lines — it is **locality**, and the
refusal to guess it.

── Locality is configured, never inferred ──────────────────────────────────

It would be easy, and wrong, to look at a base URL and decide: `localhost` means
`LOCAL`, anything else means `EXTERNAL`. That guess is wrong in both directions.
A `localhost` URL can be an SSH tunnel to a rented box; a LAN address can be the
operator's own hardware, which is `SELF_HOSTED` and not `LOCAL`; and a hostname
that resolves to a third party today can resolve somewhere else tomorrow.

`Locality` drives what IRA is allowed to tell a user about where their words
went. A wrong value there is not a routing bug, it is a false privacy claim. So
locality is a required constructor argument with no default, and
`from_settings()` derives it from the configured *backend*, which the operator
chose, rather than from a string it parsed.

The one inference that IS made is spelled out where it happens: an Ollama
backend pointed at a non-loopback host is not `LOCAL`. That direction is safe —
it can only make a claim weaker.

── Honouring the approved model ────────────────────────────────────────────

`ModelProvider.complete` says a provider given an `ApprovedModel` must run
exactly that model at exactly that revision, or **raise**:

    Substituting the closest thing it has is the failure this exists to
    prevent: the answer would be served, metered and shown to a tenant as
    though it came from the approved model.

An OpenAI-compatible endpoint takes a model NAME. It has no revision parameter,
so this provider cannot prove it ran a particular revision, and it does not
pretend to. What it can do is:

  * ask for the model the registry named, not the one configuration prefers;
  * refuse when the registry named something this backend cannot address; and
  * echo `model_identity` only when the endpoint confirms it ran that name.

The third is the one that matters. The response carries the model the server
actually used, and when that disagrees with what was asked for, this raises
rather than returning a result the gateway would happily meter. `_identity_
refusal` in the gateway is the second line of defence; this is the first.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

import httpx

from core.interfaces.model_provider import (
    ApprovedModel,
    CompletionRequest,
    CompletionResult,
    Locality,
    ProviderDisclosure,
    ProviderTimeout,
    Tier,
)
from core.models.served import note_served

logger = logging.getLogger("ira.models.providers.openai")


def is_read_timeout(exc: BaseException) -> bool:
    """True when the backend had the whole request and sent no answer in time.

    The OpenAI SDK raises one `APITimeoutError` for every httpx timeout, with
    the httpx exception as its cause. Only a READ timeout means the model had
    the request and was still working on it. A connect or pool timeout means
    the request never arrived, which is a failed call, not a slow model.
    """
    return isinstance(exc, httpx.ReadTimeout) or isinstance(exc.__cause__, httpx.ReadTimeout)


def read_limit_s(client: Any) -> Optional[float]:
    """The client's read timeout in seconds, when it states one."""
    timeout = getattr(client, "timeout", None)
    if isinstance(timeout, httpx.Timeout):
        return timeout.read
    if isinstance(timeout, (int, float)):
        return float(timeout)
    return None

#: Hosts that are genuinely the same machine. Anything else, whatever it is
#: called, is at best `SELF_HOSTED`.
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", ""})


# ── The output budget: prompt + answer must fit the server's context ────────
#
# Ollama serves a fixed context window (4,096 tokens unless the server was
# started with OLLAMA_CONTEXT_LENGTH) and its OpenAI-compatible route has no
# field that changes it per request. IRA asked for up to 4,096 (fast), 16,384
# (deep) or 32,768 (reasoning) output tokens regardless. When prompt + output
# reached the window, llama-server shifted the context: it kept the first four
# tokens and discarded half of the rest, which is the system prompt, the
# owner's instructions and the question, and went on generating without them.
# Nothing reported it. Traced on 1 November 2026: a greeting (935 prompt
# tokens) reasoned past 3,161 generated tokens and `n_discard = 2045`.
#
# So each request asks for at most what fits. The prompt is not tokenised here
# (the provider has no tokenizer for the served model), so its size is an
# upper estimate, and the error is in the safe direction: a smaller budget.

#: Ollama's own default context below 24 GiB of VRAM, which covers every
#: CPU-only install and an 8 GB Mac. Assumed when the setting is 0 or unset.
DEFAULT_OLLAMA_CONTEXT = 4096

#: The least IRA asks a model for, even when the prompt estimate leaves less.
#: Below this a reasoning model cannot get to an answer at all, and asking for
#: nothing would fail every request a slightly generous estimate overstates.
MIN_OUTPUT_TOKENS = 256

#: Chat-template tokens around each message (`<|im_start|>role\n…<|im_end|>\n`)
#: and around the whole prompt (the assistant header and a reasoning model's
#: opening tag), counted generously.
_TEMPLATE_TOKENS_PER_MESSAGE = 8
_TEMPLATE_TOKENS = 16

#: What the user is told when the model used its whole budget before writing
#: anything they can see, and the note added to an answer cut off at the
#: budget. A reasoning model spends its tokens on hidden reasoning first, so
#: the first case is the common one, and an empty reply would look like IRA had
#: nothing to say.
OUT_OF_BUDGET_MESSAGE = (
    "I could not answer this time: the model ran out of its answer "
    "budget{budget} while it was still reasoning, before it wrote any answer. "
    "Please try again, perhaps with a shorter or simpler question.")
CUT_OFF_NOTICE = (
    "\n\n[This answer is incomplete: the model ran out of its answer "
    "budget{budget} before it finished.]")


def _content_text(content: Any) -> str:
    """A message's text. OpenAI-style content may be a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content
                       if isinstance(p, dict) and p.get("type", "text") == "text")
    return "" if content is None else str(content)


def estimate_prompt_tokens(messages: Any) -> int:
    """An upper estimate of a chat prompt's size in tokens, without a tokenizer.

    Calibrated against the server's own counts on 1 November 2026: English
    prompts ran at 4.4 to 4.6 characters per token, chat template included.
    This counts an ASCII character as a third of a token, a digit as a whole
    one (Qwen's tokenizer splits numbers into single digits), and other text
    at a token per two UTF-8 bytes, plus the template overhead.
    """
    total = _TEMPLATE_TOKENS
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else message
        text = _content_text(content)
        digits = sum(1 for ch in text if "0" <= ch <= "9")
        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        other_bytes = len(text.encode("utf-8", "replace")) - ascii_chars
        total += (_TEMPLATE_TOKENS_PER_MESSAGE + digits
                  + -(-(ascii_chars - digits) // 3) + -(-other_bytes // 2))
    return total


def ollama_context_tokens(cfg: Any) -> int:
    """The context window of the configured Ollama server, in tokens.

    `OLLAMA_CONTEXT_LENGTH` is read under Ollama's own name so that one line
    in `.env` sets both the server (start-ira.sh exports `.env` to the
    `ollama serve` it starts) and IRA's budget. 0, unset or unreadable means
    Ollama's default, of which 4,096 is the smallest.
    """
    try:
        value = int(getattr(cfg, "ollama_context_length", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else DEFAULT_OLLAMA_CONTEXT


def fit_output_budget(messages: Any, requested: Optional[int],
                      context_tokens: int) -> int:
    """The most output tokens to ask for: `requested`, lowered to what fits.

    Never raises the caller's own limit. When the prompt estimate leaves less
    than `MIN_OUTPUT_TOKENS`, that floor is asked for and a warning logged:
    the prompt itself is close to the whole window.
    """
    room = context_tokens - estimate_prompt_tokens(messages)
    if room < MIN_OUTPUT_TOKENS:
        logger.warning(
            "the prompt (estimated) leaves %d of %d context tokens for the "
            "answer; asking for %d", room, context_tokens, MIN_OUTPUT_TOKENS)
        room = MIN_OUTPUT_TOKENS
    if requested is None or requested <= 0:
        return room
    return min(requested, room)


def budget_notice(finish_reason: Optional[str], answered: bool,
                  limit: Optional[int]) -> Optional[str]:
    """What to tell the user when the model stopped at its token limit.

    `None` when it stopped for any other reason. `answered` is whether any
    visible text came back: without it the whole budget went on reasoning.
    """
    if finish_reason != "length":
        return None
    template = CUT_OFF_NOTICE if answered else OUT_OF_BUDGET_MESSAGE
    return template.format(budget=f" of {limit:,} tokens" if limit else "")


class ProviderMisconfigured(RuntimeError):
    """The provider cannot state something it is required to state.

    Raised at construction, never at call time. A provider that discovers
    halfway through a request that it does not know its own locality has
    already accepted the prompt.
    """


class UnsupportedApprovedModel(RuntimeError):
    """The registry named a model this backend cannot address.

    Deliberately not a refusal object: a refusal is a routing outcome the user
    is shown, and this is a deployment error. The gateway catches it, meters the
    attempt as an error and refuses — which is the behaviour the contract asks
    for, without this module having to know what a refusal looks like.
    """


class OpenAICompatibleProvider:
    """One `/v1`-speaking backend, declared rather than guessed.

    `model_for_tier` maps a tier to the model name this backend should be asked
    for when the registry has NOT named one. It is consulted only in that case:
    when `registry_required` is on, the registry's answer wins and this map is
    not read at all.
    """

    __slots__ = ("_provider_id", "_display_name", "_locality", "_base_url",
                 "_api_key", "_timeout", "_model_for_tier", "_retains_prompts",
                 "_region", "_trains_on_data", "_client_factory",
                 "_context_tokens")

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        locality: Locality,
        base_url: str,
        api_key: str,
        model_for_tier: dict[Tier, str],
        retains_prompts: Optional[bool],
        region: str = "",
        trains_on_data: Optional[bool] = None,
        timeout: Any = None,
        client_factory: Any = None,
        context_tokens: Optional[int] = None,
    ) -> None:
        """`context_tokens` is the server's context window when the server
        does NOT enforce it itself. Ollama shifts the context silently, so it
        is set for Ollama; vLLM refuses an oversized request, so it is not."""
        if not provider_id:
            raise ProviderMisconfigured("a provider must have an id")
        if not isinstance(locality, Locality):
            raise ProviderMisconfigured(
                f"{provider_id}: locality must be a Locality, got "
                f"{type(locality).__name__}. Where inference happens drives what "
                f"IRA may tell a user about their data; it is not optional "
                f"metadata and it is not inferred from a URL.")
        if not base_url:
            raise ProviderMisconfigured(f"{provider_id}: no base_url")
        self._provider_id = provider_id
        self._display_name = display_name or provider_id
        self._locality = locality
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._model_for_tier = dict(model_for_tier)
        self._retains_prompts = retains_prompts
        self._region = region
        self._trains_on_data = trains_on_data
        self._client_factory = client_factory
        self._context_tokens = context_tokens

    # ── Identity and disclosure ─────────────────────────────────────────────

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def disclosure(self) -> ProviderDisclosure:
        """What this provider does with data. Never optimistic.

        `retains_prompts=None` travels through as `None`. A local backend that
        genuinely writes nothing to disk may say `False`; a backend nobody has
        checked says `None`, and the surface renders that as "not known". The
        one thing this must never do is turn an unchecked provider into a clean
        one, which is why there is no `or False` anywhere in this method.
        """
        return ProviderDisclosure(
            provider_id=self._provider_id,
            display_name=self._display_name,
            locality=self._locality,
            retains_prompts=self._retains_prompts,
            region=self._region,
            trains_on_data=self._trains_on_data,
        )

    def supports(self, tier: Tier) -> bool:
        return tier in self._model_for_tier

    # ── The call ────────────────────────────────────────────────────────────

    def _model_name(self, request: CompletionRequest) -> tuple[str, Optional[ApprovedModel]]:
        """Which model to ask for, and the approval it came from.

        When the registry named one it wins outright. `base_model` is the
        upstream identity — `Qwen/Qwen3-8B` — and `model_key` is what a
        deployment registered; an Ollama tag is neither, so the resolution order
        is: the base model if this backend knows it, else the model key. If the
        backend can address neither, this raises rather than falling back to
        configuration, because falling back is exactly the substitution the
        contract forbids.
        """
        approved = request.approved_model
        if approved is None:
            model = self._model_for_tier.get(request.tier)
            if not model:
                raise UnsupportedApprovedModel(
                    f"{self._provider_id} has no model configured for the "
                    f"{request.tier.value} tier")
            return model, None

        for candidate in (approved.base_model, approved.model_key):
            if candidate:
                return candidate, approved

        raise UnsupportedApprovedModel(
            f"{self._provider_id}: the registry approved "
            f"{approved.identity!r} but named no model this backend can "
            f"address. Asking for the configured model instead would serve a "
            f"tenant weights nobody approved.")

    def _client_for(self, tier: Tier) -> Any:
        """The `/v1` client for a tier.

        Tier-aware for two reasons, and the second is the one that decided it.

        A vLLM deployment genuinely has a different endpoint per tier —
        `VLLM_FAST_URL`, `VLLM_DEEP_URL`, `VLLM_REASONING_URL` — so one client
        per provider would have to pick one and be wrong for the others.

        And `utils/llm.py` has encoded exactly that mapping since long before
        the cloud work, in `get_fast_client` / `get_deep_client` /
        `get_reasoning_client`. Making the factory tier-aware lets the
        composition root delegate to those rather than reimplement them, which
        means Phase 8 moves the *route* without moving the client construction —
        and every existing test that patches one of those three functions keeps
        testing the thing it was written to test.

        `base_url` therefore describes this provider rather than dialling it. It
        is what `disclosure()` and locality derivation read.
        ── Not cached, and that was a bug for about an hour ────────────────

        The first version cached one client per tier. `utils/llm.py`'s
        factories build a fresh `AsyncOpenAI` on every call and always have, so
        caching here changed behaviour in two ways that both matter: a settings
        reload could not reach the client, and a test that patched
        `get_fast_client` after the first request was silently ignored.

        The second is how it was found — `test_vision_complete_failsoft_on_error`
        got the *previous* test's mock and returned its answer. That is exactly
        the failure mode a cache in front of a patchable seam produces, and it
        would have been indistinguishable from a flaky test.
        """
        if self._client_factory is None:
            raise ProviderMisconfigured(f"{self._provider_id}: no client factory")
        client = self._client_factory(tier, self._base_url, self._api_key,
                                      self._timeout)
        if client is None:
            raise ProviderMisconfigured(
                f"{self._provider_id}: no client for the {tier.value} tier")
        return client

    @staticmethod
    def _identity(served: str, asked: str, approved: Optional[ApprovedModel],
                  provider_id: str) -> str:
        """The identity to report, or a raise.

        An OpenAI-compatible response echoes the model it used. When the
        registry approved something and the server answers with a different
        name, the honest outcomes are "raise" or "return an empty identity" —
        and the gateway treats an empty identity as a failure anyway. Raising is
        better: it happens at the provider, names the provider, and produces one
        error rather than a refusal a reader has to trace back.
        """
        if approved is None:
            return ""
        if served and served != asked:
            raise UnsupportedApprovedModel(
                f"{provider_id}: asked for {asked!r} on the registry's "
                f"authority and the backend answered as {served!r}. A backend "
                f"that substitutes a model must not have its answer served.")
        return approved.identity

    def _max_tokens(self, request: CompletionRequest) -> Optional[int]:
        """The output limit to send: the caller's, lowered to what fits.

        Only when this provider knows a context window it must keep to, and
        not for images, whose token cost this estimate cannot see.
        """
        if self._context_tokens is None or request.tier is Tier.VISION:
            return request.max_tokens
        return fit_output_budget(request.messages, request.max_tokens,
                                 self._context_tokens)

    def _log_budget_spent(self, request: CompletionRequest,
                          limit: Optional[int], answered: bool,
                          completion_tokens: Optional[int] = None) -> None:
        logger.warning(
            "%s: the model stopped at its output limit (%s tokens, %s tier, "
            "context %s, prompt estimate %d, completion tokens %s) %s",
            self._provider_id, limit, request.tier.value, self._context_tokens,
            estimate_prompt_tokens(request.messages),
            completion_tokens if completion_tokens is not None else "not reported",
            "and the answer was cut off" if answered
            else "before writing any answer; the user is told so")

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        model, approved = self._model_name(request)
        client = self._client_for(request.tier)
        max_tokens = self._max_tokens(request)

        extra: dict[str, Any] = {}
        if request.response_format is not None:
            extra["response_format"] = dict(request.response_format)

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=list(request.messages),
                max_tokens=max_tokens,
                temperature=request.temperature,
                stream=False,
                **extra,
            )
        except Exception as exc:
            if is_read_timeout(exc):
                raise ProviderTimeout(self._provider_id, read_limit_s(client)) from exc
            raise

        choice = response.choices[0] if response.choices else None
        text = (getattr(getattr(choice, "message", None), "content", "") or "")
        served = getattr(response, "model", "") or ""
        usage = getattr(response, "usage", None)

        answered = bool(text.strip())
        notice = budget_notice(getattr(choice, "finish_reason", None), answered,
                               max_tokens)
        if notice is not None:
            self._log_budget_spent(request, max_tokens, answered,
                                   getattr(usage, "completion_tokens", None))
            text = text + notice if answered else notice

        return CompletionResult(
            text=text,
            # What the SERVER said it ran, when it said anything. Reporting the
            # name we asked for would make a substituting backend invisible in
            # the usage table as well as in the result.
            model=served or model,
            provider_id=self._provider_id,
            disclosure=self.disclosure(),
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model_identity=self._identity(served, model, approved,
                                          self._provider_id),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield token deltas.

        No retry, for the reason `utils/llm.py` has always given: a mid-stream
        failure cannot be replayed without sending the caller duplicate tokens.

        The approved-model check happens before the first token, because after
        it there is nothing useful to do with the answer — it is already on its
        way to a user.

        The first chunk also names the model that is answering, which the chat
        readout reports (finding P1-05). `complete` reports the same name in
        `CompletionResult.model`: the server's, or the one asked for when the
        server names none.

        A stream that ends at the output limit ends with one more delta saying
        so (`budget_notice`): the whole message when nothing visible came
        back, a note after an answer that was cut off.
        """
        model, approved = self._model_name(request)
        client = self._client_for(request.tier)
        max_tokens = self._max_tokens(request)

        stream = await client.chat.completions.create(
            model=model,
            messages=list(request.messages),
            max_tokens=max_tokens,
            temperature=request.temperature,
            stream=True,
        )
        checked = False
        answered = False
        finish_reason: Optional[str] = None
        async for chunk in stream:
            if not checked:
                served = getattr(chunk, "model", "") or ""
                self._identity(served, model, approved, self._provider_id)
                note_served(served or model, request.tier.value)
                checked = True
            choices = getattr(chunk, "choices", None) or ()
            if not choices:
                continue
            finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
            delta = getattr(getattr(choices[0], "delta", None), "content", None)
            if delta:
                answered = answered or bool(delta.strip())
                yield delta
        notice = budget_notice(finish_reason, answered, max_tokens)
        if notice is not None:
            self._log_budget_spent(request, max_tokens, answered)
            yield notice

    # ── Construction from settings ──────────────────────────────────────────

    @staticmethod
    def _ollama_locality(base_url: str) -> Locality:
        """`LOCAL` only when the endpoint really is this machine.

        The one inference in this module, and it only ever weakens a claim: an
        Ollama pointed at another host is the operator's own infrastructure, not
        the user's device, and telling a user their words never left the machine
        would be false.
        """
        host = (urlparse(base_url).hostname or "").lower()
        return Locality.LOCAL if host in _LOOPBACK else Locality.SELF_HOSTED

    @classmethod
    def from_settings(cls, cfg: Any, *, client_factory: Any,
                      timeout: Any = None) -> "OpenAICompatibleProvider":
        """The provider `utils/llm.py`'s configuration describes.

        This deliberately reproduces the existing backend choice rather than
        improving it: Phase 8's job is to put the gateway *underneath* the live
        path, and a routing change smuggled in at the same time would make any
        behavioural difference impossible to attribute.
        """
        backend = str(getattr(cfg, "llm_backend", "ollama") or "ollama").strip().lower()
        ollama = backend == "ollama" or bool(getattr(cfg, "dev_mode", False))

        if ollama:
            base_url = getattr(cfg, "ollama_base_url", "") or ""
            locality = cls._ollama_locality(base_url)
            tiers = {
                Tier.FAST: _resolve_tier_model(cfg, "fast"),
                Tier.DEEP: _resolve_tier_model(cfg, "deep"),
                Tier.REASONING: _resolve_tier_model(cfg, "reasoning"),
            }
            vision = _resolve_vision_model(cfg)
            if vision:
                tiers[Tier.VISION] = vision
            return cls(
                provider_id="ollama-local",
                display_name="Local Ollama",
                locality=locality,
                base_url=base_url,
                api_key="ollama",
                model_for_tier={t: m for t, m in tiers.items() if m},
                # A local Ollama keeps no prompt log of its own. Stated as a
                # fact about the software, and only when it is genuinely on this
                # machine — a remote one is somebody's server and unchecked.
                retains_prompts=False if locality is Locality.LOCAL else None,
                region="on-device" if locality is Locality.LOCAL else "",
                trains_on_data=False if locality is Locality.LOCAL else None,
                timeout=timeout,
                client_factory=client_factory,
                # Ollama shifts its context silently instead of refusing.
                context_tokens=ollama_context_tokens(cfg),
            )

        reasoning_url = getattr(cfg, "vllm_reasoning_url", "") or ""
        tiers = {
            Tier.FAST: getattr(cfg, "vllm_fast_model", "") or "",
            Tier.DEEP: getattr(cfg, "vllm_deep_model", "") or "",
            Tier.REASONING: (getattr(cfg, "vllm_reasoning_model", "") or ""
                             if reasoning_url
                             else getattr(cfg, "vllm_deep_model", "") or ""),
        }
        # Vision is a separate endpoint on vLLM, and the tier-aware client
        # factory dials it. Registered only when one is configured, so
        # `supports(VISION)` stays an honest answer rather than a hope.
        if getattr(cfg, "vllm_vision_url", ""):
            tiers[Tier.VISION] = getattr(cfg, "vllm_vision_model", "") or "vision"

        return cls(
            provider_id="vllm",
            display_name="vLLM",
            locality=Locality.SELF_HOSTED,
            base_url=getattr(cfg, "vllm_fast_url", "") or "",
            api_key=getattr(cfg, "vllm_api_key", "") or "",
            model_for_tier={t: m for t, m in tiers.items() if m},
            # Nobody has checked what this operator's vLLM logs. `None` says
            # exactly that, and the Privacy Centre renders it as "not known".
            retains_prompts=None,
            region="",
            trains_on_data=None,
            timeout=timeout,
            client_factory=client_factory,
        )


def _resolve_tier_model(cfg: Any, tier: str) -> str:
    """The Ollama model for a tier, via the existing selection layer.

    Imported lazily and defensively for the reason `utils/llm.py` records: model
    selection must never break the chat flow. The fallback is the legacy config
    value, which is what the direct path used before Phase 8.
    """
    legacy = {
        "fast": getattr(cfg, "ollama_model_fast", "") or "",
        "deep": getattr(cfg, "ollama_model_deep", "") or "",
        "reasoning": getattr(cfg, "ollama_model_reasoning", "") or "",
    }[tier]
    if not getattr(cfg, "ira_use_model_router", True):
        return legacy
    try:
        from utils.llm import resolve_ollama_model
        return resolve_ollama_model(tier) or legacy
    except Exception:  # noqa: BLE001 — selection must never break the chat flow
        return legacy


def _resolve_vision_model(cfg: Any) -> str:
    try:
        from utils.llm import resolve_ollama_vision_model
        return resolve_ollama_vision_model() or ""
    except Exception:  # noqa: BLE001
        return getattr(cfg, "ollama_vision_model", "") or ""


__all__ = [
    "OpenAICompatibleProvider",
    "ProviderMisconfigured",
    "UnsupportedApprovedModel",
    "budget_notice",
    "estimate_prompt_tokens",
    "fit_output_budget",
    "ollama_context_tokens",
]
