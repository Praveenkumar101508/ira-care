"""Prompts 3.1 & 3.3 — local vision-language model path + /chat/vision wiring.

The VL model call is mocked (no Ollama / no pulled model needed). Covers: fail-soft
when no vision model is configured, fail-soft on a backend error, a successful
completion/stream, and that /chat/vision streams through the helper.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import utils.llm as llm

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object


async def _collect(agen):
    return [t async for t in agen]


def test_vision_complete_failsoft_when_unconfigured(monkeypatch):
    monkeypatch.setattr(llm, "_vision_client_and_model", lambda: (None, None))
    out = asyncio.run(llm.vision_complete(prompt="describe", image_b64="x"))
    assert "unavailable" in out.lower()


def test_vision_complete_success(monkeypatch):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="A red apple"))])))))
    monkeypatch.setattr(llm, "_vision_client_and_model", lambda: (client, "qwen2.5vl"))
    out = asyncio.run(llm.vision_complete(prompt="what is it?", image_b64="x"))
    assert out == "A red apple"


def test_vision_complete_failsoft_on_error(monkeypatch):
    """Fail soft when the backend errors — the property, not the wording.

    ── Changed in Phase 8, and the reason matters more than the change ─────

    This asserted `out.startswith("Vision unavailable")`, which was the string
    `vision_complete` produced from its own `except` block. Phase 8 routed the
    vision path through the gateway, so a provider that raises is now caught by
    `gateway.complete()` and returned as a routing refusal instead.

    The behaviour this test exists for is unchanged and is asserted below: no
    exception escapes into a route, something readable comes back, and the raw
    backend error is not handed to a user. What changed is which layer produced
    the sentence, and pinning the sentence would have meant either keeping an
    ungoverned code path alive to satisfy a string comparison, or asserting
    nothing at all.
    """
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=RuntimeError("model not pulled")))))
    monkeypatch.setattr(llm, "_vision_client_and_model", lambda: (client, "qwen2.5vl"))
    out = asyncio.run(llm.vision_complete(prompt="?", image_b64="x"))

    assert isinstance(out, str) and out.strip(), (
        "the vision path stopped failing soft: a backend error must produce a "
        "message, not an exception into a streaming route")
    assert "no answer was produced" in out or out.startswith("Vision unavailable"), (
        f"the failure message no longer tells the user nothing was answered: "
        f"{out!r}")
    assert "model not pulled" not in out, (
        "the raw backend exception reached the user")


def test_stream_vision_failsoft_when_unconfigured(monkeypatch):
    monkeypatch.setattr(llm, "_vision_client_and_model", lambda: (None, None))
    toks = asyncio.run(_collect(llm.stream_vision_tokens(prompt="p", image_b64="x")))
    assert any("unavailable" in t.lower() for t in toks)


def test_stream_vision_success(monkeypatch):
    async def _chunks():
        for c in ["Hel", "lo"]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=c))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=_chunks()))))
    monkeypatch.setattr(llm, "_vision_client_and_model", lambda: (client, "m"))
    toks = asyncio.run(_collect(llm.stream_vision_tokens(prompt="p", image_b64="x")))
    assert "".join(toks) == "Hello"


def test_chat_vision_route_streams_via_helper(monkeypatch):
    import api.routes.chat as chatmod
    from api.routes.chat import chat_vision, VisionRequest

    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv1"))
    monkeypatch.setattr("memory.store.save_message", AsyncMock())

    async def _fake_stream(**kwargs):
        for t in ["It ", "is ", "a cat."]:
            yield t
    monkeypatch.setattr("utils.llm.stream_vision_tokens", _fake_stream)

    async def _run():
        resp = await chat_vision(
            VisionRequest(message="what is this?", image_b64="abc", mime_type="image/png"),
            _user="owner",
        )
        chunks = []
        async for ev in resp.body_iterator:
            chunks.append(ev.decode() if isinstance(ev, (bytes, bytearray)) else str(ev))
        return "".join(chunks)

    blob = asyncio.run(_run())
    assert "a cat." in blob
    assert '"agent": "vision"' in blob or "vision" in blob
