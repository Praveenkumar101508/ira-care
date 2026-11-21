"""P8 — the live inference path, driven end to end through the gateway.

Three closure reports recorded the same limitation and all three were careful to
say what it did NOT mean:

    "an unapproved model produces zero provider calls" must be read as a
    property of the gateway, not as "no unapproved model can run in IRA today"

These tests are the difference. Everything here drives `utils.llm.chat_complete`
— the function forty-three modules call — and asserts what happens to a real
request, rather than asserting that a source file mentions a class.

The structural guards in `test_p5_truth_pass` and `test_p6_truth_pass` check that
the wiring exists. This checks that the wiring *does something*, because a
gateway that is constructed and then bypassed would satisfy both of those.
"""
from __future__ import annotations

import os

# `chat_complete` reads real settings, and Settings requires these. Set before
# the first import of anything that builds one — the same preamble
# `tests/test_vision.py` uses, for the same reason.
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest  # noqa: E402

import utils.llm as llm  # noqa: E402
from core.interfaces.model_provider import (  # noqa: E402
    CompletionResult,
    Locality,
    ProviderDisclosure,
    Tier,
)
from core.models import composition  # noqa: E402
from core.models.gateway import IRAModelGateway, Quota, UsageSoFar  # noqa: E402


class RecordingProvider:
    """A provider that records what it was asked and answers predictably."""

    def __init__(self, provider_id="ollama-local", locality=Locality.LOCAL,
                 retains=False, text="an answer", fails=False):
        self._id = provider_id
        self._locality = locality
        self._retains = retains
        self._text = text
        self._fails = fails
        self.calls: list = []
        self.streams: list = []

    @property
    def provider_id(self):
        return self._id

    def disclosure(self):
        return ProviderDisclosure(
            provider_id=self._id, display_name=self._id,
            locality=self._locality, retains_prompts=self._retains,
            region="on-device" if self._locality is Locality.LOCAL else "",
            trains_on_data=False)

    def supports(self, tier: Tier) -> bool:
        return True

    async def complete(self, request):
        self.calls.append(request)
        if self._fails:
            raise RuntimeError("provider exploded")
        return CompletionResult(
            text=self._text, model="qwen3:8b", provider_id=self._id,
            disclosure=self.disclosure(), prompt_tokens=7, completion_tokens=9)

    async def stream(self, request):
        self.streams.append(request)
        if self._fails:
            raise RuntimeError("provider exploded")
        for token in ("an ", "answer"):
            yield token


@pytest.fixture()
def wired(monkeypatch):
    """Install a gateway with a recording provider on the live path.

    Patches `composition.gateway`, which is the one function `utils/llm.py`
    reaches for. Nothing here patches `chat_complete` itself — the point is to
    exercise the real dispatch.
    """
    state: dict = {"metered": []}
    provider = RecordingProvider()

    def _build(**_kw):
        gw = IRAModelGateway(costs={provider.provider_id: {}})
        gw.register(provider)
        gw.quota_reader = state.get("quota_reader")
        gw.usage_writer = lambda _p, row: state["metered"].append(row)
        gw.registry_required = state.get("registry_required", False)
        return gw

    monkeypatch.setattr(composition, "gateway", _build)
    monkeypatch.setattr(llm, "_privacy_mode", lambda: state.get("mode", "local_first"))
    monkeypatch.setattr(llm, "_principal_or_none",
                        _async(lambda: state.get("principal")))
    state["provider"] = provider
    yield state
    composition.reset_gateway()


def _async(fn):
    async def _inner(*a, **kw):
        return fn(*a, **kw)
    return _inner


class FakePrincipal:
    import uuid as _uuid
    tenant_id = _uuid.UUID("f0f0f0f0-0000-4000-8000-00000000000a")
    user_id = _uuid.UUID("f0f0f0f0-0000-4000-8000-0000000000aa")
    username = "owner"


# ── The claim the phase exists to make ──────────────────────────────────────


async def test_a_completion_reaches_the_provider_through_the_gateway(wired):
    out = await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert out == "an answer"
    assert len(wired["provider"].calls) == 1, (
        "chat_complete did not reach the provider through the gateway")


async def test_the_tier_the_caller_asked_for_survives_the_route(wired):
    await llm.chat_complete([{"role": "user", "content": "hi"}], use_deep=True)
    await llm.chat_complete([{"role": "user", "content": "hi"}], use_reasoning=True)
    assert [c.tier for c in wired["provider"].calls] == [Tier.DEEP, Tier.REASONING]


async def test_the_caller_cannot_choose_which_model_approves_it(wired):
    """`CompletionRequest.approved_model` is set by the gateway from the
    registry. `chat_complete` has no parameter that reaches it, and a request
    arriving with one already set is refused — so the live path cannot be used
    to smuggle an approval in."""
    await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert wired["provider"].calls[0].approved_model is None


async def test_a_privacy_refusal_reaches_the_caller_as_words_not_an_exception(wired):
    """`local_only` with an external provider: the policy refuses, and forty
    three call sites that expect a string get a sentence explaining why."""
    wired["provider"] = RecordingProvider(provider_id="clean-cloud",
                                          locality=Locality.EXTERNAL)
    external = wired["provider"]

    def _build(**_kw):
        gw = IRAModelGateway()
        gw.register(external)
        return gw

    import core.models.composition as comp
    comp.gateway = _build  # patched by the fixture's monkeypatch scope
    wired["mode"] = "local_only"

    out = await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert isinstance(out, str)
    assert "local_only" in out or "never leaves this machine" in out
    assert external.calls == [], (
        "the prompt reached an external provider that the privacy policy "
        "refused — a refusal that happens after the call is not a refusal")


async def test_an_exhausted_quota_refuses_the_live_path(wired):
    wired["principal"] = FakePrincipal()
    wired["quota_reader"] = lambda _p: (
        Quota(daily_request_limit=1, on_exceeded="refuse"),
        UsageSoFar(daily_requests=1))

    out = await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert isinstance(out, str) and "requests" in out.lower()
    assert wired["provider"].calls == [], (
        "a request over the daily ceiling still reached the provider")


async def test_a_successful_call_is_metered_with_its_tokens(wired):
    wired["principal"] = FakePrincipal()
    await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert len(wired["metered"]) == 1
    row = wired["metered"][0]
    assert row["outcome"] == "ok"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 9)
    assert row["locality"] == "local"


async def test_nothing_is_metered_without_a_principal(wired):
    """A private install has one user and no tenant. Metering against nobody
    would be a row attributed to whoever is read next."""
    await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert wired["metered"] == []


# ── Streaming ───────────────────────────────────────────────────────────────


async def test_streaming_goes_through_the_gateway_and_yields_tokens(wired):
    tokens = [t async for t in llm.stream_tokens([{"role": "user", "content": "hi"}])]
    assert "".join(tokens) == "an answer"
    assert len(wired["provider"].streams) == 1


async def test_a_refused_stream_never_opens_one(wired):
    """The streaming version of P5-07: a check after the first token has
    already sent part of the answer to a user."""
    wired["principal"] = FakePrincipal()
    wired["quota_reader"] = lambda _p: (
        Quota(daily_request_limit=1, on_exceeded="refuse"),
        UsageSoFar(daily_requests=1))

    tokens = [t async for t in llm.stream_tokens([{"role": "user", "content": "hi"}])]
    assert wired["provider"].streams == [], (
        "a refused request still opened a stream to the provider")
    assert tokens and "requests" in tokens[0].lower(), (
        "a refused stream produced no explanation, so the user sees nothing "
        "happen at all")


async def test_a_finished_stream_is_metered_once(wired):
    wired["principal"] = FakePrincipal()
    [t async for t in llm.stream_tokens([{"role": "user", "content": "hi"}])]
    assert len(wired["metered"]) == 1
    assert wired["metered"][0]["outcome"] == "ok"


async def test_an_abandoned_stream_is_still_metered(wired):
    """A tenant who starts a thousand streams and reads none of them did a
    thousand streams' worth of work. `abandoned` is its own outcome — 075
    widened the column for it."""
    wired["principal"] = FakePrincipal()

    agen = llm.stream_tokens([{"role": "user", "content": "hi"}])
    assert await agen.__anext__() == "an "
    await agen.aclose()

    assert len(wired["metered"]) == 1, "an abandoned stream was never metered"
    assert wired["metered"][0]["outcome"] == "abandoned"


async def test_a_stream_reports_no_tokens_rather_than_an_estimate(wired):
    """Stated as a limitation in `gateway.stream`, asserted here so it cannot
    quietly become an estimate: a guessed number in a usage row becomes a
    guessed number in a bill."""
    wired["principal"] = FakePrincipal()
    [t async for t in llm.stream_tokens([{"role": "user", "content": "hi"}])]
    row = wired["metered"][0]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (0, 0)


# ── The fallback, bounded ───────────────────────────────────────────────────


async def test_with_no_provider_configured_the_direct_path_still_answers(monkeypatch):
    """A deployment with nothing configured must not lose its chat path to a
    gateway that has nowhere to route. The fallback exists for exactly this,
    and `test_p8_truth_pass` asserts it is the only thing that reaches it."""
    empty = IRAModelGateway()
    monkeypatch.setattr(composition, "gateway", lambda **_kw: empty)

    called: list = []

    async def _direct(client, model, messages, max_tokens, temperature, rf=None):
        called.append(model)
        return "direct answer"

    monkeypatch.setattr(llm, "_complete_no_stream", _direct)
    monkeypatch.setattr(llm, "get_fast_client", lambda: object())

    out = await llm.chat_complete([{"role": "user", "content": "hi"}])
    assert out == "direct answer" and called, (
        "with no provider configured the chat path produced nothing at all")
