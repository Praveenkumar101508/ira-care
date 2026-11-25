"""IRA's own audio decoder (voice/audio_decode.py), which replaced PyAV.

PyAV bundles FFmpeg built with the GPL encoders libx264 and libx265, and the owner decided it
does not ship (docs/security/LICENCES.md). The decoder that replaced it reads WAV with the
standard library and numpy and refuses everything else. These tests check that it:

* turns WAV at any common rate, width and channel count into 16 kHz mono float32 that still
  carries the same sound (frequency, level, length);
* filters before converting down, so sound above 8 kHz is removed rather than folded back
  into the speech band as a false tone;
* refuses formats it does not read with ``UnsupportedAudio`` naming what it found, never with
  a crash and never by decoding the bytes as if they were PCM;
* keeps the routes' length limit, read from the header before any sample is converted.

Each test that could pass by accident has a control beside it showing the check can fail.
"""
from __future__ import annotations

import io
import math
import os
import struct
import wave

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import numpy as np
import pytest

from voice import audio_decode as ad

SR = 16_000


# ── WAV builders (standard library only) ──────────────────────────────────────

def _tone(rate: int, seconds: float, hz: float = 440.0, level: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return level * np.sin(2 * np.pi * hz * t)


def _wav(samples: np.ndarray, rate: int, *, channels: int = 1, width: int = 2,
         float_format: bool = False, extensible: bool = False, data_size: int | None = None) -> bytes:
    """A WAV file holding ``samples`` (float, -1..1) copied to every channel.

    Written by hand rather than with ``wave`` so 24/32-bit, float and extensible headers can
    be built too.
    """
    frames = np.repeat(samples[:, None], channels, axis=1).reshape(-1)
    if float_format:
        data = frames.astype("<f4" if width == 4 else "<f8").tobytes()
        code = 3
    elif width == 1:
        data = np.clip(np.round(frames * 127 + 128), 0, 255).astype(np.uint8).tobytes()
        code = 1
    elif width == 2:
        data = np.clip(np.round(frames * 32767), -32768, 32767).astype("<i2").tobytes()
        code = 1
    elif width == 3:
        ints = np.clip(np.round(frames * 8388607), -8388608, 8388607).astype(np.int32)
        b = ints.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
        data = b.tobytes()
        code = 1
    else:
        data = np.clip(np.round(frames * 2147483647), -2147483648, 2147483647).astype("<i4").tobytes()
        code = 1
    block = channels * width
    if extensible:
        guid = struct.pack("<H", code) + bytes.fromhex("000000001000800000aa00389b71")
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, channels, rate, rate * block, block, width * 8,
                          22, width * 8, 0) + guid
    else:
        fmt = struct.pack("<HHIIHH", code, channels, rate, rate * block, block, width * 8)
    size = len(data) if data_size is None else data_size
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"LIST" + struct.pack("<I", 4) + b"INFO"           # a chunk to skip
    body += b"data" + struct.pack("<I", size) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _dominant_hz(x: np.ndarray, rate: int = SR) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.argmax(spectrum)) * rate / x.size


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


# ── 1. WAV in, 16 kHz mono float32 out ────────────────────────────────────────

@pytest.mark.parametrize("rate", [8_000, 11_025, 16_000, 22_050, 32_000, 44_100, 48_000, 96_000])
@pytest.mark.parametrize("channels", [1, 2, 6])
def test_any_rate_and_channel_count_becomes_16k_mono(rate, channels):
    blob = _wav(_tone(rate, 1.5), rate, channels=channels)
    out = ad.decode_to_16k_mono(blob)
    assert out.dtype == np.float32 and out.ndim == 1
    assert abs(out.size - int(1.5 * SR)) <= 2, out.size
    mid = out[1600:-1600]
    assert abs(_dominant_hz(mid) - 440.0) < 3.0                 # the same tone
    assert abs(_rms(mid) - 0.5 / math.sqrt(2)) < 0.01           # at the same level
    ideal = _tone(SR, 1.5)[:out.size][1600:-1600]
    assert np.max(np.abs(mid - ideal)) < 0.003                  # and in time with it


@pytest.mark.parametrize("width, float_format, extensible, tolerance", [
    (1, False, False, 0.02),     # 8-bit unsigned
    (2, False, False, 2e-4),
    (3, False, False, 1e-5),
    (4, False, False, 1e-5),
    (4, True, False, 1e-5),      # float32
    (8, True, False, 1e-5),      # float64
    (2, False, True, 2e-4),      # WAVE_FORMAT_EXTENSIBLE, PCM
    (4, True, True, 1e-5),       # WAVE_FORMAT_EXTENSIBLE, float
])
def test_every_pcm_and_float_width_is_read_at_its_value(width, float_format, extensible, tolerance):
    x = _tone(SR, 0.5, level=0.7)
    out = ad.decode_to_16k_mono(_wav(x, SR, width=width, float_format=float_format,
                                     extensible=extensible))
    assert out.size == x.size
    assert np.max(np.abs(out - x)) < tolerance


def test_control_the_value_check_catches_a_misread_width():
    """Reading 24-bit samples as if they were 16-bit must fail the check above."""
    x = _tone(SR, 0.5, level=0.7)
    blob = bytearray(_wav(x, SR, width=3))
    fmt_at = blob.index(b"fmt ") + 8
    struct.pack_into("<H", blob, fmt_at + 14, 16)          # lie: 16 bits per sample
    struct.pack_into("<H", blob, fmt_at + 12, 2)
    out = ad.decode_to_16k_mono(bytes(blob))
    assert out.size != x.size or np.max(np.abs(out - x)) > 0.1


def test_channels_are_averaged_not_just_the_first_taken():
    """Left carries the tone, right its inverse: the mono mix is silence. Taking one channel
    would hear a full-level tone."""
    x = _tone(SR, 0.5)
    frames = np.stack([x, -x], axis=1).reshape(-1)
    blob = _wav(frames, SR, channels=1)                    # build, then relabel as stereo
    blob = bytearray(blob)
    fmt_at = blob.index(b"fmt ") + 8
    struct.pack_into("<HHIIHH", blob, fmt_at, 1, 2, SR, SR * 4, 4, 16)
    out = ad.decode_to_16k_mono(bytes(blob))
    assert out.size == x.size and _rms(out) < 1e-3
    left_only = np.frombuffer(blob[blob.index(b"data") + 8:], dtype="<i2")[0::2] / 32768.0
    assert _rms(left_only) > 0.3                           # control: one channel is not silent


def test_sound_above_8_khz_is_removed_not_folded_back():
    """A 10 kHz tone cannot exist at 16 kHz. Without a low-pass filter it reappears as a
    false 6 kHz tone at full level, which the recogniser hears as noise."""
    rate = 44_100
    out = ad.decode_to_16k_mono(_wav(_tone(rate, 1.0, hz=10_000), rate))
    assert _rms(out[800:-800]) < 0.005
    # Control: plain decimation (what an unfiltered converter does) keeps the alias.
    t = np.arange(rate) / rate
    naive = (0.5 * np.sin(2 * np.pi * 10_000 * t))[::3]   # 14.7 kHz, near enough
    assert _rms(naive) > 0.3


def test_matches_a_reference_resampler():
    """Independent check against SciPy's polyphase resampler, where SciPy is installed."""
    signal = pytest.importorskip("scipy.signal")
    rng = np.random.default_rng(7)
    rate = 44_100
    x = rng.standard_normal(rate) * 0.1
    ours = ad.decode_to_16k_mono(_wav(x, rate, width=4, float_format=True))
    theirs = signal.resample_poly(x, SR, rate)
    band = slice(400, -400)
    # Both remove what is above 8 kHz; on band-limited content they agree closely.
    assert ours.size == theirs.size
    corr = np.corrcoef(ours[band], theirs[band])[0, 1]
    assert corr > 0.99, corr


def test_a_streamed_wav_with_an_unknown_length_is_read_to_its_end():
    """Recorders that stream write 0 or 0xFFFFFFFF as the data size."""
    x = _tone(SR, 1.0)
    for size in (0, 0xFFFFFFFF):
        out = ad.decode_to_16k_mono(_wav(x, SR, data_size=size))
        assert out.size == x.size


def test_a_cut_off_wav_gives_what_is_there():
    x = _tone(SR, 1.0)
    blob = _wav(x, SR)
    out = ad.decode_to_16k_mono(blob[:len(blob) - 2001])  # not a whole sample at the end
    assert out.size == x.size - 1001


def test_stdlib_written_wav_round_trips():
    """What voice/wakeword.py posts: 16-bit mono PCM written by the `wave` module."""
    pcm = (np.round(_tone(SR, 1.0) * 32767)).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    out = ad.decode_to_16k_mono(buf.getvalue())
    assert np.array_equal(out, np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0)


def test_empty_data_is_empty_audio_not_an_error():
    out = ad.decode_to_16k_mono(_wav(np.zeros(0), SR))
    assert out.dtype == np.float32 and out.size == 0


# ── 2. Everything else is refused, by name ────────────────────────────────────

_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x21" + b"\x00" * 40 + b"\xff\xfb\x90\x64" + b"\x55" * 400
_SAMPLES = {
    "an MP3 file": _MP3,
    "an MP3 or AAC stream": b"\xff\xfb\x90\x64" + bytes(range(256)) * 4,
    "a FLAC file": b"fLaC\x00\x00\x00\x22" + b"\x12" * 400,
    "an Ogg file (Vorbis or Opus)": b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead" + b"\x01" * 400,
    "a WebM or Matroska file": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01" + b"webm" + b"\x00" * 400,
    "an MP4 or M4A file": b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00" + b"\x00" * 400,
    "an AIFF file": b"FORM\x00\x00\x01\x00AIFFCOMM" + b"\x00" * 400,
    "an empty file": b"",
    "a file in a format this server does not recognise": b"hello, this is a text file\n" * 20,
}


@pytest.mark.parametrize("kind", sorted(_SAMPLES))
def test_a_format_it_does_not_read_is_refused_by_name(kind):
    with pytest.raises(ad.UnsupportedAudio) as e:
        ad.decode_to_16k_mono(_SAMPLES[kind])
    assert e.value.kind == kind
    assert "WAV" in str(e.value)


def test_random_bytes_are_refused_not_decoded_as_noise():
    rng = np.random.default_rng(3)
    junk = rng.integers(0, 256, 50_000, dtype=np.uint8).tobytes()
    with pytest.raises(ad.UnsupportedAudio):
        ad.decode_to_16k_mono(junk)


def _relabel(code: int, bits: int = 16) -> bytes:
    blob = bytearray(_wav(_tone(SR, 0.2), SR))
    fmt_at = blob.index(b"fmt ") + 8
    struct.pack_into("<H", blob, fmt_at, code)
    struct.pack_into("<H", blob, fmt_at + 14, bits)
    return bytes(blob)


@pytest.mark.parametrize("blob, says", [
    (_relabel(0x0007, 8), "format code 0x0007"),       # mu-law
    (_relabel(0x0006, 8), "format code 0x0006"),       # A-law
    (_relabel(0x0055), "format code 0x0055"),          # MP3 inside a WAV
    (_relabel(0x0002, 4), "format code 0x0002"),       # ADPCM
    (_relabel(1, 12), "12-bit PCM"),
    (_relabel(3, 16), "16-bit float"),
])
def test_a_wav_in_a_coding_it_does_not_read_is_refused(blob, says):
    with pytest.raises(ad.UnsupportedAudio) as e:
        ad.decode_to_16k_mono(blob)
    assert e.value.kind.startswith("a WAV file this server cannot read") and says in e.value.kind


@pytest.mark.parametrize("mutate, says", [
    (lambda b: b.replace(b"data", b"junk"), "no data chunk"),
    (lambda b: b.replace(b"fmt ", b"junk"), "no format chunk"),
    (lambda b: b[:b.index(b"fmt ") + 14], "truncated format chunk"),
])
def test_a_broken_wav_is_refused_with_the_reason(mutate, says):
    with pytest.raises(ad.UnsupportedAudio, match=says):
        ad.decode_to_16k_mono(mutate(_wav(_tone(SR, 0.2), SR)))


@pytest.mark.parametrize("channels, rate, says", [(0, SR, "0 channels"), (2, 1_000, "1000 Hz"),
                                                   (2, 1_000_000, "1000000 Hz")])
def test_impossible_headers_are_refused(channels, rate, says):
    blob = bytearray(_wav(_tone(SR, 0.2), SR))
    fmt_at = blob.index(b"fmt ") + 8
    struct.pack_into("<HHI", blob, fmt_at, 1, channels, rate)
    with pytest.raises(ad.UnsupportedAudio, match=says):
        ad.decode_to_16k_mono(bytes(blob))


# ── 3. The length limit comes from the header ─────────────────────────────────

def test_a_recording_over_the_limit_is_refused_with_its_length():
    blob = _wav(_tone(44_100, 3.0), 44_100, channels=2)
    with pytest.raises(ad.AudioTooLong) as e:
        ad.decode_to_16k_mono(blob, max_seconds=2)
    assert e.value.seconds == pytest.approx(3.0) and e.value.limit == 2
    assert ad.decode_to_16k_mono(blob, max_seconds=3).size == 48_000


def test_the_limit_is_checked_before_converting(monkeypatch):
    calls = []
    monkeypatch.setattr(ad, "_to_float_mono", lambda *a: calls.append(a))
    with pytest.raises(ad.AudioTooLong):
        ad.decode_to_16k_mono(_wav(_tone(SR, 3.0), SR), max_seconds=1)
    assert calls == []


# ── 4. The faster-whisper interface ──────────────────────────────────────────

def test_decode_audio_reads_paths_streams_and_bytes(tmp_path):
    blob = _wav(_tone(22_050, 1.0), 22_050)
    path = tmp_path / "a.wav"
    path.write_bytes(blob)
    for source in (str(path), path, io.BytesIO(blob), blob):
        out = ad.decode_audio(source, sampling_rate=SR)
        assert out.dtype == np.float32 and abs(out.size - SR) <= 1


def test_decode_audio_split_stereo_keeps_the_channels_apart():
    x = _tone(SR, 0.5)
    frames = np.stack([x, np.zeros_like(x)], axis=1).reshape(-1)
    blob = bytearray(_wav(frames, SR))
    fmt_at = blob.index(b"fmt ") + 8
    struct.pack_into("<HHIIHH", blob, fmt_at, 1, 2, SR, SR * 4, 4, 16)
    left, right = ad.decode_audio(bytes(blob), split_stereo=True)
    assert _rms(left) > 0.3 and _rms(right) < 1e-6


def test_pad_or_trim_matches_its_contract():
    a = np.ones((80, 10))
    assert ad.pad_or_trim(a, 3000).shape == (80, 3000)
    assert ad.pad_or_trim(np.ones((80, 4000)), 3000).shape == (80, 3000)
    assert ad.pad_or_trim(a, 3000)[:, 10:].sum() == 0


# ── 5. Through the product path ──────────────────────────────────────────────

def test_transcribe_audio_bytes_refuses_mp3_before_loading_the_model(monkeypatch):
    import voice.stt as stt
    monkeypatch.setattr(stt, "stt_engine_installed", lambda: True)
    monkeypatch.setattr(stt, "_load_model", lambda *a: pytest.fail("the model was loaded"))
    with pytest.raises(stt.UnsupportedAudio) as e:
        stt.transcribe_audio_bytes(_MP3, max_seconds=60)
    assert e.value.kind == "an MP3 file"


def test_transcribe_audio_bytes_hands_the_recogniser_16k_float32(monkeypatch):
    import voice.stt as stt
    seen = {}

    def transcribe_sync(audio, model, language):
        seen["audio"] = audio
        return "hello", "en", 0.9

    monkeypatch.setattr(stt, "stt_engine_installed", lambda: True)
    monkeypatch.setattr(stt, "_load_model", lambda *a: object())
    monkeypatch.setattr(stt, "_transcribe_sync", transcribe_sync)
    text, _lang, _conf, pcm16 = stt.transcribe_audio_bytes(
        _wav(_tone(48_000, 2.0), 48_000, channels=2), max_seconds=60)
    assert text == "hello"
    assert seen["audio"].dtype == np.float32 and seen["audio"].size == 32_000
    assert len(pcm16) == 64_000                       # the owner-gate's 16-bit PCM, same audio


def _voice_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import voice.stt as stt
    from api.middleware.auth import require_auth
    from api.routes.voice import router

    monkeypatch.setattr(stt, "stt_engine_installed", lambda: True)
    monkeypatch.setattr(stt, "_load_model", lambda *a: object())
    monkeypatch.setattr(stt, "_transcribe_sync", lambda audio, model, lang: ("hi", "en", 0.9))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: "admin"
    return TestClient(app)


def test_the_voice_route_answers_415_naming_the_format(monkeypatch):
    r = _voice_client(monkeypatch).post(
        "/api/v1/voice/transcribe", files={"audio": ("u.webm", _SAMPLES["a WebM or Matroska file"], "audio/webm")})
    assert r.status_code == 415, r.text
    assert "WebM" in r.json()["detail"] and "WAV" in r.json()["detail"]


def test_control_the_voice_route_accepts_wav(monkeypatch):
    monkeypatch.setattr("api.routes.voice.dev_mode_active", lambda: True)   # no owner gate
    r = _voice_client(monkeypatch).post(
        "/api/v1/voice/transcribe", files={"audio": ("u.wav", _wav(_tone(SR, 1.0), SR), "audio/wav")})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "hi"
