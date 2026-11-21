"""The external-API master switch, enforced at the point of egress.

IRA_ALLOW_EXTERNAL_API is the promise that nothing leaves the box unless the
owner turned external use on. These tests pin that the media generation paths —
image, image edit, video, audio, TTS, transcription — honour it, and that a
configured Replicate token is NOT treated as consent.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from utils.external_gate import external_provider_allowed, require_external_provider


@pytest.fixture
def external_off(monkeypatch):
    monkeypatch.setenv("IRA_ALLOW_EXTERNAL_API", "false")


@pytest.fixture
def external_on(monkeypatch):
    monkeypatch.setenv("IRA_ALLOW_EXTERNAL_API", "true")


# ── The gate itself ──────────────────────────────────────────────────────────

def test_the_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("IRA_ALLOW_EXTERNAL_API", raising=False)
    assert external_provider_allowed() is False


def test_the_gate_refuses_with_a_403_and_an_explanation(external_off):
    with pytest.raises(HTTPException) as ei:
        require_external_provider("Image generation")

    assert ei.value.status_code == 403
    detail = str(ei.value.detail)
    assert "IRA_ALLOW_EXTERNAL_API" in detail
    assert "Nothing was sent off the device" in detail


def test_the_gate_passes_when_the_switch_is_on(external_on):
    require_external_provider("Image generation")   # must not raise


def test_the_gate_fails_closed_when_the_setting_cannot_be_read(monkeypatch):
    def _boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr("reasoning.api_consent.external_api_allowed", _boom)
    assert external_provider_allowed() is False


def test_a_blocked_attempt_is_recorded_as_a_consent_event(external_off, monkeypatch):
    events = []
    monkeypatch.setattr(
        "reasoning.api_consent.record_consent_event",
        lambda **kw: events.append(kw) or kw,
    )

    with pytest.raises(HTTPException):
        require_external_provider("Video generation")

    assert events, "a refused external attempt must be auditable"
    assert events[0]["consent_approved"] is False
    assert events[0]["provider"] == "Replicate"


# ── Every egress helper is gated ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_image_generation_is_gated(external_off, monkeypatch):
    import httpx

    from api.routes.image_gen import GenerateRequest, _generate_replicate

    def _no_network(*a, **kw):
        raise AssertionError("no HTTP client may be constructed while external is off")

    monkeypatch.setattr(httpx, "AsyncClient", _no_network)

    with pytest.raises(HTTPException) as ei:
        await _generate_replicate(GenerateRequest(prompt="a cat"), "r8_token", "flux")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_image_editing_is_gated(external_off, monkeypatch):
    import httpx

    from api.routes.image_gen import EditRequest, _edit_replicate

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: pytest.fail("no egress"))

    with pytest.raises(HTTPException) as ei:
        await _edit_replicate(EditRequest(image_b64="x", instruction="make it blue"), "r8_token")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_video_generation_is_gated(external_off, monkeypatch):
    import httpx

    from api.routes.video_gen import VideoGenRequest, _generate_replicate_video

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: pytest.fail("no egress"))

    with pytest.raises(HTTPException) as ei:
        await _generate_replicate_video(VideoGenRequest(prompt="a sunset"))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_audio_generation_and_tts_are_gated(external_off, monkeypatch):
    import httpx

    from api.routes.audio_gen import AudioGenRequest, _generate_replicate_audio

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: pytest.fail("no egress"))

    for audio_type in ("music", "tts"):
        with pytest.raises(HTTPException) as ei:
            await _generate_replicate_audio(AudioGenRequest(prompt="a jingle"), audio_type)
        assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_a_token_alone_is_not_consent(external_off, monkeypatch):
    """The old behaviour — token set means call out — must be gone."""
    from config import get_settings

    monkeypatch.setattr(get_settings(), "replicate_api_token", "r8_configured", raising=False)

    from api.routes.image_gen import GenerateRequest, _generate_replicate

    with pytest.raises(HTTPException):
        await _generate_replicate(GenerateRequest(prompt="a cat"), "r8_configured", "flux")


# ── Transcription is local-first ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcription_uses_local_whisper_and_never_leaves_the_box(external_off, monkeypatch):
    """A local transcript must be produced without any external call."""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: pytest.fail("no egress"))
    monkeypatch.setattr(
        "voice.stt.transcribe_audio_bytes",
        lambda blob, *a, **kw: ("hello from the local model", "en", 0.9, b""),
        raising=False,
    )

    events = await _run_transcribe(monkeypatch)
    payloads = [e for e in events if "transcript" in e]

    assert payloads and payloads[0]["provider"] == "local"
    assert payloads[0]["transcript"] == "hello from the local model"


@pytest.mark.asyncio
async def test_transcription_refuses_external_fallback_when_the_switch_is_off(
    external_off, monkeypatch,
):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: pytest.fail("no egress"))
    monkeypatch.setattr(
        "voice.stt.transcribe_audio_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("faster-whisper missing")),
        raising=False,
    )
    from config import get_settings
    monkeypatch.setattr(get_settings(), "replicate_api_token", "r8_configured", raising=False)

    events = await _run_transcribe(monkeypatch)
    text = " ".join(e.get("token", "") for e in events)

    assert "external APIs are off" in text
    assert "not sent anywhere" in text
    assert not any("transcript" in e for e in events)


async def _run_transcribe(monkeypatch) -> list[dict]:
    """Drive /audio/transcribe's SSE generator and collect the JSON payloads."""
    import json
    import types

    from api.routes.audio_gen import audio_transcribe

    class _Upload:
        filename = "clip.wav"
        content_type = "audio/wav"

        async def read(self, *_a, **_kw):
            return b"\x00\x01" * 1000

    async def _read_capped(file, max_bytes=0):
        return await file.read()

    monkeypatch.setitem(
        __import__("sys").modules, "utils.file_utils",
        types.SimpleNamespace(read_with_size_cap=_read_capped),
    )

    response = await audio_transcribe(file=_Upload(), language="en", _user="owner")
    events = []
    async for chunk in response.body_iterator:
        raw = chunk["data"] if isinstance(chunk, dict) else str(chunk)
        try:
            events.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return events
