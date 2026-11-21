"""Tests for voice/lipsync.py — viseme timestamps for avatar lip sync.

Covers the dependency-free core: word→viseme expansion, WAV duration parsing,
window distribution, the estimation fallback, and the top-level dispatcher's
fail-soft path when whisper is unavailable (the default on CI hosts).
"""

from __future__ import annotations

import io
import math
import wave

import pytest

from voice.lipsync import (
    VISEMES,
    viseme_timestamps,
    wav_duration_seconds,
    word_to_visemes,
    _estimated_frames,
    _frames_for_window,
)


def _make_wav(seconds: float, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


# ── word_to_visemes ───────────────────────────────────────────────────────────

def test_word_expansion_covers_expected_classes():
    assert word_to_visemes("map")[0][0] == "PP"          # bilabial onset
    assert any(v == "aa" for v, _ in word_to_visemes("map"))
    assert any(v == "TH" for v, _ in word_to_visemes("this"))
    assert any(v == "CH" for v, _ in word_to_visemes("church"))
    assert any(v == "ou" for v, _ in word_to_visemes("moon"))
    assert any(v == "FF" for v, _ in word_to_visemes("phone"))


def test_word_expansion_only_emits_known_visemes():
    for word in ("hello", "quixotic", "rhythm", "playwright", "12345", "…"):
        for viseme, units in word_to_visemes(word):
            assert viseme in VISEMES
            assert units > 0


def test_doubled_letters_merge_into_one_viseme():
    single = [v for v, _ in word_to_visemes("mel")]
    doubled = [v for v, _ in word_to_visemes("mell")]
    assert single == doubled  # the second 'l' extends, not repeats


# ── WAV duration ──────────────────────────────────────────────────────────────

def test_wav_duration_from_header():
    assert math.isclose(wav_duration_seconds(_make_wav(1.5)), 1.5, rel_tol=1e-3)


def test_wav_duration_garbage_is_zero():
    assert wav_duration_seconds(b"not a wav") == 0.0
    assert wav_duration_seconds(b"") == 0.0


# ── window distribution ───────────────────────────────────────────────────────

def test_frames_fill_the_word_window_exactly():
    frames = _frames_for_window("hello", 2.0, 2.5)
    assert frames, "expected frames for a plain word"
    assert frames[0]["offset"] == pytest.approx(2.0, abs=1e-3)
    last = frames[-1]
    assert last["offset"] + last["duration"] == pytest.approx(2.5, abs=1e-2)
    for f in frames:
        assert f["value"] in VISEMES
        assert f["duration"] > 0


def test_frames_empty_for_degenerate_windows():
    assert _frames_for_window("hi", 1.0, 1.0) == []
    assert _frames_for_window("", 0.0, 1.0) == []


# ── estimation fallback ───────────────────────────────────────────────────────

def test_estimated_frames_tile_the_duration():
    frames = _estimated_frames("Go now. Stop!", 2.0)
    assert frames
    assert frames[0]["offset"] == pytest.approx(0.0)
    last = frames[-1]
    assert last["offset"] + last["duration"] == pytest.approx(2.0, abs=1e-2)
    assert last["value"] == "sil"  # always closes the mouth
    # punctuation and gaps become silence beats
    assert sum(1 for f in frames if f["value"] == "sil") >= 3


def test_estimated_frames_empty_for_zero_duration():
    assert _estimated_frames("hello", 0.0) == []


# ── dispatcher fail-soft ──────────────────────────────────────────────────────

def test_viseme_timestamps_falls_back_without_whisper(monkeypatch):
    # Force the alignment path off → deterministic estimation on any host.
    monkeypatch.setenv("IRA_LIPSYNC_ALIGN", "false")
    frames, source = viseme_timestamps("hello world", _make_wav(1.0))
    assert source == "estimated"
    assert frames
    assert all(f["value"] in VISEMES for f in frames)


def test_viseme_timestamps_survives_whisper_failure(monkeypatch):
    # Alignment enabled but whisper import/model fails → estimated, no raise.
    monkeypatch.setenv("IRA_LIPSYNC_ALIGN", "true")
    import voice.lipsync as lipsync

    monkeypatch.setattr(lipsync, "_whisper_word_frames", lambda wav: None)
    frames, source = viseme_timestamps("hello world", _make_wav(1.0))
    assert source == "estimated"
    assert frames
