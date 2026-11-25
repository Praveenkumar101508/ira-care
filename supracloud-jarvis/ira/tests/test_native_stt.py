"""Local speech-to-text on the native install.

Three defects were found by running `/api/v1/voice/transcribe` on a fresh
native install (Python 3.12, `pip install -r requirements.txt`) on 2026-10-18,
and each has a guard here:

  1. faster-whisper was not in requirements.txt at all, and a missing engine
     was answered with **422 "Could not decode or transcribe the audio"**,
     retryable=false — blaming the user's recording. It is now 503.
  2. Once installed, faster-whisper 1.2.1 resolved PyAV 19, which removed the
     `metadata_errors=` argument its decoder passes, so every decode raised
     TypeError. PyAV has since been removed altogether (its FFmpeg carries GPL
     encoders; docs/security/LICENCES.md): IRA decodes WAV itself
     (voice/audio_decode.py), and that REAL decoder is exercised below.
  3. The checkpoint was fetched by bare name — faster-whisper maps it to a Hub
     repository at `main`, the MOD-02 shape. It now resolves through
     `core/models/artefacts.py`, and an unpinned name is refused.

Plus the default: `distil-large-v3` measured ~17–20 s per short utterance on
a 4-vCPU box. The resolver now picks by hardware tier.
"""
from __future__ import annotations

import io
import json
import math
import os
import pathlib
import re
import struct
import wave

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

IRA_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = IRA_ROOT.parents[1]


def _voice_app() -> FastAPI:
    from api.middleware.auth import require_auth
    from api.routes.voice import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: "admin"
    return app


def _wav(seconds: float = 1.0, rate: int = 22_050, hz: float = 440.0) -> bytes:
    """A real 16-bit mono WAV, built with the stdlib — no codec involved."""
    n = int(seconds * rate)
    frames = b"".join(
        struct.pack("<h", int(12_000 * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


@pytest.fixture
def fresh_stt(monkeypatch):
    """voice.stt with an empty model cache, restored afterwards."""
    import voice.stt as stt
    monkeypatch.setattr(stt, "_whisper_model", None)
    monkeypatch.setattr(stt, "_whisper_loaded", False)
    return stt


# ── 1. A missing engine is the server's problem, not the audio's ─────────────

def test_transcribe_raises_unavailable_without_faster_whisper(fresh_stt, monkeypatch):
    monkeypatch.setattr(fresh_stt, "WhisperModel", None)
    with pytest.raises(fresh_stt.STTUnavailable):
        fresh_stt.transcribe_audio_bytes(_wav())


def test_route_answers_503_when_the_engine_is_unavailable(monkeypatch):
    import voice.stt as stt

    def _missing(blob, *a, **k):
        raise stt.STTUnavailable("faster-whisper is not installed")

    monkeypatch.setattr(stt, "transcribe_audio_bytes", _missing)
    r = TestClient(_voice_app()).post(
        "/api/v1/voice/transcribe", files={"audio": ("u.wav", b"RIFF", "audio/wav")})
    assert r.status_code == 503, r.text


def test_route_still_answers_422_for_audio_it_cannot_decode(monkeypatch):
    """The other half: a genuinely bad recording keeps its 422. Without this the
    fix above could have turned every failure into 503 and still passed."""
    import voice.stt as stt

    def _undecodable(blob, *a, **k):
        raise ValueError("Invalid data found when processing input")

    monkeypatch.setattr(stt, "transcribe_audio_bytes", _undecodable)
    r = TestClient(_voice_app()).post(
        "/api/v1/voice/transcribe", files={"audio": ("u.wav", b"RIFF junk", "audio/wav")})
    assert r.status_code == 422, r.text


# ── 2. The real decoder ──────────────────────────────────────────────────────

def test_the_real_decoder_reads_a_wav():
    """The route's own decode of a WAV it did not write: resampled to 16 kHz.

    Under PyAV 19 every decode raised TypeError and the route answered 422 on
    perfectly good audio. The decoder is now IRA's own and needs no package.
    """
    import voice.stt as stt

    audio = stt._decode_to_16k_mono(_wav(seconds=1.0, rate=22_050))
    assert audio.dtype.name == "float32"
    assert abs(len(audio) - 16_000) <= 160, len(audio)   # resampled to 16 kHz
    assert 0.1 < float(abs(audio).max()) <= 1.0          # the tone, not silence


# ── 3. Every fetch is pinned (MOD-02) ────────────────────────────────────────

def test_every_loadable_checkpoint_is_pinned():
    from core.models import artefacts
    import voice.stt as stt

    for name, key in stt.WHISPER_ARTEFACTS.items():
        assert key in artefacts.ARTEFACTS, f"{name} maps to {key}, which is not pinned"
        pin = artefacts.ARTEFACTS[key]
        assert re.fullmatch(r"[0-9a-f]{40}", pin.revision), (name, pin.revision)
        assert pin.identifier.startswith("Systran/"), (name, pin.identifier)
    assert stt.DEFAULT_CPU_WHISPER in stt.WHISPER_ARTEFACTS
    assert stt.LOW_RESOURCE_WHISPER in stt.WHISPER_ARTEFACTS


def test_the_load_passes_the_pinned_repository_and_revision(fresh_stt, monkeypatch):
    from core.models import artefacts

    seen = {}

    class _Recorder:
        def __init__(self, source, **kw):
            seen["source"] = source
            seen.update(kw)

    monkeypatch.setattr(fresh_stt, "WhisperModel", _Recorder)
    fresh_stt._load_model("small", "cpu", "int8")
    pin = artefacts.ARTEFACTS["whisper_small"]
    assert seen["source"] == pin.identifier       # a repository, never a bare name
    assert seen["revision"] == pin.revision       # …at the recorded commit


@pytest.mark.parametrize("name", ["medium", "tiny.en", "large-v3-turbo", "openai/whisper-small"])
def test_an_unpinned_checkpoint_is_refused_not_fetched(fresh_stt, monkeypatch, name):
    constructed = []
    monkeypatch.setattr(fresh_stt, "WhisperModel",
                        lambda *a, **k: constructed.append((a, k)))
    with pytest.raises(fresh_stt.STTUnavailable, match="not a pinned checkpoint"):
        fresh_stt._load_model(name, "cpu", "int8")
    assert not constructed, "an unpinned name reached the downloader"
    # A configuration error is not cached as a failed load: fixing the env and
    # retrying must not need a restart.
    assert fresh_stt._whisper_loaded is False


def test_a_moving_revision_override_is_refused(fresh_stt, monkeypatch):
    from core.models import artefacts

    monkeypatch.setitem(artefacts.ARTEFACTS, "whisper_small", artefacts.Artefact(
        identifier="Systran/faster-whisper-small", revision="main"))
    monkeypatch.setattr(fresh_stt, "WhisperModel", lambda *a, **k: pytest.fail("fetched"))
    with pytest.raises(fresh_stt.STTUnavailable):
        fresh_stt._load_model("small", "cpu", "int8")


def test_a_local_model_directory_is_loaded_without_a_revision(fresh_stt, monkeypatch, tmp_path):
    """Air-gapped installs point WHISPER_MODEL at a converted model on disk.
    Nothing is fetched, so there is nothing to pin."""
    seen = {}

    class _Recorder:
        def __init__(self, source, **kw):
            seen["source"] = source
            seen.update(kw)

    monkeypatch.setattr(fresh_stt, "WhisperModel", _Recorder)
    fresh_stt._load_model(str(tmp_path), "cpu", "int8")
    assert seen["source"] == str(tmp_path) and seen["revision"] is None


# ── 4. The default fits the machine ─────────────────────────────────────────

@pytest.mark.parametrize("env,cpus,expected", [
    ({}, 8, "small"),                                        # CPU default
    ({}, 4, "small"),
    ({}, 2, "base"),                                         # weak machine
    ({"IRA_MODEL_PROFILE": "low_resource"}, 16, "base"),     # same tier as the chat model
    ({"IRA_MODEL_PROFILE": "strong_local"}, 16, "small"),    # still CPU: distil is ~4x slower
    ({"IRA_MODEL_PROFILE": "no-such-profile"}, 8, "small"),  # unknown degrades to default
    ({"WHISPER_MODEL": "large-v3"}, 2, "large-v3"),          # an explicit choice wins
    ({"WHISPER_MODEL": "  "}, 8, "small"),                   # blank is not a choice
])
def test_the_default_checkpoint_follows_the_hardware_tier(env, cpus, expected):
    import voice.stt as stt
    assert stt.resolve_whisper_model(env=env, cpus=cpus) == expected


def test_the_example_env_leaves_the_choice_to_the_resolver():
    """`setup-windows.ps1` copies .env.example to .env, so a model named there
    overrides the resolver on every install that used the setup script.

    EXACTLY empty, not "empty before the #": start-ira.sh and start-ira.ps1
    export everything after `=` verbatim, inline comment included, and the
    resolver would read the comment as an (unpinned, refused) model name.
    """
    text = (IRA_ROOT.parent / ".env.example").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("WHISPER_MODEL="))
    assert line == "WHISPER_MODEL=", line


# ── 5. Supertonic stays out until its licence is read (L-06) ─────────────────

def _section(text: str, item: str) -> str:
    start = text.index(f"## {item}")
    nxt = text.find("\n## ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


