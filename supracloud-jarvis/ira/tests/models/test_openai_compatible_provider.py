"""P8-01 — the provider the gateway never had.

Three closure reports recorded the same limitation: *"nothing in the product
constructs one"*. The reason was simpler than the sentence suggests — there was
no `ModelProvider` implementation anywhere in the tree, so there was nothing to
construct a gateway around.

Most of this file is about **locality**, because that is the field a wrong value
in is not a routing bug but a false privacy claim, and about the approved-model
contract, because a provider that substitutes a model produces an answer that is
served, metered and attributed to weights nobody approved.
"""
from __future__ import annotations

import pytest

from core.interfaces.model_provider import (
    ApprovedModel,
    CompletionRequest,
    Locality,
    Tier,
)
from core.models.providers import (
    OpenAICompatibleProvider,
    ProviderMisconfigured,
    UnsupportedApprovedModel,
)

# No `pytestmark = pytest.mark.asyncio` here: `pytest.ini` sets
# `asyncio_mode = auto`, so async tests are collected without it, and applying
# it at module level marks the SYNC tests too — which pytest-asyncio warns
# about and which would quietly stop being a warning one day.


# ── A fake `/v1` endpoint ────────────────────────────────────────────────────


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)
        self.delta = _Msg(content)


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Response:
    def __init__(self, *, text="an answer", model="asked-for", usage=(11, 22)):
        self.choices = [_Choice(text)]
        self.model = model
        self.usage = _Usage(*usage) if usage else None


class _Chunk:
    def __init__(self, content, model="asked-for"):
        self.choices = [_Choice(content)]
        self.model = model


class _Completions:
    def __init__(self, endpoint):
        self._endpoint = endpoint

    async def create(self, **kwargs):
        self._endpoint.calls.append(kwargs)
        if kwargs.get("stream"):
            return _AsyncIter(self._endpoint.chunks)
        return self._endpoint.response


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class FakeEndpoint:
    """Records what was asked for. The assertions are mostly about that."""

    def __init__(self, *, served_model="asked-for", chunks=None):
        self.calls: list[dict] = []
        self.response = _Response(model=served_model)
        self.chunks = chunks or [_Chunk("an ", served_model),
                                 _Chunk("answer", served_model)]
        self.chat = type("chat", (), {"completions": _Completions(self)})()


def build(**overrides):
    endpoint = overrides.pop("endpoint", None) or FakeEndpoint()
    kwargs = dict(
        provider_id="ollama-local",
        display_name="Local Ollama",
        locality=Locality.LOCAL,
        base_url="http://127.0.0.1:11434/v1",
        api_key="ollama",
        model_for_tier={Tier.FAST: "qwen3:8b", Tier.DEEP: "qwen3:14b"},
        retains_prompts=False,
        region="on-device",
        trains_on_data=False,
        client_factory=lambda tier, url, key, timeout: endpoint,
    )
    kwargs.update(overrides)
    return OpenAICompatibleProvider(**kwargs), endpoint


# ── Locality ─────────────────────────────────────────────────────────────────


def test_a_provider_that_cannot_state_its_locality_will_not_construct():
    """At construction, not at call time. A provider that discovers halfway
    through a request that it does not know where it is has already accepted
    the prompt."""
    with pytest.raises(ProviderMisconfigured) as exc:
        build(locality="local")  # a string that LOOKS right
    assert "not inferred from a URL" in str(exc.value)


def test_locality_is_not_guessed_from_the_base_url():
    """A `localhost` URL can be an SSH tunnel to a rented box. The constructor
    takes whatever it is told, so a deployment that means SELF_HOSTED gets
    SELF_HOSTED even on a loopback address."""
    provider, _ = build(locality=Locality.SELF_HOSTED,
                        base_url="http://127.0.0.1:11434/v1")
    assert provider.disclosure().locality is Locality.SELF_HOSTED


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", Locality.LOCAL),
    ("http://127.0.0.1:11434/v1", Locality.LOCAL),
    ("http://[::1]:11434/v1", Locality.LOCAL),
    ("http://gpu-box.lan:11434/v1", Locality.SELF_HOSTED),
    ("https://ollama.example.com/v1", Locality.SELF_HOSTED),
])
def test_the_one_inference_only_ever_weakens_the_claim(url, expected):
    """`from_settings` derives Ollama's locality from the host, and the only
    direction it can move is LOCAL → SELF_HOSTED. Telling a user their words
    never left the machine when they crossed a network is the failure; the
    reverse is merely conservative."""
    assert OpenAICompatibleProvider._ollama_locality(url) is expected


def test_a_remote_ollama_does_not_claim_to_retain_nothing():
    """`retains_prompts=False` is a statement about software running on this
    machine. On somebody's server nobody has checked it, and `None` is what
    "not known" looks like — the distinction `model_providers.retains_prompts`
    is nullable for."""
    cfg = _Settings(llm_backend="ollama", ollama_base_url="http://gpu.lan:11434/v1")
    provider = OpenAICompatibleProvider.from_settings(
        cfg, client_factory=lambda *a: FakeEndpoint())
    d = provider.disclosure()
    assert d.locality is Locality.SELF_HOSTED
    assert d.retains_prompts is None
    assert d.trains_on_data is None
    assert d.region == ""


def test_a_local_ollama_states_what_is_true_of_it():
    cfg = _Settings(llm_backend="ollama", ollama_base_url="http://127.0.0.1:11434/v1")
    provider = OpenAICompatibleProvider.from_settings(
        cfg, client_factory=lambda *a: FakeEndpoint())
    d = provider.disclosure()
    assert d.locality is Locality.LOCAL
    assert d.retains_prompts is False and d.trains_on_data is False
    assert d.leaves_the_machine is False and d.is_third_party is False


def test_an_unchecked_vllm_reports_unknown_retention_not_clean():
    cfg = _Settings(llm_backend="vllm", vllm_api_key="k",
                    vllm_fast_url="https://vllm.internal/v1",
                    vllm_fast_model="Qwen3-8B", vllm_deep_model="Qwen3-14B")
    provider = OpenAICompatibleProvider.from_settings(
        cfg, client_factory=lambda *a: FakeEndpoint())
    d = provider.disclosure()
    assert d.locality is Locality.SELF_HOSTED
    assert d.retains_prompts is None, (
        "an unchecked provider was disclosed as one that retains nothing")


# ── Tiers ────────────────────────────────────────────────────────────────────


def test_supports_answers_from_what_is_configured_not_from_the_enum():
    provider, _ = build()
    assert provider.supports(Tier.FAST) and provider.supports(Tier.DEEP)
    assert not provider.supports(Tier.REASONING)
    assert not provider.supports(Tier.VISION)


async def test_a_tier_with_no_model_refuses_rather_than_picking_one():
    provider, _ = build()
    with pytest.raises(UnsupportedApprovedModel):
        await provider.complete(CompletionRequest(messages=[{"role": "user",
                                                             "content": "hi"}],
                                                  tier=Tier.REASONING))


# ── The approved model ───────────────────────────────────────────────────────


def _approved(**over):
    kwargs = dict(model_key="local_fast", version=3, base_model="Qwen/Qwen3-8B",
                  revision="b968826d", artifact_hash="sha256:aa",
                  licence_identifier="Apache License")
    kwargs.update(over)
    return ApprovedModel(**kwargs)


async def test_the_registry_beats_configuration():
    """The configured model for FAST is `qwen3:8b`. When the registry approves
    `Qwen/Qwen3-8B`, that is what gets asked for — otherwise the rollout and
    rollback in 067 could not influence which weights answered."""
    provider, endpoint = build()
    endpoint.response = _Response(model="Qwen/Qwen3-8B")
    result = await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
        approved_model=_approved()))
    assert endpoint.calls[0]["model"] == "Qwen/Qwen3-8B"
    assert result.model_identity == "local_fast@3:b968826d"


async def test_an_approval_naming_nothing_addressable_raises():
    """Rather than falling back to the configured model, which is the exact
    substitution the contract forbids."""
    provider, _ = build()
    with pytest.raises(UnsupportedApprovedModel) as exc:
        await provider.complete(CompletionRequest(
            messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
            approved_model=_approved(base_model="", model_key="")))
    assert "nobody approved" in str(exc.value)


async def test_a_substituting_backend_has_its_answer_refused():
    """**The one that matters.** The endpoint is asked for the approved model
    and answers as something else. Returning that would serve, meter and
    attribute an answer to weights nobody approved."""
    provider, endpoint = build()
    endpoint.response = _Response(model="qwen3:0.5b")
    with pytest.raises(UnsupportedApprovedModel) as exc:
        await provider.complete(CompletionRequest(
            messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
            approved_model=_approved()))
    assert "substitutes a model" in str(exc.value)


async def test_an_unapproved_request_reports_no_identity():
    """`model_identity` is what the gateway compares against the approval. With
    no approval there is nothing to echo, and inventing one would make an
    ungoverned deployment look governed."""
    provider, _ = build()
    result = await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))
    assert result.model_identity == ""


async def test_the_result_reports_what_the_server_ran_not_what_we_asked():
    """With no approval in play a backend may legitimately serve a different
    name — an alias, a resolved tag. Recording the asked-for name would hide
    that in the usage table as well as in the result."""
    provider, endpoint = build()
    endpoint.response = _Response(model="qwen3:8b-instruct-q4_K_M")
    result = await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))
    assert endpoint.calls[0]["model"] == "qwen3:8b"
    assert result.model == "qwen3:8b-instruct-q4_K_M"


# ── Tokens, options and streaming ───────────────────────────────────────────


async def test_usage_is_read_from_the_response():
    """Metering is only as good as this. A provider returning zeros makes every
    quota unenforceable while looking like it works."""
    provider, _ = build()
    result = await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))
    assert (result.prompt_tokens, result.completion_tokens) == (11, 22)


async def test_a_response_without_usage_reports_zero_rather_than_raising():
    provider, endpoint = build()
    endpoint.response = _Response(usage=None)
    result = await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)


async def test_response_format_is_passed_through_only_when_set():
    provider, endpoint = build()
    await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))
    assert "response_format" not in endpoint.calls[0]
    await provider.complete(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
        response_format={"type": "json_object"}))
    assert endpoint.calls[1]["response_format"] == {"type": "json_object"}


async def test_streaming_yields_deltas():
    provider, endpoint = build()
    got = [chunk async for chunk in provider.stream(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))]
    assert "".join(got) == "an answer"
    assert endpoint.calls[0]["stream"] is True


async def test_a_stream_from_a_substituting_backend_stops_at_the_first_token():
    """After the first token there is nothing useful to do with the answer — it
    is already on its way to a user. So the check happens before it."""
    provider, endpoint = build()
    endpoint.chunks = [_Chunk("an ", "qwen3:0.5b"), _Chunk("answer", "qwen3:0.5b")]
    stream = provider.stream(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
        approved_model=_approved()))
    with pytest.raises(UnsupportedApprovedModel):
        [chunk async for chunk in stream]


async def test_a_stream_is_never_retried():
    """`utils/llm.py` has always said why: replaying a broken stream sends the
    caller duplicate tokens. Asserted by counting calls rather than by reading
    the decorator, because a retry could be added anywhere in the chain."""
    provider, endpoint = build()
    [c async for c in provider.stream(CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))]
    assert len(endpoint.calls) == 1


# ── A minimal settings stand-in ─────────────────────────────────────────────


class _Settings:
    def __init__(self, **kw):
        self.llm_backend = kw.get("llm_backend", "ollama")
        self.dev_mode = kw.get("dev_mode", False)
        self.ollama_base_url = kw.get("ollama_base_url", "http://127.0.0.1:11434/v1")
        self.ollama_model_fast = kw.get("ollama_model_fast", "qwen3:8b")
        self.ollama_model_deep = kw.get("ollama_model_deep", "qwen3:14b")
        self.ollama_model_reasoning = kw.get("ollama_model_reasoning", "qwen3:14b")
        self.ollama_vision_model = kw.get("ollama_vision_model", "")
        self.ira_use_model_router = kw.get("ira_use_model_router", False)
        self.vllm_api_key = kw.get("vllm_api_key", "")
        self.vllm_fast_url = kw.get("vllm_fast_url", "")
        self.vllm_deep_url = kw.get("vllm_deep_url", "")
        self.vllm_reasoning_url = kw.get("vllm_reasoning_url", "")
        self.vllm_fast_model = kw.get("vllm_fast_model", "")
        self.vllm_deep_model = kw.get("vllm_deep_model", "")
        self.vllm_reasoning_model = kw.get("vllm_reasoning_model", "")
