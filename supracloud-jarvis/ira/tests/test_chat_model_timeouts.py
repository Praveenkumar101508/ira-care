"""P1-02: a slow model gets one request, and the caller gets a timely 504.

Found on a clean CPU-only Linux install on 31 October 2026 (PR #190): the
OpenAI SDK's default of two retries resent a non-streamed chat whose answer had
not arrived within OLLAMA_READ_TIMEOUT_S (600 s there), so the model wrote the
same answer up to three times, for up to 30 minutes. The web app's proxy had
given up at 15 and handed the caller an empty HTTP 500. When the attempts ran
out, the gateway turned the timeout into a refusal reading "IRA did not send
this anywhere", which was returned as the model's answer.

These tests run IRA's real client stack — `utils.llm`, the gateway, the
OpenAI-compatible provider and the SDK — against a stand-in model on
127.0.0.1 that holds every request open, and count what reaches it. Nothing
here measures speed: the time checks only tell a short configured limit from a
long one, with a wide margin for a busy machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/

#: Long enough that, if it were the limit that ended a request, the time checks
#: below would see it; short enough that a regression fails in seconds.
LONG_S = 30.0


class _HeldModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible model that never answers in time.

    Each generation request is recorded, then held open until the client goes
    away or the test releases it. `/api/tags` answers with no models, for the
    availability probe.
    """

    server: "_ModelServer"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        data = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.server.lock:
            self.server.requests.append(json.loads(body or b"{}"))
        sock = self.connection
        while not self.server.release.is_set():
            ready, _, _ = select.select([sock], [], [], 0.05)
            if not ready:
                continue
            try:
                peeked = sock.recv(1, socket.MSG_PEEK)
            except OSError:
                peeked = b""
            if not peeked:
                with self.server.lock:
                    self.server.disconnects += 1
                return


class _ModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HeldModel)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.disconnects = 0
        self.release = threading.Event()

    @property
    def generation_requests(self) -> int:
        with self.lock:
            return len(self.requests)

    def wait_for_disconnects(self, n: int, within_s: float = 10.0) -> int:
        end = time.monotonic() + within_s
        while time.monotonic() < end:
            with self.lock:
                if self.disconnects >= n:
                    return self.disconnects
            time.sleep(0.02)
        with self.lock:
            return self.disconnects


@pytest.fixture
def held_model(monkeypatch):
    """The stand-in model, with IRA's settings pointed at it."""
    from config import get_settings
    from core.models.composition import reset_gateway

    server = _ModelServer()
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
        "OLLAMA_READ_TIMEOUT_S": "0.5",
        "IRA_CHAT_TIMEOUT_S": str(LONG_S),
        # The client honours proxy settings in the environment; the stand-in
        # is on this machine.
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gateway()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        get_settings.cache_clear()
        reset_gateway()


def _configure(monkeypatch, **env: str) -> None:
    from config import get_settings
    from core.models.composition import reset_gateway

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gateway()


QUESTION = [{"role": "user", "content": "SYNTHETIC question for a model that is slow"}]


# ── (a) A read timeout is not followed by a second request ──────────────────


async def test_a_slow_answer_is_asked_for_once(held_model):
    from core.interfaces.model_provider import ProviderTimeout
    from utils import llm

    with pytest.raises(ProviderTimeout) as caught:
        await llm.chat_complete(QUESTION)

    assert held_model.generation_requests == 1, (
        f"the model received the same generation {held_model.generation_requests} "
        f"times; each retry makes it write the whole answer again")
    assert caught.value.limit_s == 0.5


async def test_a_slow_stream_is_opened_once(held_model):
    """The stream's opening request had the same retries: on a CPU, a long
    prompt can take longer than the read timeout to produce a first token."""
    from utils import llm

    with pytest.raises(Exception) as caught:
        async for _token in llm.stream_tokens(QUESTION):
            pass

    assert "timed out" in str(caught.value).lower()
    assert held_model.generation_requests == 1


async def test_the_direct_path_asks_once_and_raises_the_same_timeout(held_model):
    """The path used when no gateway can be built had its own three attempts."""
    from core.interfaces.model_provider import ProviderTimeout
    from utils import llm

    with pytest.raises(ProviderTimeout):
        await llm._complete_no_stream(llm._make_ollama_client(), "stand-in", QUESTION, 16, 0.1)

    assert held_model.generation_requests == 1


def test_every_generation_client_is_built_without_retries(held_model):
    from utils import llm

    assert llm._make_ollama_client().max_retries == 0
    assert llm._make_vllm_client("http://127.0.0.1:9/v1").max_retries == 0


async def test_the_stand_in_counts_a_retry_when_one_is_sent(held_model, monkeypatch):
    """The instrument check: with the SDK's default restored, the same stand-in
    sees the two extra requests. Without this, a stand-in that could not count
    them would make the tests above pass for the wrong reason."""
    from core.interfaces.model_provider import ProviderTimeout
    from utils import llm

    monkeypatch.setattr(llm, "GENERATION_MAX_RETRIES", 2)
    with pytest.raises(ProviderTimeout):
        await llm.chat_complete(QUESTION)

    assert held_model.generation_requests == 3


# ── (b) The chat route answers with the documented error, within its bound ──


def _chat_app(monkeypatch, graph):
    """POST /api/v1/chat with the real error contract, the I/O around the
    model stubbed and `run_graph` replaced by `graph`."""
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    import api.routes.chat as chatmod
    from api.middleware.workspace import require_workspace_principal
    from core.runtime.errors import install_error_handlers
    from tests.conftest import make_workspace_principal

    monkeypatch.setattr(chatmod, "cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "cache_set", AsyncMock())
    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv-synthetic"))
    monkeypatch.setattr(chatmod, "guard_chat_memory", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "_USE_CORTEX", False)
    monkeypatch.setattr(chatmod, "run_graph", graph)

    app = FastAPI()
    app.include_router(chatmod.router, prefix="/api/v1")
    install_error_handlers(app)
    principal = make_workspace_principal(is_owner=False)
    app.dependency_overrides[require_workspace_principal] = lambda: principal
    app.dependency_overrides[chatmod._workspace_username] = lambda: "synthetic-user"
    return app


async def _graph_asking_the_model(_principal, **kwargs):
    """A stand-in for the agent graph that asks the real model client once."""
    from utils import llm

    text = await llm.chat_complete(
        [{"role": "user", "content": kwargs["user_query"]}])
    return {"final_response": text, "active_agent": "conversational",
            "model_used": "qwen3-fast", "latency_ms": 1}


async def _post_chat(app) -> tuple[httpx.Response, float]:
    # The response a client receives, including the 500 the error contract
    # sends for an unexpected exception (which the transport would re-raise).
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://ira.test") as client:
        started = time.monotonic()
        response = await client.post(
            "/api/v1/chat", json={"message": "SYNTHETIC question", "session_id": "s-synthetic"})
        return response, time.monotonic() - started


async def test_a_turn_past_the_chat_deadline_answers_504_model_timeout(held_model, monkeypatch):
    """The model's own read timeout is long here; the turn's deadline is not.
    The route must answer at the deadline, say so in the documented shape, and
    close its connection to the model."""
    _configure(monkeypatch, OLLAMA_READ_TIMEOUT_S=str(LONG_S), IRA_CHAT_TIMEOUT_S="1")
    app = _chat_app(monkeypatch, _graph_asking_the_model)

    response, elapsed = await _post_chat(app)

    assert response.status_code == 504, response.text
    body = response.json()
    assert body["error_code"] == "model_timeout"
    assert body["message"] == "The model did not finish its answer in time. It was not asked again."
    assert body["retryable"] is False
    assert body["safe_details"] == {"limit_s": 1.0}
    assert elapsed < LONG_S / 2, (
        f"answered after {elapsed:.0f} s: the model's read timeout ended the turn, "
        f"not IRA_CHAT_TIMEOUT_S")
    assert held_model.generation_requests == 1
    assert held_model.wait_for_disconnects(1) == 1, (
        "the request to the model was left open after the turn was given up")


async def test_a_model_read_timeout_answers_504_with_its_limit(held_model, monkeypatch):
    """One call that runs out of time: before the fix, three requests and then
    HTTP 200 whose `response` was a refusal sentence."""
    _configure(monkeypatch, OLLAMA_READ_TIMEOUT_S="0.5", IRA_CHAT_TIMEOUT_S=str(LONG_S))
    app = _chat_app(monkeypatch, _graph_asking_the_model)

    response, elapsed = await _post_chat(app)

    assert response.status_code == 504, response.text
    body = response.json()
    assert body["error_code"] == "model_timeout"
    assert body["retryable"] is False
    assert body["safe_details"] == {"limit_s": 0.5}
    assert "did not send this anywhere" not in response.text
    assert elapsed < LONG_S / 2
    assert held_model.generation_requests == 1


async def test_an_answer_inside_the_deadline_is_unchanged(monkeypatch):
    """The deadline is a bound, not a behaviour change for a turn that fits."""
    _configure(monkeypatch, IRA_CHAT_TIMEOUT_S="5")

    async def quick_graph(_principal, **_kwargs):
        return {"final_response": "SYNTHETIC answer", "active_agent": "conversational",
                "model_used": "qwen3-fast", "latency_ms": 1}

    try:
        response, _elapsed = await _post_chat(_chat_app(monkeypatch, quick_graph))
    finally:
        from config import get_settings
        get_settings.cache_clear()

    assert response.status_code == 200, response.text
    assert response.json()["response"] == "SYNTHETIC answer"


async def test_a_timeout_that_is_not_the_deadline_is_not_reported_as_one(monkeypatch):
    """Something else timing out inside the turn (here a stand-in for a
    database wait) is not dressed up as the chat deadline."""
    _configure(monkeypatch, IRA_CHAT_TIMEOUT_S=str(LONG_S))

    async def graph_with_a_database_timeout(_principal, **_kwargs):
        raise TimeoutError("SYNTHETIC pool wait")

    try:
        response, _elapsed = await _post_chat(_chat_app(monkeypatch, graph_with_a_database_timeout))
    finally:
        from config import get_settings
        get_settings.cache_clear()

    assert response.status_code == 500, response.text
    assert response.json()["error_code"] == "internal_error"


# ── The gateway: a timeout is raised, metered and its slot handed back ─────


class _Principal:
    tenant_id = "00000000-0000-4000-8000-0000000000a1"
    user_id = "00000000-0000-4000-8000-0000000000a2"
    username = "synthetic-user"


class _LocalProvider:
    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour

    @property
    def provider_id(self) -> str:
        return "ollama-local"

    def disclosure(self):
        from core.interfaces.model_provider import Locality, ProviderDisclosure
        return ProviderDisclosure(provider_id="ollama-local", display_name="Local",
                                  locality=Locality.LOCAL, retains_prompts=False)

    def supports(self, tier) -> bool:
        return True

    async def complete(self, request):
        return await self._behaviour()

    async def stream(self, request):  # pragma: no cover - not used here
        yield ""


def _gateway(behaviour):
    from core.models.gateway import IRAModelGateway

    metered: list[dict] = []
    released: list[object] = []
    gw = IRAModelGateway()
    gw.register(_LocalProvider(behaviour))
    gw.costs = {"ollama-local": {}}
    gw.quota_reserver = lambda _p: True
    gw.quota_releaser = released.append
    gw.usage_writer = lambda _p, row: metered.append(row)
    return gw, metered, released


async def test_the_gateway_raises_a_provider_timeout_rather_than_refusing():
    from core.interfaces.model_provider import CompletionRequest, ProviderTimeout

    async def times_out():
        raise ProviderTimeout("ollama-local", 0.5)

    gw, metered, released = _gateway(times_out)
    with pytest.raises(ProviderTimeout):
        await gw.complete(CompletionRequest(messages=QUESTION), principal=_Principal(),
                          privacy_mode="local_only")

    assert [row["outcome"] for row in metered] == ["error"]
    assert len(released) == 1, "a timed-out call must hand its quota slot back"


async def test_the_gateway_still_refuses_on_any_other_provider_failure():
    from core.interfaces.model_provider import CompletionRequest

    async def explodes():
        raise RuntimeError("SYNTHETIC failure")

    gw, metered, released = _gateway(explodes)
    outcome = await gw.complete(CompletionRequest(messages=QUESTION), principal=_Principal(),
                                privacy_mode="local_only")

    assert outcome.ok is False
    assert [row["outcome"] for row in metered] == ["error"]
    assert len(released) == 1


async def test_a_cancelled_call_is_metered_as_abandoned_and_its_slot_returned():
    """The chat deadline cancels the turn while the provider has the request.
    That call happened and is metered, as a stream nobody finished reading is."""
    from core.interfaces.model_provider import CompletionRequest

    started = asyncio.Event()

    async def never_answers():
        started.set()
        await asyncio.Event().wait()

    gw, metered, released = _gateway(never_answers)
    task = asyncio.ensure_future(gw.complete(
        CompletionRequest(messages=QUESTION), principal=_Principal(), privacy_mode="local_only"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [row["outcome"] for row in metered] == ["abandoned"]
    assert len(released) == 1


# ── The numbers agree with each other and with the proxy ────────────────────



def test_env_example_documents_both_limits_at_their_code_defaults():
    from config import Settings

    text = (APP_ROOT / ".env.example").read_text(encoding="utf-8")
    values = dict(re.findall(r"(?m)^(OLLAMA_READ_TIMEOUT_S|IRA_CHAT_TIMEOUT_S)=(\S+)", text))
    assert float(values["OLLAMA_READ_TIMEOUT_S"]) == Settings.model_fields["ollama_read_timeout_s"].default
    assert float(values["IRA_CHAT_TIMEOUT_S"]) == Settings.model_fields["ira_chat_timeout_s"].default
