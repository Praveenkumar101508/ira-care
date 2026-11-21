"""P0 (performance), ranked item 3, end to end: a reasoning model that runs
past its context no longer loses the question.

Traced on a clean install on 1 November 2026 (`qwen3:4b`, which is Qwen3 4B
Thinking 2507, on Ollama with a 4,096-token context): a greeting reasoned past
the context and Ollama's llama-server shifted it, logging
`slot context shift, n_keep = 4, n_left = 4091, n_discard = 2045`. The first
four tokens were kept and the next 2,045 discarded: the system prompt, the
owner profile and the question. The model went on generating without them,
and nothing told IRA or the user.

The stand-in below is an OpenAI-compatible server that does what llama-server
does: a fixed context, hidden reasoning in `delta.reasoning` before any
visible text, and the same context shift when generation fills the window.
Its answer says whether the question was still in its context when it wrote
it. These tests run IRA's real client stack against it: `utils.llm`, the
gateway, the OpenAI-compatible provider and the OpenAI SDK. They count tokens
and shifts; nothing here measures time.
"""
from __future__ import annotations

import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

#: The stand-in's context window, and IRA's setting for it.
N_CTX = 512
#: llama-server's `--keep 4`, as Ollama starts it.
N_KEEP = 4
ANSWERED = "Answer written with the question in context."
LOST = "Answer written WITHOUT the question: it was discarded."

PERSONA = "SYNTHETIC persona text that makes the system prompt realistic. " * 13
QUESTION = "SYNTHETIC: good morning! Reply with one short friendly sentence."
MESSAGES = [{"role": "system", "content": PERSONA},
            {"role": "user", "content": QUESTION}]


def _tokens(text: str) -> int:
    """The stand-in's tokenizer: four characters a token, as the real server
    counted English prompts on 1 November (4.4 to 4.6 characters a token)."""
    return max(1, math.ceil(len(text) / 4))


class _LlamaLike(BaseHTTPRequestHandler):
    """A thinking model behind an OpenAI-compatible route, with a context."""

    server: "_StandIn"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": []})

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        record = self.server.generate(body)
        if not body.get("stream"):
            self._json({
                "id": "chatcmpl-standin", "object": "chat.completion", "created": 0,
                "model": body.get("model", "stand-in"),
                "choices": [{"index": 0, "finish_reason": record["finish_reason"],
                             "message": {"role": "assistant",
                                         "content": "".join(record["visible"]),
                                         "reasoning": "".join(record["hidden"])}}],
                "usage": {"prompt_tokens": record["prompt_tokens"],
                          "completion_tokens": record["generated"],
                          "total_tokens": record["prompt_tokens"] + record["generated"]},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(delta: dict, finish=None) -> None:
            chunk = {"id": "chatcmpl-standin", "object": "chat.completion.chunk",
                     "created": 0, "model": body.get("model", "stand-in"),
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

        for token in record["hidden"]:
            send({"role": "assistant", "content": "", "reasoning": token})
        for token in record["visible"]:
            send({"role": "assistant", "content": token})
        send({}, record["finish_reason"])
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _StandIn(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, reasoning_tokens: int) -> None:
        super().__init__(("127.0.0.1", 0), _LlamaLike)
        self.lock = threading.Lock()
        self.reasoning_tokens = reasoning_tokens
        self.records: list[dict] = []

    def generate(self, body: dict) -> dict:
        """Run the model's token loop the way llama-server does, shifts and all."""
        messages = body.get("messages") or []
        window: list = []
        for index, message in enumerate(messages):
            window += [index] * (_tokens(str(message.get("content") or "")) + 4)
        window += ["header"] * 3
        question = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        question_tokens = window.count(question)
        prompt_tokens = len(window)

        limit = body.get("max_tokens")
        hidden: list[str] = []
        visible: list[str] = []
        shifts: list[int] = []
        answer: list[str] = []
        finish = "stop"
        generated = 0
        while True:
            if generated < self.reasoning_tokens:
                token, into = "think ", hidden
            else:
                if not answer and not visible:
                    # The answer is written from what is in context NOW.
                    kept = window.count(question) == question_tokens
                    answer = [w + " " for w in (ANSWERED if kept else LOST).split()]
                    answer[-1] = answer[-1].rstrip()
                if not answer:
                    break
                token, into = answer.pop(0), visible
            if limit is not None and generated >= limit:
                finish = "length"
                break
            if len(window) >= N_CTX:
                n_left = len(window) - N_KEEP
                n_discard = n_left // 2
                del window[N_KEEP:N_KEEP + n_discard]
                shifts.append(n_discard)
            window.append("generated")
            into.append(token)
            generated += 1

        record = {"max_tokens": limit, "prompt_tokens": prompt_tokens,
                  "generated": generated, "shifts": shifts,
                  "question_in_context": window.count(question) == question_tokens,
                  "finish_reason": finish, "hidden": hidden, "visible": visible}
        with self.lock:
            self.records.append(record)
        return record


def _serve(monkeypatch, reasoning_tokens: int) -> _StandIn:
    from config import get_settings
    from core.models.composition import reset_gateway

    server = _StandIn(reasoning_tokens)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    for key, value in {
        "LLM_BACKEND": "ollama",
        "DEV_MODE": "false",
        "IRA_EDITION": "private",
        "OLLAMA_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "OLLAMA_MODEL_FAST": "stand-in", "OLLAMA_MODEL_DEEP": "stand-in",
        "OLLAMA_MODEL_REASONING": "stand-in",
        "IRA_USE_MODEL_ROUTER": "false",
        "OLLAMA_CONTEXT_LENGTH": str(N_CTX),
        "OLLAMA_READ_TIMEOUT_S": "30",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gateway()
    return server


@pytest.fixture
def stand_in(monkeypatch, request):
    """`request.param` is how many tokens the model reasons before answering."""
    from config import get_settings
    from core.models.composition import reset_gateway

    server = _serve(monkeypatch, getattr(request, "param", 2000))
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        get_settings.cache_clear()
        reset_gateway()


def _assert_bounded(record: dict) -> None:
    assert record["shifts"] == [], (
        f"the model generated past its {N_CTX}-token context: llama-server "
        f"shifted it {len(record['shifts'])} time(s), discarding "
        f"{record['shifts']} tokens, and the question went with them")
    assert record["question_in_context"]
    assert record["max_tokens"] is not None
    assert record["prompt_tokens"] + record["max_tokens"] <= N_CTX


# ── The instrument: the stand-in reproduces the defect when asked to ────────


def test_the_stand_in_discards_the_question_when_asked_for_more_than_fits(stand_in):
    """Main sent the fast tier's 4,096 whatever the context. Sent straight to
    the stand-in, that request shifts the context and the answer is written
    without the question. Without this check, a stand-in that could not
    overflow would make the tests below pass for the wrong reason."""
    port = stand_in.server_address[1]
    response = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", json={
        "model": "stand-in", "messages": MESSAGES, "max_tokens": 4096,
        "stream": False})
    record = stand_in.records[0]
    assert record["shifts"], "the stand-in did not overflow"
    assert record["shifts"][0] == (N_CTX - N_KEEP) // 2
    assert not record["question_in_context"]
    assert response.json()["choices"][0]["message"]["content"] == LOST
    assert response.json()["choices"][0]["finish_reason"] == "stop"


# ── IRA's requests stay inside the context ──────────────────────────────────


@pytest.mark.parametrize("tier", ["fast", "deep", "reasoning"])
async def test_a_long_reasoning_stream_stays_inside_the_context(stand_in, tier):
    from utils import llm

    got = [t async for t in llm.stream_tokens(
        MESSAGES, use_deep=tier == "deep", use_reasoning=tier == "reasoning")]
    text = "".join(got)

    assert len(stand_in.records) == 1
    record = stand_in.records[0]
    _assert_bounded(record)
    assert record["finish_reason"] == "length"
    assert LOST not in text
    assert "ran out of its answer budget" in text, (
        f"the model stopped at its limit and the user was shown {text!r}")


async def test_a_long_reasoning_completion_stays_inside_the_context(stand_in):
    from utils import llm

    text = await llm.chat_complete(MESSAGES)
    record = stand_in.records[0]
    _assert_bounded(record)
    assert text.startswith("I could not answer this time")
    assert LOST not in text


async def test_the_direct_path_stays_inside_the_context_too(stand_in, monkeypatch):
    """The path taken when no gateway can be built makes the same request."""
    from utils import llm

    monkeypatch.setattr(llm, "_gateway_and_request", lambda *a, **k: (None, None))
    text = "".join([t async for t in llm.stream_tokens(MESSAGES)])
    _assert_bounded(stand_in.records[0])
    assert "ran out of its answer budget" in text

    text = await llm.chat_complete(MESSAGES)
    _assert_bounded(stand_in.records[1])
    assert text.startswith("I could not answer this time")


# ── A model that answers within its budget is untouched ─────────────────────


@pytest.mark.parametrize("stand_in", [20], indirect=True)
async def test_an_answer_that_fits_is_streamed_unchanged(stand_in):
    from utils import llm

    text = "".join([t async for t in llm.stream_tokens(MESSAGES)])
    record = stand_in.records[0]
    assert record["finish_reason"] == "stop" and record["shifts"] == []
    assert text == ANSWERED
