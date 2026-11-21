"""
vLLM / Ollama client with 3-tier intelligent routing (2026 model stack).

Production model tiers:
  Fast      — Qwen3-8B           (<2s TTFT, conversational/quick lookups)
  Deep      — Qwen3-14B          (code gen, security, complex reasoning)
  Reasoning — Qwen3-32B / R1-32B (Think Mode, DeepSearch, long chains)

Dev mode: all tiers → local Ollama (qwen3:8b recommended)

Cloud upgrade path (8×H100):
  Fast      → Qwen3-30B-A3B (MoE — set VLLM_FAST_URL + FAST_MODEL)
  Deep      → Qwen3-72B     (set VLLM_DEEP_URL + DEEP_MODEL)
  Reasoning → DeepSeek-R1 671B or Qwen3-235B-A22B (VLLM_REASONING_URL)
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

import logging

from config import get_settings
from utils.yaml_config import get_fast_keywords, get_deep_keywords, get_reasoning_keywords

_log = logging.getLogger("ira.utils.llm")

# ── Agents that always use the deep path ────────────────────────────────────
_DEEP_AGENTS = {"security", "creator", "researcher"}


def should_use_deep(query: str, agent: str | None = None) -> bool:
    """Return True if this query should route to the deep (Qwen3-14B) model."""
    if agent in _DEEP_AGENTS:
        return True
    q = query.lower()
    if any(kw in q for kw in get_fast_keywords()):
        return False
    if len(query.split()) > 60:
        return True
    return any(kw in q for kw in get_deep_keywords())


def should_use_reasoning(
    query: str,
    *,
    think_mode: bool = False,
    deep_search: bool = False,
) -> bool:
    """
    Return True if this query should route to the reasoning path (Qwen3-32B / R1).
    Triggered by: Think Mode toggle, DeepSearch, or explicit reasoning keywords.
    """
    if think_mode or deep_search:
        return True
    q = query.lower()
    return any(kw in q for kw in get_reasoning_keywords())


# ── Client factories ──────────────────────────────────────────────────────────

_LLM_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)

#: Every generation request is sent once. The OpenAI SDK's default of two
#: retries resends a request whose answer did not arrive within the read
#: timeout, and the model then writes the whole answer again, queued behind the
#: first attempt. On a CPU-only machine one question ran for up to three read
#: timeouts (30 minutes at 600 s) after the browser's proxy had already given
#: up at 15 (finding P1-02, 31 October 2026). `core/runtime/timeouts.py` lists
#: model inference among the operations never retried automatically.
#:
#: Connection failures are not retried either. The SDK raises the same way for
#: a refused connection and for a read that timed out after the model had the
#: request, and httpx's connect-only retry needs a custom transport, which
#: would stop the client honouring the proxy settings in the environment. A
#: refused connection to the model is reported at once.
GENERATION_MAX_RETRIES = 0


def _make_vllm_client(base_url: str) -> AsyncOpenAI:
    cfg = get_settings()
    return AsyncOpenAI(api_key=cfg.vllm_api_key, base_url=base_url, timeout=_LLM_TIMEOUT,
                       max_retries=GENERATION_MAX_RETRIES)


def _make_ollama_client() -> AsyncOpenAI:
    """Local Ollama client — points at the OpenAI-compatible API on localhost."""
    cfg = get_settings()
    # The read timeout follows the machine: a CPU-only install sets a longer one.
    timeout = httpx.Timeout(connect=5.0, read=cfg.ollama_read_timeout_s, write=30.0, pool=5.0)
    return AsyncOpenAI(api_key="ollama", base_url=cfg.ollama_base_url, timeout=timeout,
                       max_retries=GENERATION_MAX_RETRIES)


def _use_ollama() -> bool:
    """L2: True when the local Ollama backend is active.

    Driven by the LLM_BACKEND switch (independent of auth/biometrics) OR the
    legacy dev_mode flag. Keeping dev_mode here preserves backwards compatibility
    without relying on it — set llm_backend="ollama" to run locally with auth ON.
    """
    cfg = get_settings()
    return cfg.llm_backend == "ollama" or cfg.dev_mode


# ── Model-selection wiring (ira/reasoning/model_router) ─────────────────────────
# When IRA_USE_MODEL_ROUTER is on (default), the Ollama model for each tier is
# resolved through the smart model-selection layer (profiles + availability +
# local fallback) instead of the static cfg.ollama_model_* names. Fail-safe: any
# error, or a model that isn't installed, falls back to the legacy config value,
# so the existing chat flow is never broken. Only the Ollama path is affected;
# the vLLM path is untouched.

_TIER_TO_MODE = {"fast": "LOCAL_FAST", "deep": "LOCAL_MAIN", "reasoning": "LOCAL_REASONING"}


def _legacy_ollama_model(cfg, tier: str) -> str:
    return {
        "fast": cfg.ollama_model_fast,
        "deep": cfg.ollama_model_deep,
        "reasoning": cfg.ollama_model_reasoning,
    }[tier]


def resolve_ollama_model(tier: str) -> str:
    """Resolve the Ollama model for a tier ("fast" | "deep" | "reasoning").

    Routes through the model-selection layer when ``ira_use_model_router`` is on,
    otherwise returns the legacy ``cfg.ollama_model_*`` value. Never raises.
    """
    cfg = get_settings()
    legacy = _legacy_ollama_model(cfg, tier)
    if not getattr(cfg, "ira_use_model_router", True):
        return legacy
    try:
        from reasoning.model_profiles import ModelMode
        from reasoning.model_router import resolve_model

        mode = getattr(ModelMode, _TIER_TO_MODE[tier])
        _used_mode, model, _fallback = resolve_model(mode)
        if model and legacy and model != legacy:
            from reasoning.model_availability import get_availability, is_available

            # When nothing in the profile's chain is installed, the router lands
            # on its terminal tiny model anyway (gemma3n:e4b) and Ollama answers
            # "model not found". If the configured OLLAMA_MODEL_* IS installed —
            # a machine set up with a smaller model — use it, as the vision
            # resolver below already does. Only on a positive probe.
            avail = get_availability()
            if (avail.reachable and not is_available(model, availability=avail)
                    and is_available(legacy, availability=avail)):
                return legacy
        return model or legacy
    except Exception:  # noqa: BLE001 — model selection must never break the chat flow
        return legacy


def resolve_ollama_vision_model() -> str:
    """Resolve the Ollama vision model, honouring the profile / IRA_LOCAL_VISION_MODEL.

    Vision must never fall back to a text model, so this does NOT use the chat
    fallback chain. If the preferred vision model isn't installed but the legacy
    ``ollama_vision_model`` is, the legacy one is kept (don't break a working box).
    Never raises.
    """
    cfg = get_settings()
    legacy = getattr(cfg, "ollama_vision_model", "") or ""
    if not getattr(cfg, "ira_use_model_router", True):
        return legacy
    try:
        from reasoning.model_profiles import ModelMode, model_for
        from reasoning.model_availability import get_availability, is_available

        preferred = model_for(ModelMode.LOCAL_VISION)
        avail = get_availability()
        if not avail.reachable:
            # Can't probe: keep a working legacy model if one is configured.
            return legacy or preferred
        if is_available(preferred, availability=avail):
            return preferred
        if legacy and is_available(legacy, availability=avail):
            return legacy
        return preferred  # neither installed → let vision_complete fail soft
    except Exception:  # noqa: BLE001
        return legacy


def get_fast_client() -> AsyncOpenAI:
    cfg = get_settings()
    return _make_ollama_client() if _use_ollama() else _make_vllm_client(cfg.vllm_fast_url)


def get_deep_client() -> AsyncOpenAI:
    cfg = get_settings()
    return _make_ollama_client() if _use_ollama() else _make_vllm_client(cfg.vllm_deep_url)


def get_reasoning_client() -> AsyncOpenAI:
    """
    Returns the reasoning-tier client.
    Falls back to the deep client if no dedicated reasoning endpoint is configured.
    """
    cfg = get_settings()
    if _use_ollama():  # L2
        return _make_ollama_client()
    if cfg.vllm_reasoning_url:
        return _make_vllm_client(cfg.vllm_reasoning_url)
    # Graceful fallback — use deep path (still much better than fast for reasoning)
    return _make_vllm_client(cfg.vllm_deep_url)


# ── Core completion function ──────────────────────────────────────────────────

async def _complete_no_stream(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    response_format: dict | None = None,
) -> str:
    """
    Non-streaming completion, sent once — the direct path, used only when no
    gateway could be built.

    It used to be retried up to three times, on any error. A read timeout is an
    error, and each retry asked the model to write the whole answer again; see
    `GENERATION_MAX_RETRIES`. A read timeout raises `ProviderTimeout`, as the
    gateway path does, so a caller sees the same thing on both paths.

    ``response_format`` is the OpenAI-compatible structured-output hint (e.g.
    ``{"type": "json_object"}``). Both backends honour it on the /v1 surface
    (Ollama maps it to its native ``format``; vLLM to guided decoding), so the
    single client path stays backend-agnostic. Passed through only when set.
    """
    from core.interfaces.model_provider import ProviderTimeout
    from core.models.providers.openai_compatible import (
        budget_notice,
        is_read_timeout,
        read_limit_s,
    )

    max_tokens = _direct_budget(messages, max_tokens)
    extra: dict = {}
    if response_format is not None:
        extra["response_format"] = response_format
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
            **extra,
        )
    except Exception as exc:
        if is_read_timeout(exc):
            raise ProviderTimeout("direct", read_limit_s(client)) from exc
        raise
    choice = response.choices[0]
    text = choice.message.content or ""
    notice = budget_notice(getattr(choice, "finish_reason", None), bool(text.strip()),
                           max_tokens)
    if notice is None:
        return text
    _log.warning("direct: the model stopped at its output limit (%s tokens)", max_tokens)
    return text + notice if text.strip() else notice


def _direct_budget(messages: list[dict], max_tokens: int) -> int:
    """The direct path keeps to Ollama's context window as the provider does.

    See `fit_output_budget`: past the window Ollama discards the start of the
    prompt and keeps generating. vLLM refuses such a request itself.
    """
    if not _use_ollama():
        return max_tokens
    from core.models.providers.openai_compatible import (
        fit_output_budget,
        ollama_context_tokens,
    )
    return fit_output_budget(messages, max_tokens, ollama_context_tokens(get_settings()))


# ── The gateway path ─────────────────────────────────────────────────────────
#
# Phase 8. `IRAModelGateway` runs policy → quota → availability → cost → call →
# meter, in that order, and each step can only narrow. What lives here is the
# translation between this module's long-standing signature and the gateway's
# request/refusal vocabulary — deliberately nothing else, so that the routing
# rules stay in one place and this file cannot develop a second opinion.

_TIER = {"fast": "FAST", "deep": "DEEP", "reasoning": "REASONING"}


def _gateway_and_request(messages, *, tier, max_tokens, temperature,
                         response_format=None, sensitivity="normal"):
    """`(gateway, request)`, or `(None, None)` if the gateway cannot be built.

    Never raises. A failure to import or construct the gateway must not take
    the chat path down with it — but it must also not be invisible, so it is
    logged and the caller's fallback is asserted by a test rather than trusted.
    """
    try:
        from core.interfaces.model_provider import CompletionRequest, Tier
        from core.models.composition import gateway as _gateway

        gw = _gateway()
        if not gw.providers:
            return None, None
        return gw, CompletionRequest(
            messages=messages,
            tier=getattr(Tier, _TIER[tier]),
            temperature=temperature if temperature is not None else 0.7,
            max_tokens=max_tokens,
            response_format=response_format,
            sensitivity=sensitivity,
        )
    except Exception as exc:  # noqa: BLE001 — logged, and the caller falls back
        _log.warning("model gateway unavailable, calling the provider "
                     "directly: %s", exc)
        return None, None


async def _principal_or_none():
    """The caller's principal in the cloud edition, `None` in the private one.

    A private install has one user and no tenant, so there is nothing to meter
    against and `None` is the honest value. Resolution failures are `None` too:
    quota and metering are then skipped, and the policy still runs.
    """
    try:
        from core.editions import EDITION_CLOUD, current_edition
        if current_edition() != EDITION_CLOUD:
            return None
        from core.runtime.request_context import get_principal
        return get_principal()
    except Exception:  # noqa: BLE001
        return None


def _privacy_mode() -> str:
    return str(getattr(get_settings(), "ira_privacy_mode", "local_first"))


async def _complete_via_gateway(messages, *, tier, max_tokens, temperature,
                                response_format, client, model) -> str:
    gw, request = _gateway_and_request(
        messages, tier=tier, max_tokens=max_tokens, temperature=temperature,
        response_format=response_format)
    if gw is None:
        return await _complete_no_stream(
            client, model, messages, max_tokens, temperature, response_format)

    outcome = await gw.complete(request, principal=await _principal_or_none(),
                                privacy_mode=_privacy_mode())
    if getattr(outcome, "ok", True) is False:
        # A refusal is an ordinary outcome a user must be shown, not an error —
        # `RoutingRefusal`'s own docstring says so. Returning its reason keeps
        # every existing caller working: they all expect a string, and the
        # string they get says what was refused and why.
        return outcome.reason
    # The model the provider says ran, for the chat readout (finding P1-05).
    # A refusal above is not recorded: no model wrote it.
    from core.models.served import note_served
    note_served(outcome.model, tier)
    return outcome.text


async def _stream_via_gateway(messages, *, tier, max_tokens, temperature,
                              client, model):
    """An async iterator of OpenAI-shaped chunks, so `stream_tokens` is unchanged.

    The gateway decides BEFORE the first token — a stream opened and then
    refused has already sent the prompt to a provider, which is the failure
    P5-07 removed from the non-streaming path.
    """
    gw, request = _gateway_and_request(
        messages, tier=tier, max_tokens=max_tokens, temperature=temperature)
    if gw is None:
        return await client.chat.completions.create(
            model=model, messages=messages,
            max_tokens=_direct_budget(messages, max_tokens),
            temperature=temperature, stream=True)

    return await gw.stream(request, principal=await _principal_or_none(),
                           privacy_mode=_privacy_mode())


async def chat_complete(
    messages: list[dict],
    *,
    use_deep: bool = False,
    use_reasoning: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    stream: bool = False,
    response_format: dict | None = None,
) -> str | AsyncIterator:
    """
    Send messages to the appropriate LLM endpoint (fast / deep / reasoning).
    Returns the full response string (stream=False) or an async iterator (stream=True).

    ``response_format`` (non-streaming only) requests structured output, e.g.
    ``{"type": "json_object"}`` — used by the Reflexion critic to get a parseable
    Critique. Ignored for streaming calls (token streams can't be schema-bound).

    Tier selection (first match wins):
      use_reasoning=True → reasoning tier (Qwen3-32B or DeepSeek-R1)
      use_deep=True      → deep tier      (Qwen3-14B)
      default            → fast tier      (Qwen3-8B)

    No call is retried, streaming or not: see `GENERATION_MAX_RETRIES`. A
    non-streaming call whose model does not answer within the read timeout
    raises `core.interfaces.model_provider.ProviderTimeout`.
    """
    cfg = get_settings()
    ollama = _use_ollama()  # L2: local Ollama backend active?

    if use_reasoning:
        client = get_reasoning_client()
        # L2: with Ollama every tier maps to the configured 14B; on vLLM use the
        # dedicated reasoning model only when a reasoning endpoint is configured.
        if ollama:
            model = resolve_ollama_model("reasoning")  # M1: smart selection + fallback
        else:
            model = cfg.vllm_reasoning_model if cfg.vllm_reasoning_url else cfg.vllm_deep_model
        max_tokens = max_tokens or cfg.reasoning_max_tokens
        temperature = temperature if temperature is not None else cfg.reasoning_temperature
    elif use_deep:
        client = get_deep_client()
        model = resolve_ollama_model("deep") if ollama else cfg.vllm_deep_model  # L2 + M1
        max_tokens = max_tokens or cfg.deep_max_tokens
        temperature = temperature if temperature is not None else cfg.deep_temperature
    else:
        client = get_fast_client()
        model = resolve_ollama_model("fast") if ollama else cfg.vllm_fast_model  # L2 + M1
        max_tokens = max_tokens or cfg.fast_max_tokens
        temperature = temperature if temperature is not None else cfg.fast_temperature

    tier = "reasoning" if use_reasoning else ("deep" if use_deep else "fast")

    # ── The gateway. Phase 8. ───────────────────────────────────────────────
    #
    # Three closure reports in a row recorded that the registry, the licence
    # gate and the privacy policy governed `IRAModelGateway` and that **nothing
    # in the product constructed one** — so "no unapproved model can serve" was
    # a property of a path nothing took. This is where that stops being true.
    #
    # The client and model resolved above are not wasted: `_client_factory` in
    # `core/models/composition.py` calls the same `get_*_client` functions, so
    # the gateway ends up dialling the same endpoint. What changes is that
    # policy, quota, registry and metering now run first, and a refusal is a
    # refusal rather than a completion nobody checked.
    #
    # `_dispatch` falls back to the direct call ONLY when no gateway could be
    # built at all — a deployment with no provider configured — and that path
    # is asserted in the Phase 8 truth pass so it cannot quietly become the
    # normal one.
    if stream:
        return await _stream_via_gateway(
            messages, tier=tier, max_tokens=max_tokens, temperature=temperature,
            client=client, model=model,
        )

    from utils.telemetry import trace_span
    with trace_span("llm.complete", {"tier": tier, "model": model, "max_tokens": max_tokens}):
        return await _complete_via_gateway(
            messages, tier=tier, max_tokens=max_tokens, temperature=temperature,
            response_format=response_format, client=client, model=model,
        )


async def stream_tokens(
    messages: list[dict],
    *,
    use_deep: bool = False,
    use_reasoning: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """Yield raw token strings from the streaming response.

    Handles three shapes, because `chat_complete(stream=True)` now has three
    things to return and collapsing them would hide the one that matters:

      * **a refusal** — the gateway declined before the provider was called.
        Yielded as its reason, once, so a caller that streams tokens into a UI
        shows the user why nothing is coming rather than an empty response.
      * **an iterator of strings** — the gateway's stream.
      * **an OpenAI chunk stream** — the direct fallback, when no gateway could
        be built.
    """
    response = await chat_complete(
        messages,
        use_deep=use_deep,
        use_reasoning=use_reasoning,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
    )

    if getattr(response, "ok", True) is False:
        yield response.reason
        return

    # ── Closed deterministically, not when the collector gets round to it ──
    #
    # The gateway meters a stream in a `finally`, including the case where the
    # caller stops reading. If this generator is closed and the inner one is
    # merely dropped, that `finally` runs whenever CPython happens to collect
    # it — which in practice meant a test closing the stream saw no usage row
    # at all, and in production would mean metering that sometimes happens.
    #
    # A billing record that depends on garbage-collection timing is not a
    # billing record, so the close is explicit.
    #
    # On the gateway path the provider says when the model stopped at its
    # output limit; on the direct path (OpenAI chunks) it is said here.
    answered = False
    finish_reason = None
    try:
        async for chunk in response:
            if isinstance(chunk, str):
                if chunk:
                    yield chunk
                continue
            if not chunk.choices:
                continue
            finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
            delta = chunk.choices[0].delta.content
            if delta:
                answered = answered or bool(delta.strip())
                yield delta
        from core.models.providers.openai_compatible import budget_notice
        notice = budget_notice(finish_reason, answered, None)
        if notice is not None:
            _log.warning("direct: the model stream stopped at its output limit")
            yield notice
    finally:
        aclose = getattr(response, "aclose", None)
        if aclose is not None:
            await aclose()


# ── Vision (image) path — local Ollama VL model, fail-soft ───────────────────
# Prompt 3.1: a local vision-language model served by Ollama (e.g. qwen2.5-VL /
# llava). Selection mirrors the text tiers (_use_ollama): local Ollama VL when the
# backend is ollama, otherwise the vLLM vision endpoint. Both fail soft.

_VISION_UNAVAILABLE = (
    "Vision is unavailable — no vision-language model is configured. Pull a VL model "
    "on the dev workstation (e.g. `ollama pull qwen2.5vl`) and set OLLAMA_VISION_MODEL."
)


def _vision_client_and_model() -> tuple[AsyncOpenAI | None, str | None]:
    """Return (client, model) for the vision path, or (None, None) if unconfigured."""
    cfg = get_settings()
    if _use_ollama():
        model = resolve_ollama_vision_model()  # M4: profile / IRA_LOCAL_VISION_MODEL
        if not model:
            return None, None
        return _make_ollama_client(), model
    if not cfg.vllm_vision_url:
        return None, None
    return _make_vllm_client(cfg.vllm_vision_url), (cfg.vllm_vision_model or "vision")


def vision_available() -> bool:
    """True when a vision-language model is configured for the active backend."""
    client, _model = _vision_client_and_model()
    return client is not None


def _vision_messages(prompt: str, image_b64: str, mime_type: str, system: str | None) -> list[dict]:
    data_url = f"data:{mime_type};base64,{image_b64}"
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]})
    return messages


async def vision_messages_complete(
    messages: list[dict],
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str:
    """Run a pre-built vision conversation through the gateway. Fail soft.

    ── Why this exists (P8-05, found by a guard rather than by design) ─────

    The Phase 8 guard that asserts the gateway is the only thing reaching an
    inference endpoint found **two route modules** building their own
    `AsyncOpenAI` against `VLLM_VISION_URL` and calling it directly:
    `api/routes/multimodal.py` and `api/routes/video_gen.py`. Neither went
    through `utils/llm.py` at all, so neither was governed by the privacy
    policy, the quota, the registry or the licence gate — and neither appeared
    in any usage row.

    They were not wired to `vision_complete` because they do things it cannot
    express: several images in one message, a custom system prompt, streaming.
    So the general entry point is this one, and `vision_complete` is now a thin
    caller of it.

    Fail-soft, like everything on the vision path: a returned string that says
    what went wrong, never an exception into a route that is streaming to a
    browser.
    """
    gw, request = _gateway_and_request(
        messages, tier="fast", max_tokens=max_tokens, temperature=temperature)
    if gw is not None:
        from core.interfaces.model_provider import Tier
        from dataclasses import replace as _replace

        request = _replace(request, tier=Tier.VISION)
        provider = next(iter(gw.providers.values()), None)
        if provider is not None and provider.supports(Tier.VISION):
            try:
                outcome = await gw.complete(
                    request, principal=await _principal_or_none(),
                    privacy_mode=_privacy_mode())
            except Exception as exc:  # noqa: BLE001 — fail soft
                return f"Vision unavailable: {str(exc)[:160]}"
            if getattr(outcome, "ok", True) is False:
                return outcome.reason
            return (outcome.text or "").strip()

    # No gateway, or no vision model registered on it. The direct path stays
    # for exactly the case `_VISION_UNAVAILABLE` describes, and says so.
    client, model = _vision_client_and_model()
    if client is None:
        return _VISION_UNAVAILABLE
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 — fail soft
        return f"Vision unavailable: {str(exc)[:160]}"


async def vision_complete(
    *, prompt: str, image_b64: str, mime_type: str = "image/jpeg", system: str | None = None,
) -> str:
    """Call the VL model with an image + prompt and return text. Fail soft (never raises)."""
    if not vision_available():
        return _VISION_UNAVAILABLE
    return await vision_messages_complete(
        _vision_messages(prompt, image_b64, mime_type, system))


async def stream_vision_messages(
    messages: list[dict],
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> AsyncIterator[str]:
    """Stream a pre-built vision conversation through the gateway. Fail soft.

    The streaming twin of `vision_messages_complete`, and it exists for the
    same reason: `api/routes/video_gen.py` streamed frame analysis straight
    from its own client, ungoverned and unmetered.

    A refusal is yielded as its reason and nothing else follows, so a route
    piping this into a server-sent-event stream shows the user why there is no
    answer instead of an empty response.
    """
    gw, request = _gateway_and_request(
        messages, tier="fast", max_tokens=max_tokens, temperature=temperature)
    if gw is not None:
        from core.interfaces.model_provider import Tier
        from dataclasses import replace as _replace

        request = _replace(request, tier=Tier.VISION)
        provider = next(iter(gw.providers.values()), None)
        if provider is not None and provider.supports(Tier.VISION):
            try:
                outcome = await gw.stream(
                    request, principal=await _principal_or_none(),
                    privacy_mode=_privacy_mode())
                if getattr(outcome, "ok", True) is False:
                    yield outcome.reason
                    return
                async for token in outcome:
                    if token:
                        yield token
            except Exception as exc:  # noqa: BLE001 — fail soft
                yield f"Vision unavailable: {str(exc)[:160]}"
            return

    client, model = _vision_client_and_model()
    if client is None:
        yield _VISION_UNAVAILABLE
        return
    try:
        stream = await client.chat.completions.create(
            model=model, messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens, temperature=temperature, stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as exc:  # noqa: BLE001 — fail soft
        yield f"Vision unavailable: {str(exc)[:160]}"


async def stream_vision_tokens(
    *, prompt: str, image_b64: str, mime_type: str = "image/jpeg", system: str | None = None,
) -> AsyncIterator[str]:
    """Stream tokens from the VL model; on unavailable/error yield a clear message (fail soft)."""
    client, model = _vision_client_and_model()
    if client is None:
        yield _VISION_UNAVAILABLE
        return
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=_vision_messages(prompt, image_b64, mime_type, system),  # type: ignore[arg-type]
            max_tokens=2048,
            temperature=0.3,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as exc:  # noqa: BLE001 — fail soft
        yield f"Vision unavailable: {str(exc)[:160]}"
