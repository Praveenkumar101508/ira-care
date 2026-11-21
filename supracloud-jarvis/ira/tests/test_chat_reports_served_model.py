"""P1-05: the chat names the model that answered, not the tier it asked for.

Found on a clean CPU-only Linux install on 31 October 2026 (PR #190): the chat
response's `model_used` and the stream done-frame's `model` said `qwen3-deep`
while Ollama served `qwen3:4b`. Both were tier names, written by the agent or
the route without asking what answered, and the Agent activity panel showed
them to the owner as "Model". Constitution rule D4: displayed state reflects
what the backend reported.

The route tests run IRA's real model stack — `utils.llm`, the gateway, the
OpenAI-compatible provider and the SDK — and the real agent graph against a
stand-in model on 127.0.0.1 that answers as `qwen3:4b`. Only database and web
I/O is stubbed.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import httpx
import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from core.models import served as served_models  # noqa: E402
from core.models.served import NOT_REPORTED, Served  # noqa: E402

#: What the 31 October install served, and what the stand-in answers as.
SERVED = "qwen3:4b"
#: A question the keyword router sends to the researcher, as on 31 October.
QUESTION = "SYNTHETIC: what is the capital of France?"
ANSWER = "SYNTHETIC: The capital of France is Paris."


# ── A stand-in model on this machine ─────────────────────────────────────────


class _StandIn(BaseHTTPRequestHandler):
    """An OpenAI-compatible model that names `server.served_as` in every answer."""

    server: "_ModelServer"

    def log_message(self, *args) -> None:
        pass

    def _json(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": [{"name": SERVED, "model": SERVED}]})

    def do_POST(self) -> None:  # noqa: N802
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/show":
            # Model metadata, not a generation (`reasoning/model_traits.py`). The
            # stand-in answers at once, as a model that does not reason first.
            self._json({"capabilities": ["completion"], "model_info": {}})
            return
        with self.server.lock:
            self.server.requests.append({"model": req.get("model"), "stream": bool(req.get("stream"))})
        name = self.server.served_as
        if not req.get("stream"):
            self._json({
                "id": "synthetic", "object": "chat.completion", "created": 0, "model": name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for word in ANSWER.split(" "):
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 0, "model": name,
                     "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _ModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StandIn)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.served_as = SERVED


@pytest.fixture
def stand_in(monkeypatch):
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
        "OLLAMA_MODEL_FAST": SERVED, "OLLAMA_MODEL_DEEP": SERVED, "OLLAMA_MODEL_REASONING": SERVED,
        "IRA_USE_MODEL_ROUTER": "false",
        # The client honours proxy settings in the environment; the stand-in is on this machine.
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gateway()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        get_settings.cache_clear()
        reset_gateway()


def _chat_app(monkeypatch, *, run_graph=None):
    """The chat router with database and web I/O stubbed; the model stack is real.

    `run_graph` replaces the agent graph when given. Otherwise the real graph
    runs, compiled afresh with its in-memory checkpointer.
    """
    from fastapi import FastAPI
    from sse_starlette.sse import AppStatus

    import agents.graph as graph
    import api.routes.chat as chatmod
    from api.middleware.workspace import require_workspace_principal
    from tests.conftest import make_workspace_principal

    # sse-starlette keeps one exit event for the process, bound to the event
    # loop that first waited on it; each test has its own loop. It creates the
    # event again when it is None.
    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    monkeypatch.setattr(chatmod, "cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "cache_set", AsyncMock())
    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv-synthetic"))
    monkeypatch.setattr(chatmod, "guard_chat_memory", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "retrieve", AsyncMock(return_value=[]))
    monkeypatch.setattr(chatmod, "get_recent_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(chatmod, "get_profile_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(chatmod, "_USE_CORTEX", False)
    monkeypatch.setattr(graph, "retrieve", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph, "save_message", AsyncMock())
    monkeypatch.setattr(graph, "_compiled_graph", None)
    monkeypatch.setattr("owner_profile.get_profile_summary", AsyncMock(return_value=""))
    monkeypatch.setattr("memory.relationships.get_people_summary", AsyncMock(return_value=""))
    monkeypatch.setattr("memory.store.save_message", AsyncMock())
    monkeypatch.setattr("utils.search_tools.get_search_context", AsyncMock(return_value=("", {})))
    monkeypatch.setattr("api.routes._dispatch.dispatch", AsyncMock(return_value=None))
    monkeypatch.setattr("cognition.ingest.feed_cognitive_percept", AsyncMock())
    if run_graph is not None:
        monkeypatch.setattr(chatmod, "run_graph", run_graph)

    app = FastAPI()
    app.include_router(chatmod.router, prefix="/api/v1")
    principal = make_workspace_principal(is_owner=True)
    app.dependency_overrides[require_workspace_principal] = lambda: principal
    app.dependency_overrides[chatmod._workspace_username] = lambda: "synthetic-user"
    return app


async def _post_chat(app, message: str = QUESTION) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ira.test", timeout=60) as client:
        response = await client.post(
            "/api/v1/chat", json={"message": message, "session_id": f"s-{uuid.uuid4()}"})
    assert response.status_code == 200, response.text
    return response.json()


async def _stream_done_frame(app, message: str = QUESTION) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ira.test", timeout=60) as client:
        response = await client.post("/api/v1/chat/stream", json={
            "message": message, "session_id": f"s-{uuid.uuid4()}"})
    assert response.status_code == 200, response.text
    frames = [json.loads(line[5:].strip()) for line in response.text.splitlines()
              if line.startswith("data:")]
    assert not [f for f in frames if "error" in f], frames
    done = [f for f in frames if f.get("done")]
    assert len(done) == 1, f"expected one done frame, got {frames}"
    return done[0]


# ── The finding, through both routes ─────────────────────────────────────────


async def test_the_chat_response_names_the_model_that_answered(stand_in, monkeypatch):
    body = await _post_chat(_chat_app(monkeypatch))

    assert body["agent_used"] == "researcher"
    assert body["model_used"] == SERVED, (
        f"the response reported {body['model_used']!r}; the model that answered was {SERVED!r}")
    assert body["model_tier"] == "deep"
    assert [r["stream"] for r in stand_in.requests] == [False]


async def test_the_stream_done_frame_names_the_model_that_answered(stand_in, monkeypatch):
    done = await _stream_done_frame(_chat_app(monkeypatch))

    assert done["agent"] == "researcher"
    assert done["model"] == SERVED, (
        f"the done-frame reported {done['model']!r}; the model that answered was {SERVED!r}")
    assert done["model_tier"] == "deep"
    assert [r["stream"] for r in stand_in.requests] == [True]


async def test_the_name_reported_is_the_servers_not_the_configured_one(stand_in, monkeypatch):
    """IRA asks for `qwen3:4b`; the server says it ran something else. The
    readout repeats the server, which is the only party that knows."""
    stand_in.served_as = "qwen3:4b-q4_K_M"
    app = _chat_app(monkeypatch)

    body = await _post_chat(app)
    done = await _stream_done_frame(app)

    assert {r["model"] for r in stand_in.requests} == {SERVED}
    assert body["model_used"] == "qwen3:4b-q4_K_M"
    assert done["model"] == "qwen3:4b-q4_K_M"


# ── Unknown is said, not filled in ───────────────────────────────────────────


async def test_a_turn_no_provider_reported_says_not_reported(monkeypatch):
    """The agents still write a tier name into `model_used`. It must not reach
    the response as the model."""
    async def graph_without_a_report(_principal, **_kwargs):
        return {"final_response": "SYNTHETIC answer", "active_agent": "researcher",
                "model_used": "qwen3-deep", "latency_ms": 1}

    body = await _post_chat(_chat_app(monkeypatch, run_graph=graph_without_a_report))

    assert body["model_used"] == NOT_REPORTED
    assert body["model_tier"] is None


async def test_a_step_that_answered_without_a_model_keeps_its_name(monkeypatch):
    async def gate_refusal(_principal, **_kwargs):
        return {"final_response": "SYNTHETIC refusal", "active_agent": "access_denied",
                "model_used": "security_gate", "latency_ms": 0}

    body = await _post_chat(_chat_app(monkeypatch, run_graph=gate_refusal))

    assert body["model_used"] == "security_gate"
    assert body["model_tier"] is None


async def test_a_stream_no_provider_reported_says_not_reported(monkeypatch):
    import api.routes.chat as chatmod

    async def tokens_without_a_report(messages, use_deep=False, use_reasoning=False):
        for token in ("SYNTHETIC", " answer"):
            yield token

    app = _chat_app(monkeypatch)
    monkeypatch.setattr("agents.supervisor.classify", AsyncMock(
        return_value={"active_agent": "conversational", "use_deep_model": True}))
    monkeypatch.setattr(chatmod, "stream_tokens", tokens_without_a_report)

    done = await _stream_done_frame(app, "SYNTHETIC hello there")

    assert done["model"] == NOT_REPORTED
    assert done["model_tier"] == "deep"        # what IRA asked for, labelled as the tier


@pytest.mark.parametrize("served_model, expected", [(SERVED, SERVED), (None, NOT_REPORTED)])
async def test_a_tool_agent_turn_reports_the_graphs_answer(monkeypatch, served_model, expected):
    """Career, digital and browser turns run the graph and then word-stream it."""
    async def tool_graph(_principal, **_kwargs):
        return {"final_response": "SYNTHETIC result", "active_agent": "career",
                "model_used": "qwen3-deep", "latency_ms": 1,
                "served_model": served_model, "served_tier": "deep" if served_model else None}

    app = _chat_app(monkeypatch, run_graph=tool_graph)
    monkeypatch.setattr("agents.supervisor.classify", AsyncMock(
        return_value={"active_agent": "career", "use_deep_model": True}))

    done = await _stream_done_frame(app, "SYNTHETIC tailor my resume")

    assert done["agent"] == "career"
    assert done["model"] == expected


# ── What is recorded, and what is not ───────────────────────────────────────


async def test_the_routing_call_is_not_reported_as_the_answer(stand_in):
    """The supervisor asks the model which agent should answer. If the gate then
    refuses, no model answered, so the routing call must not be recorded."""
    from agents.graph import make_initial_state
    from agents.supervisor import classify
    from utils.yaml_config import get_agent_rules

    query = ("SYNTHETIC please give me one short sentence about the colour of the sky "
             "on a calm and quiet day near the sea")
    assert len(query.split()) > 15
    assert not any(kw in query.lower() for kws, _agent in get_agent_rules() for kw in kws), (
        "the query matches a routing keyword, so the model would not be asked")

    state = make_initial_state(session_id="s", conversation_id="c", user_query=query,
                               user_id="synthetic-user")
    with served_models.recording() as served:
        await classify(state)

    assert len(stand_in.requests) == 1, "the classifier did not ask the model"
    assert served.calls == []


async def test_a_completion_is_recorded_with_its_tier_and_a_refusal_is_not(monkeypatch):
    """`utils.llm.chat_complete` through the gateway, with a provider that
    reports `qwen3:8b`. A refusal is text no model wrote."""
    from core.interfaces.model_provider import CompletionResult, Locality, ProviderDisclosure
    from core.models import composition
    from core.models.gateway import IRAModelGateway
    import utils.llm as llm

    class Provider:
        def __init__(self, locality):
            self._locality = locality

        @property
        def provider_id(self):
            return "synthetic"

        def disclosure(self):
            return ProviderDisclosure(provider_id="synthetic", display_name="synthetic",
                                      locality=self._locality, retains_prompts=False,
                                      region="", trains_on_data=False)

        def supports(self, tier):
            return True

        async def complete(self, request):
            return CompletionResult(text="SYNTHETIC answer", model="qwen3:8b", provider_id="synthetic",
                                    disclosure=self.disclosure())

    def wire(locality):
        def build(**_kw):
            gw = IRAModelGateway(costs={"synthetic": {}})
            gw.register(Provider(locality))
            gw.usage_writer = lambda _p, _row: None
            gw.registry_required = False
            return gw
        monkeypatch.setattr(composition, "gateway", build)

    monkeypatch.setattr(llm, "_privacy_mode", lambda: "local_only")
    monkeypatch.setattr(llm, "_principal_or_none", AsyncMock(return_value=None))
    try:
        wire(Locality.LOCAL)
        with served_models.recording() as served:
            assert await llm.chat_complete([{"role": "user", "content": "hi"}], use_deep=True) == \
                "SYNTHETIC answer"
        assert served.calls == [Served(model="qwen3:8b", tier="deep")]

        wire(Locality.EXTERNAL)          # local_only refuses an external provider
        with served_models.recording() as served:
            refusal = await llm.chat_complete([{"role": "user", "content": "hi"}])
        assert refusal != "SYNTHETIC answer"
        assert served.calls == []
    finally:
        composition.reset_gateway()


async def test_the_provider_stream_records_what_the_server_named():
    from core.interfaces.model_provider import CompletionRequest, Tier
    from tests.models.test_openai_compatible_provider import FakeEndpoint, build

    provider, _endpoint = build(endpoint=FakeEndpoint(served_model="qwen3:8b-served"))
    with served_models.recording() as served:
        tokens = [t async for t in provider.stream(CompletionRequest(
            messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST))]

    assert "".join(tokens) == "an answer"
    assert served.calls == [Served(model="qwen3:8b-served", tier="fast")]


async def test_a_stream_from_a_substituting_backend_records_nothing():
    """The identity check refuses the stream at its first chunk; nothing it
    produced is shown, so nothing is reported as having answered."""
    from core.interfaces.model_provider import CompletionRequest, Tier
    from core.models.providers import UnsupportedApprovedModel
    from tests.models.test_openai_compatible_provider import FakeEndpoint, _approved, build

    provider, _ = build(endpoint=FakeEndpoint(served_model="qwen3:0.5b"))
    with served_models.recording() as served:
        with pytest.raises(UnsupportedApprovedModel):
            [t async for t in provider.stream(CompletionRequest(
                messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
                approved_model=_approved()))]
    assert served.calls == []


def test_nothing_is_recorded_outside_a_recording():
    served_models.note_served("qwen3:4b", "fast")      # no recording open: a no-op

    with served_models.recording() as served:
        served_models.note_served("", "fast")           # no name: nothing to report
        served_models.note_served(None, "fast")
    assert served.calls == []


def test_an_abandoned_stream_can_end_its_recording_from_another_context():
    """sse-starlette may close an abandoned stream's generator from another
    context, where the token cannot be reset. That must not raise."""
    served, token = served_models.begin()
    other = contextvars.Context()
    other.run(served_models.end, token)                 # would raise ValueError unguarded
    served_models.end(token)                            # and the real end still works
    served_models.note_served("qwen3:4b")
    assert served.calls == []
