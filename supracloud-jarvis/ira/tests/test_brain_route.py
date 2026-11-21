"""Tests for api/routes/brain.py — the realtime-brain WebSocket + lifecycle.

Covers the feature flag, the source-clamping security rule (a client can never
inject a trusted source), the lifespan start/stop of the persistent loop, and
the WebSocket itself (auth, disabled state, percept → thought streaming).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from api.routes.brain import _safe_source, brain_enabled, start_brain, stop_brain


# ── Feature flag + source clamping ──────────────────────────────────────────

def test_brain_disabled_by_default(monkeypatch):
    monkeypatch.delenv("IRA_BRAIN_ENABLED", raising=False)
    assert brain_enabled() is False


def test_brain_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("IRA_BRAIN_ENABLED", "true")
    assert brain_enabled() is True
    monkeypatch.setenv("IRA_BRAIN_ENABLED", "0")
    assert brain_enabled() is False


def test_safe_source_never_allows_trusted_or_unknown():
    # the security-critical cases: a client may not claim a trusted source
    assert _safe_source("internal") == "user"
    assert _safe_source("timer") == "user"
    assert _safe_source("anything-else") == "user"
    assert _safe_source(None) == "user"
    # legitimate external sources pass through (case-insensitive)
    assert _safe_source("voice") == "voice"
    assert _safe_source("VISION") == "vision"
    assert _safe_source("channel") == "channel"


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def test_start_brain_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("IRA_BRAIN_ENABLED", raising=False)
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    await start_brain(app)
    assert getattr(app.state, "brain", None) is None


async def test_start_and_stop_brain_when_enabled(monkeypatch):
    monkeypatch.setenv("IRA_BRAIN_ENABLED", "true")
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    await start_brain(app)
    assert app.state.brain is not None
    assert app.state.brain_task is not None and not app.state.brain_task.done()
    await asyncio.sleep(0.05)  # let the tick loop actually start
    assert app.state.brain.running is True
    await stop_brain(app)
    assert app.state.brain is None


# ── feed_percept seam (voice / vision sources) ───────────────────────────────

async def test_feed_percept_noop_when_no_brain():
    from api.routes.brain import feed_percept

    app = types.SimpleNamespace(state=types.SimpleNamespace(brain=None))
    assert await feed_percept(app, "voice", "hello") is False


async def test_feed_percept_delivers_and_clamps_source():
    from api.routes.brain import feed_percept

    brain = _FakeBrain()
    app = types.SimpleNamespace(state=types.SimpleNamespace(brain=brain))
    assert await feed_percept(app, "voice", "watch the gpu") is True
    assert brain.perceived == [("voice", "watch the gpu")]
    # a non-external source from a subsystem is still clamped
    await feed_percept(app, "internal", "sneaky")
    assert brain.perceived[-1] == ("user", "sneaky")


async def test_feed_percept_ignores_empty_text():
    from api.routes.brain import feed_percept

    brain = _FakeBrain()
    app = types.SimpleNamespace(state=types.SimpleNamespace(brain=brain))
    assert await feed_percept(app, "voice", "   ") is False
    assert brain.perceived == []


# ── WebSocket ────────────────────────────────────────────────────────────────

class _FakeBrain:
    """Minimal stand-in: records percepts, emits a thought when perceiving."""

    def __init__(self):
        self.thought_cbs: list = []
        self.speak_cbs: list = []
        self.perceived: list[tuple[str, str]] = []

    def on_thought(self, cb):
        self.thought_cbs.append(cb)

    def on_speak(self, cb):
        self.speak_cbs.append(cb)

    def off_thought(self, cb):
        if cb in self.thought_cbs:
            self.thought_cbs.remove(cb)

    def off_speak(self, cb):
        if cb in self.speak_cbs:
            self.speak_cbs.remove(cb)

    async def perceive(self, source: str, text: str):
        self.perceived.append((source, text))
        for cb in list(self.thought_cbs):
            cb(f"thinking about: {text}")


def _app_with_brain(monkeypatch, brain, *, username="owner"):
    # stub auth so the test needs no JWT/config (handler imports it lazily)
    fake_auth = types.ModuleType("api.middleware.auth")
    # `sub` is read with getattr, matching the real TokenPayload, so the stub
    # returns an object rather than the dict it used to — a dict silently gave
    # `None` and the socket authorised an empty username.
    fake_auth.decode_token = lambda _t: types.SimpleNamespace(sub=username)

    # P2-06: the socket authorises through the shared gate rather than
    # `decode_token`, and starts a watchdog that re-checks the credential.
    #
    # The gate is stubbed to REFUSE an empty subject and to refuse a refresh
    # token, because those are two of the things the real one exists to do —
    # a stub that said yes to everything would make
    # `test_ws_rejects_a_token_with_no_subject` pass against a socket with no
    # gate at all, which is the shape that has already caught us twice.
    async def _authorise(_t):
        if not username:
            raise ValueError("no subject")
        return types.SimpleNamespace(sub=username, tok="access", ver=0, jti="j")

    fake_auth.authorise_socket_token = _authorise

    # The watchdog is a real coroutine in production. Here it must never return,
    # because `gather`/`create_task` treats its return as "the credential died"
    # — a stub that returned immediately would close every socket under test.
    async def _watch(_ws, _payload, interval=None):
        await asyncio.Event().wait()

    fake_auth.watch_socket_credential = _watch

    # 3C-05: the socket now resolves a principal and requires owner authority.
    # Stubbed faithfully — `is_owner` answers for one name, not for everyone,
    # so `test_ws_rejects_a_non_owner` below can actually fail.
    fake_auth.is_owner = lambda u=None: u == "owner"
    monkeypatch.setitem(sys.modules, "api.middleware.auth", fake_auth)

    fake_ws = types.ModuleType("api.middleware.workspace")

    async def _principal_from_username(u):
        if not u:
            raise PermissionError("no username in token")
        return types.SimpleNamespace(username=u)

    fake_ws.principal_from_username = _principal_from_username
    monkeypatch.setitem(sys.modules, "api.middleware.workspace", fake_ws)

    from fastapi import FastAPI
    from api.routes.brain import router

    app = FastAPI()
    app.include_router(router)
    app.state.brain = brain
    return app


def test_ws_rejects_connection_without_token(monkeypatch):
    from fastapi import FastAPI, WebSocketDisconnect
    from fastapi.testclient import TestClient
    from api.routes.brain import router

    app = FastAPI()
    app.include_router(router)
    app.state.brain = _FakeBrain()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/brain") as ws:
            ws.receive_text()


def test_ws_reports_disabled_when_no_brain(monkeypatch):
    from fastapi.testclient import TestClient

    app = _app_with_brain(monkeypatch, None)
    client = TestClient(app)
    with client.websocket_connect("/ws/brain?token=x") as ws:
        frame = json.loads(ws.receive_text())
    assert frame["type"] == "error"
    assert "disabled" in frame["text"].lower()


def test_ws_streams_thought_and_clamps_client_source(monkeypatch):
    from fastapi.testclient import TestClient

    brain = _FakeBrain()
    app = _app_with_brain(monkeypatch, brain)
    client = TestClient(app)
    with client.websocket_connect("/ws/brain?token=x") as ws:
        # a client trying to claim the trusted "internal" source must be clamped
        ws.send_text(json.dumps({"type": "perceive", "source": "internal", "text": "hi"}))
        frame = json.loads(ws.receive_text())

    assert frame["type"] == "thought"
    assert "hi" in frame["text"]
    assert brain.perceived == [("user", "hi")]  # 'internal' was clamped to 'user'


def test_ws_ping_pong(monkeypatch):
    from fastapi.testclient import TestClient

    app = _app_with_brain(monkeypatch, _FakeBrain())
    client = TestClient(app)
    with client.websocket_connect("/ws/brain?token=x") as ws:
        ws.send_text(json.dumps({"action": "ping"}))
        frame = json.loads(ws.receive_text())
    assert frame == {"action": "pong"}


def test_ws_rejects_a_non_owner(monkeypatch):
    """3C-05 — a decodable token is not authority to hear the shared brain.

    `app.state.brain` is one process-global object fed by `feed_percept` with
    chat and voice transcripts from every subsystem, and every subscriber gets
    every `thought` and `speak` frame. Before this, a token that merely parsed
    was the whole check — the same defect 3C-03 fixed on `/ws/notifications`.
    """
    from fastapi import WebSocketDisconnect
    from fastapi.testclient import TestClient

    app = _app_with_brain(monkeypatch, _FakeBrain(), username="delegate")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/brain?token=x"):
            pass
    assert exc.value.code == 4003


def test_ws_rejects_a_token_with_no_subject(monkeypatch):
    """An empty `sub` must refuse rather than resolve to an empty username.

    This is why the stub returns an object instead of a dict: `getattr` on a
    dict yields None, and a check written against None would have passed a
    blank username straight through.

    The refusal now happens **one step earlier** and the close code moved from
    4003 to 4001 with it. `sub` is a required field on `TokenPayload`, so a
    token without one is refused by `decode_token` inside
    `authorise_socket_token` (P2-06) and never reaches principal resolution at
    all. 4001 is "this credential is not valid", which is what is true here;
    4003 was "you may not have this", which was the second-best answer.
    """
    from fastapi import WebSocketDisconnect
    from fastapi.testclient import TestClient

    app = _app_with_brain(monkeypatch, _FakeBrain(), username="")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/brain?token=x"):
            pass
    assert exc.value.code == 4001
