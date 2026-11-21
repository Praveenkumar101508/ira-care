"""The wake listener hands the owner check enough audio to decide.

On `main` at e193eba5, `_secs_to_frames(1.0)` rounded 12.5 frames to 12: 12 x 2 560
= 30 720 bytes. `voice/biometrics.py` refuses anything under 32 000 bytes (one
second at 16 kHz, 16-bit mono) before it compares voices. So with the wake word
on, every wake was answered "speaker is not the owner", whoever spoke. The
existing tests passed `verify_frames=1` explicitly and never saw the default.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import voice.wakeword as ww

IRA = Path(__file__).resolve().parents[1]


def _biometric_minimum() -> int:
    """Read the floor from the biometric check itself, so the two cannot drift."""
    src = (IRA / "voice" / "biometrics.py").read_text(encoding="utf-8")
    found = re.search(r"len\(audio_bytes\)\s*<\s*([\d_]+)", src)
    assert found, "voice/biometrics.py no longer states its minimum audio length"
    return int(found.group(1).replace("_", ""))


def test_one_second_of_frames_meets_the_biometric_minimum():
    assert ww._secs_to_frames(1.0) * ww.FRAME_BYTES >= _biometric_minimum()


@pytest.mark.parametrize("secs", [0.5, 1.0, 1.5, 2.0, 5.0])
def test_a_window_is_never_shorter_than_asked(secs):
    frames = ww._secs_to_frames(secs)
    assert frames * ww.FRAME_SAMPLES >= secs * ww.SAMPLE_RATE
    assert (frames - 1) * ww.FRAME_SAMPLES < secs * ww.SAMPLE_RATE, "more than one frame over"


class _Silence:
    def read(self):
        return b"\x00" * ww.FRAME_BYTES

    def close(self):
        pass


class _Never:
    def score(self, frame):
        return 0.0


def test_the_listener_built_with_its_defaults_sends_the_owner_check_enough_audio():
    """The listener as built with its defaults, through its own wake path."""
    seen: list[int] = []

    async def gate(pcm, *, session_id):
        seen.append(len(pcm))
        return False  # stop after the owner check

    listener = ww.WakeWordListener(None, detector=_Never(), audio_source=_Silence(), gate=gate)

    async def _wake():
        await listener._on_wake(asyncio.get_running_loop())

    asyncio.run(_wake())
    assert seen and seen[0] >= _biometric_minimum(), (
        f"the owner check got {seen} bytes; the biometric check needs {_biometric_minimum()}")
