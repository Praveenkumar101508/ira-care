"""P0 (performance), ranked item 3: every generation fits the model's context.

Traced on a clean install on 1 November 2026, with `qwen3:4b` (Qwen3 4B
Thinking 2507) on Ollama, which runs a 4,096-token context: IRA asked for up to
4,096 (fast), 16,384 (deep) or 32,768 (reasoning) output tokens. A greeting
(935 prompt tokens) reasoned past 3,161 generated tokens, filled the context,
and llama-server shifted it (`n_discard = 2045`): the system prompt and the
question were discarded and the model went on generating without them.

Ollama's OpenAI-compatible route has no field for the context size, so the
provider cannot send one. It keeps each request inside the context instead,
and when the model stops at that limit the user is told so, rather than shown
an empty or truncated reply as though it were complete.

Each test here was checked against a planted defect (main's behaviour put
back); the evidence folder records which defect turns which test red.
"""
from __future__ import annotations

import logging

import pytest

from core.interfaces.model_provider import CompletionRequest, Locality, Tier
from core.models.providers import OpenAICompatibleProvider
from core.models.providers.openai_compatible import (
    DEFAULT_OLLAMA_CONTEXT,
    MIN_OUTPUT_TOKENS,
    estimate_prompt_tokens,
    fit_output_budget,
    ollama_context_tokens,
)

# ── A fake `/v1` endpoint that can reason, answer and stop at its limit ─────


class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning = reasoning


class _StreamChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, model="qwen3:4b"):
        self.choices = choices
        self.model = model


class _Message:
    def __init__(self, content, reasoning=""):
        self.content = content
        self.reasoning = reasoning


class _Choice:
    def __init__(self, content, finish_reason, reasoning=""):
        self.message = _Message(content, reasoning)
        self.finish_reason = finish_reason


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(self, content, finish_reason, completion_tokens=10):
        self.choices = [_Choice(content, finish_reason, reasoning="hidden")]
        self.model = "qwen3:4b"
        self.usage = _Usage(50, completion_tokens)


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class FakeThinkingModel:
    """Records each request. Streams `reasoning` hidden tokens, then `answer`
    as visible tokens, then a finish chunk; `finish` says why it stopped."""

    def __init__(self, *, reasoning=3, answer=("Good ", "morning!"), finish="stop"):
        self.calls: list[dict] = []
        self.reasoning = reasoning
        self.answer = list(answer)
        self.finish = finish
        self.chat = type("chat", (), {"completions": self})()

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [_Chunk([_StreamChoice(_Delta(reasoning="think "))])
                      for _ in range(self.reasoning)]
            chunks += [_Chunk([_StreamChoice(_Delta(content=t))]) for t in self.answer]
            chunks.append(_Chunk([_StreamChoice(_Delta(), finish_reason=self.finish)]))
            return _AsyncIter(chunks)
        return _Response("".join(self.answer), self.finish)


def build(endpoint, *, context_tokens=4096, tiers=None):
    provider = OpenAICompatibleProvider(
        provider_id="ollama-local",
        display_name="Local Ollama",
        locality=Locality.LOCAL,
        base_url="http://127.0.0.1:11434/v1",
        api_key="ollama",
        model_for_tier=tiers or {Tier.FAST: "qwen3:4b", Tier.DEEP: "qwen3:4b",
                                 Tier.REASONING: "qwen3:4b",
                                 Tier.VISION: "qwen2.5vl"},
        retains_prompts=False,
        region="on-device",
        trains_on_data=False,
        client_factory=lambda tier, url, key, timeout: endpoint,
        context_tokens=context_tokens,
    )
    return provider


#: About the size of the traced greeting's prompt: 4,092 characters, which the
#: server counted as 935 tokens.
PERSONA = "SYNTHETIC persona line for a prompt-size test. " * 85
GREETING = [
    {"role": "system", "content": PERSONA},
    {"role": "user", "content": "SYNTHETIC: good morning! Reply with one short friendly sentence."},
]


def _request(tier=Tier.FAST, max_tokens=4096, messages=GREETING):
    return CompletionRequest(messages=messages, tier=tier, temperature=0.6,
                             max_tokens=max_tokens)


# ── The request asks for no more than fits ─────────────────────────────────


@pytest.mark.parametrize("tier,asked", [
    (Tier.FAST, 4096), (Tier.DEEP, 16384), (Tier.REASONING, 32768),
])
async def test_each_tier_asks_for_no_more_than_the_context_leaves(tier, asked):
    """The defect: each tier's configured output limit was sent as it was,
    whatever the context. On a 4,096-token context every one of these can run
    the model past the window."""
    endpoint = FakeThinkingModel()
    provider = build(endpoint)
    [_ async for _ in provider.stream(_request(tier, asked))]

    sent = endpoint.calls[0]["max_tokens"]
    assert sent is not None and sent < asked
    assert estimate_prompt_tokens(GREETING) + sent <= 4096, (
        f"asked for {sent} output tokens on top of a prompt estimated at "
        f"{estimate_prompt_tokens(GREETING)}: past a 4,096-token context")


async def test_the_non_streamed_request_is_bounded_the_same_way():
    endpoint = FakeThinkingModel()
    await build(endpoint).complete(_request(Tier.DEEP, 16384))
    sent = endpoint.calls[0]["max_tokens"]
    assert estimate_prompt_tokens(GREETING) + sent <= 4096


async def test_a_request_that_already_fits_is_left_alone():
    """The cap only ever lowers a limit. A caller that asked for 10 tokens
    gets 10, not the room left in the context."""
    endpoint = FakeThinkingModel()
    await build(endpoint).complete(_request(Tier.FAST, 10))
    assert endpoint.calls[0]["max_tokens"] == 10


async def test_a_request_with_no_limit_gets_one():
    """No limit at all is the worst case: Ollama then generates until the
    model stops, shifting the context as often as it fills."""
    endpoint = FakeThinkingModel()
    await build(endpoint).complete(_request(Tier.FAST, None))
    sent = endpoint.calls[0]["max_tokens"]
    assert sent == 4096 - estimate_prompt_tokens(GREETING)


async def test_the_budget_follows_the_configured_context():
    endpoint = FakeThinkingModel()
    await build(endpoint, context_tokens=8192).complete(_request(Tier.DEEP, 16384))
    assert endpoint.calls[0]["max_tokens"] == 8192 - estimate_prompt_tokens(GREETING)


async def test_a_prompt_that_nearly_fills_the_context_still_gets_the_floor(caplog):
    long_prompt = [{"role": "user", "content": "word " * 6000}]
    endpoint = FakeThinkingModel()
    with caplog.at_level(logging.WARNING, logger="ira.models.providers.openai"):
        await build(endpoint).complete(_request(Tier.FAST, 4096, long_prompt))
    assert endpoint.calls[0]["max_tokens"] == MIN_OUTPUT_TOKENS
    assert any("leaves" in r.getMessage() for r in caplog.records)


async def test_a_provider_with_no_context_to_keep_sends_the_limit_unchanged():
    """vLLM refuses an oversized request itself, so it is given no context and
    its requests are not touched."""
    endpoint = FakeThinkingModel()
    await build(endpoint, context_tokens=None).complete(_request(Tier.DEEP, 16384))
    assert endpoint.calls[0]["max_tokens"] == 16384


async def test_an_image_request_is_not_capped_by_a_text_estimate():
    """An image's token cost is not in the text, so the estimate would be
    wrong for it in an unknown direction. The image path is left as it was."""
    endpoint = FakeThinkingModel()
    await build(endpoint).complete(_request(Tier.VISION, 4096))
    assert endpoint.calls[0]["max_tokens"] == 4096


# ── The context comes from configuration ───────────────────────────────────


class _Settings:
    llm_backend = "ollama"
    dev_mode = False
    ollama_base_url = "http://127.0.0.1:11434/v1"
    ollama_model_fast = ollama_model_deep = ollama_model_reasoning = "qwen3:4b"
    ollama_vision_model = ""
    ira_use_model_router = False


@pytest.mark.parametrize("value,expected", [
    (None, DEFAULT_OLLAMA_CONTEXT), (0, DEFAULT_OLLAMA_CONTEXT),
    ("not a number", DEFAULT_OLLAMA_CONTEXT), (8192, 8192),
])
def test_the_ollama_context_is_read_from_settings(value, expected):
    cfg = _Settings()
    if value is not None:
        cfg.ollama_context_length = value
    assert ollama_context_tokens(cfg) == expected


async def test_the_ollama_provider_from_settings_keeps_to_its_context():
    cfg = _Settings()
    cfg.ollama_context_length = 4096
    endpoint = FakeThinkingModel()
    provider = OpenAICompatibleProvider.from_settings(
        cfg, client_factory=lambda *a: endpoint)
    await provider.complete(_request(Tier.REASONING, 32768))
    assert endpoint.calls[0]["max_tokens"] + estimate_prompt_tokens(GREETING) <= 4096


async def test_the_vllm_provider_from_settings_is_not_capped():
    cfg = _Settings()
    cfg.llm_backend = "vllm"
    cfg.vllm_fast_url = "http://vllm.internal/v1"
    cfg.vllm_deep_url = cfg.vllm_reasoning_url = cfg.vllm_vision_url = ""
    cfg.vllm_fast_model = cfg.vllm_deep_model = "Qwen3-8B"
    cfg.vllm_reasoning_model = ""
    endpoint = FakeThinkingModel()
    provider = OpenAICompatibleProvider.from_settings(
        cfg, client_factory=lambda *a: endpoint)
    await provider.complete(_request(Tier.DEEP, 16384))
    assert endpoint.calls[0]["max_tokens"] == 16384


def test_the_application_setting_exists_and_defaults_to_what_ollama_runs():
    from config import Settings

    assert Settings.model_fields["ollama_context_length"].default == 4096


# ── Stopping at the limit is reported, never passed off as an answer ───────


async def test_a_stream_that_spends_its_budget_reasoning_says_so(caplog):
    """The case the trace showed: every token was hidden reasoning. Without
    this the user gets an empty reply that looks like IRA had nothing to say."""
    endpoint = FakeThinkingModel(reasoning=50, answer=(), finish="length")
    provider = build(endpoint)
    with caplog.at_level(logging.WARNING, logger="ira.models.providers.openai"):
        got = [t async for t in provider.stream(_request())]

    text = "".join(got)
    sent = endpoint.calls[0]["max_tokens"]
    assert "ran out of its answer budget" in text
    assert f"{sent:,} tokens" in text
    assert "before it wrote any answer" in text
    assert any("before writing any answer" in r.getMessage() for r in caplog.records)


async def test_a_stream_cut_off_at_the_limit_is_marked_incomplete():
    endpoint = FakeThinkingModel(answer=("The first half ", "of an answer"),
                                 finish="length")
    got = [t async for t in build(endpoint).stream(_request())]
    assert got[:2] == ["The first half ", "of an answer"]
    assert len(got) == 3 and "This answer is incomplete" in got[2]


async def test_a_stream_that_finishes_normally_is_unchanged():
    endpoint = FakeThinkingModel(finish="stop")
    got = [t async for t in build(endpoint).stream(_request())]
    assert got == ["Good ", "morning!"]


async def test_a_completion_that_spends_its_budget_reasoning_says_so(caplog):
    endpoint = FakeThinkingModel(answer=(), finish="length")
    with caplog.at_level(logging.WARNING, logger="ira.models.providers.openai"):
        result = await build(endpoint).complete(_request())
    assert result.text.startswith("I could not answer this time")
    assert "ran out of its answer budget" in result.text
    assert any("completion tokens 10" in r.getMessage() for r in caplog.records)


async def test_a_completion_cut_off_at_the_limit_is_marked_incomplete():
    endpoint = FakeThinkingModel(answer=("half an answer",), finish="length")
    result = await build(endpoint).complete(_request())
    assert result.text.startswith("half an answer")
    assert "This answer is incomplete" in result.text


async def test_a_completion_that_finishes_normally_is_unchanged():
    endpoint = FakeThinkingModel(finish="stop")
    result = await build(endpoint).complete(_request())
    assert result.text == "Good morning!"


# ── The estimate errs towards a smaller budget ─────────────────────────────


def test_the_estimate_is_above_what_the_server_counted_on_the_traced_prompts():
    """The server counted the traced prompts at 4.4 to 4.6 characters per
    token, chat template included. The estimate must stay above that, or the
    budget would overstate the room left."""
    for chars, counted in ((1726, 383), (4092, 935), (1780, 395), (1215, 266), (4362, 983)):
        system = "s" * (chars - 40)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": "q" * 40}]
        assert estimate_prompt_tokens(messages) > counted


def test_digits_and_non_ascii_text_are_counted_heavily():
    assert estimate_prompt_tokens([{"content": "1234567890" * 30}]) >= 300
    telugu = "నమస్కారం" * 20   # 8 characters, 24 UTF-8 bytes, per repeat
    assert estimate_prompt_tokens([{"content": telugu}]) >= len(telugu.encode()) // 2


def test_fit_output_budget_never_raises_the_callers_limit():
    assert fit_output_budget([{"content": "hi"}], 5, 4096) == 5
