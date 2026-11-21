"""v1 coverage — browser-voice end-to-end wiring: transcribe -> chat -> say.

Exercises the three HTTP seams the browser VoiceConsole drives, with every heavy
engine stubbed (no faster-whisper / supertonic / Cortex / Ollama / DB):
  1. POST /voice/transcribe  -> {text, is_owner}   (DEV_MODE owner)
  2. the transcript through the chat brain (Cortex path, skill stubbed) -> reply
  3. POST /voice/say         -> a valid WAV of that reply
This covers the round-trip the two livekit-only import tests (skipped in CI) do not.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import sys

# conftest stubs sentence_transformers as a bare module; chat -> memory needs the
# SentenceTransformer symbol to exist (it's never actually called here).
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object

import asyncio
from unittest.mock import AsyncMock

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Cfg:
    ira_admin_username = "owner"
    ira_voice_service_username = "ira-voice"
    dev_mode = True
    owner_name = "Praveen"


def _voice_app() -> FastAPI:
    from api.routes.voice import router
    from api.middleware.auth import require_auth
    from api.middleware.workspace import require_workspace_principal
    from tests.conftest import make_workspace_principal

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: "owner"
    # The workspace dependency needs overriding too, not just auth. Chat
    # namespaces its cache key from the principal, so a route reached with only
    # `require_auth` stubbed gets no tenant — which is the same shape as the
    # production failure this override models away, and is why it surfaced here
    # rather than in a route test. Same pattern as tests/test_memory_vault.py.
    app.dependency_overrides[require_workspace_principal] = make_workspace_principal
    return app


def test_voice_round_trip_transcribe_chat_say(monkeypatch):
    import voice.stt as stt
    import voice.tts_supertonic as ts
    import api.routes.voice as vroute
    import api.routes.chat as chatmod
    from api.routes.chat import ChatRequest, chat
    from tests.conftest import make_workspace_principal

    client = TestClient(_voice_app())

    # ── 1. STT: stub the local engine; DEV_MODE -> is_owner True ───────────────
    monkeypatch.setattr(
        stt, "transcribe_audio_bytes",
        lambda blob, *a, **k: ("what time is it", "en", 0.98, b"\x00\x00" * 1600),
    )
    monkeypatch.setattr(vroute, "get_settings", lambda: _Cfg())
    # P2-02: `_Cfg.dev_mode = True` no longer reaches the decision — the route
    # asks `dev_mode_active()`, which is edition-gated in api.middleware.auth.
    # Patched to the private-edition answer so this end-to-end path keeps
    # exercising the owner branch it was written for.
    monkeypatch.setattr(vroute, "dev_mode_active", lambda: True)

    tr = client.post("/api/v1/voice/transcribe", files={"audio": ("u.webm", b"blob", "audio/webm")})
    assert tr.status_code == 200
    text, is_owner = tr.json()["text"], tr.json()["is_owner"]
    assert text == "what time is it" and is_owner is True

    # ── 2. Chat: feed the transcript through the brain (Cortex path, skill stubbed) ─
    monkeypatch.setattr(chatmod, "_USE_CORTEX", True)
    monkeypatch.setattr(chatmod, "get_settings", lambda: _Cfg())
    monkeypatch.setattr(chatmod, "cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(chatmod, "cache_set", AsyncMock())
    # Reachable now that this test passes a real principal. Previously `_p` was
    # the `Depends(...)` sentinel and the call failed soft, so the handler was
    # exercised less than it appeared to be.
    import memory.relationships as _rel
    monkeypatch.setattr(_rel, "get_people_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv1"))
    monkeypatch.setattr(chatmod, "get_recent_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr("memory.store.save_message", AsyncMock())
    monkeypatch.setattr("skills._common.run_skill", lambda skill, message, **kw: "It is twelve noon.")
    monkeypatch.setattr(
        "agents.supervisor.classify",
        AsyncMock(return_value={"active_agent": "conversational", "use_deep_model": False}),
    )

    resp = asyncio.run(
        chat(
            ChatRequest(message=text, session_id="s1", is_voice=True, is_voice_owner=is_owner),
            # A real principal, because this calls the handler as a plain
            # function — no dependency resolution runs, so the `Depends(...)`
            # default would arrive as the argument itself.
            _p=make_workspace_principal(),
            _user="owner",
        )
    )
    reply = resp.response
    assert reply.strip() and resp.model_used == "cortex"

    # ── 3. TTS: speak the reply -> a valid WAV ─────────────────────────────────
    monkeypatch.setattr(
        ts, "synthesize_wav",
        lambda t, voice="F1", lang="en", steps=None: ts._encode_wav(np.zeros(2205, dtype=np.float32), 44_100),
    )
    say = client.post("/api/v1/voice/say", json={"text": reply})
    assert say.status_code == 200
    assert say.content[:4] == b"RIFF" and say.content[8:12] == b"WAVE"
