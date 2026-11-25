"""
voice/lipsync.py — viseme timestamps for the holographic avatar's lip sync.

The Supertonic engine returns audio without phoneme alignment, but the voice
host already runs faster-whisper. Transcribing IRA's OWN synthesized WAV with
word_timestamps=True is a real forced-alignment pass: the word boundaries come
from the audio itself, so the avatar's mouth follows exactly what is audible.

Pipeline (per synthesized sentence):

  1. WHISPER ALIGNMENT (preferred, ``timing_source="whisper"``)
     Decode the WAV → transcribe with word timestamps → for each recognized
     word, expand its letters into the 15-viseme alphabet the frontend uses
     (sil PP FF TH DD kk CH SS nn RR aa E ih oh ou) and distribute them across
     the word's real [start, end] window. Gaps between words become silence.

  2. DURATION ESTIMATION (fallback, ``timing_source="estimated"``)
     When whisper is unavailable (light host, model download failed, or
     IRA_LIPSYNC_ALIGN=false), expand the full request text and stretch it
     over the WAV's real duration from its header. Same quality as the
     frontend's local estimation, computed once server-side.

All heavy imports (numpy, faster-whisper) stay inside functions so this module
imports cleanly on hosts without the voice stack (see test_voice_imports).

Env:
  IRA_LIPSYNC_ALIGN   true (default) | false — disable the whisper pass (the
                      alignment adds one CPU STT pass per sentence; disable it
                      on very slow hosts to keep time-to-first-audio low).
"""

from __future__ import annotations

import io
import logging
import os
import re
import wave

logger = logging.getLogger("ira.voice.lipsync")

# ── Viseme alphabet (must mirror frontend lib/hologram/visemes.ts) ────────────

VISEMES = (
    "sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS",
    "nn", "RR", "aa", "E", "ih", "oh", "ou",
)

_VOWEL_UNITS = 1.6
_CONS_UNITS = 1.0

# Digraphs first (regex anchored at the cursor), then single letters — the same
# English letter rules as the frontend's textToVisemes, so both timing sources
# produce the same mouth shapes and only the timing quality differs.
_DIGRAPHS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"^th"), "TH", _CONS_UNITS),
    (re.compile(r"^t?ch"), "CH", _CONS_UNITS),
    (re.compile(r"^sh"), "CH", _CONS_UNITS),
    (re.compile(r"^ph"), "FF", _CONS_UNITS),
    (re.compile(r"^wh"), "ou", _CONS_UNITS),
    (re.compile(r"^ng"), "kk", _CONS_UNITS),
    (re.compile(r"^qu"), "kk", _CONS_UNITS),
    (re.compile(r"^oo"), "ou", _VOWEL_UNITS),
    (re.compile(r"^ou|^ow"), "oh", _VOWEL_UNITS),
    (re.compile(r"^ee|^ea|^ie"), "ih", _VOWEL_UNITS),
    (re.compile(r"^ai|^ay|^ei|^ey"), "E", _VOWEL_UNITS),
    (re.compile(r"^au|^aw"), "oh", _VOWEL_UNITS),
    (re.compile(r"^oi|^oy"), "oh", _VOWEL_UNITS),
)

_SINGLE: dict[str, tuple[str, float]] = {
    "a": ("aa", _VOWEL_UNITS), "e": ("E", _VOWEL_UNITS), "i": ("ih", _VOWEL_UNITS),
    "o": ("oh", _VOWEL_UNITS), "u": ("ou", _VOWEL_UNITS), "y": ("ih", _VOWEL_UNITS * 0.7),
    "p": ("PP", _CONS_UNITS), "b": ("PP", _CONS_UNITS), "m": ("PP", _CONS_UNITS),
    "f": ("FF", _CONS_UNITS), "v": ("FF", _CONS_UNITS),
    "t": ("DD", _CONS_UNITS), "d": ("DD", _CONS_UNITS),
    "k": ("kk", _CONS_UNITS), "g": ("kk", _CONS_UNITS), "c": ("kk", _CONS_UNITS),
    "q": ("kk", _CONS_UNITS), "x": ("SS", _CONS_UNITS),
    "j": ("CH", _CONS_UNITS),
    "s": ("SS", _CONS_UNITS), "z": ("SS", _CONS_UNITS),
    "n": ("nn", _CONS_UNITS), "l": ("nn", _CONS_UNITS),
    "r": ("RR", _CONS_UNITS),
    "w": ("ou", _CONS_UNITS), "h": ("aa", _CONS_UNITS * 0.5),
}

# Peak weights per viseme class (consonant closures are sharper than vowels).
_PEAK: dict[str, float] = {
    "sil": 0.0, "PP": 0.9, "FF": 0.7, "TH": 0.6, "DD": 0.65, "kk": 0.6,
    "CH": 0.75, "SS": 0.6, "nn": 0.55, "RR": 0.6,
    "aa": 1.0, "E": 0.85, "ih": 0.7, "oh": 0.95, "ou": 0.9,
}


def word_to_visemes(word: str) -> list[tuple[str, float]]:
    """Expand one word into (viseme, relative-units) pairs. Pure — no deps."""
    out: list[tuple[str, float]] = []
    lower = word.lower()
    i = 0
    while i < len(lower):
        rest = lower[i:]
        matched = False
        for pattern, viseme, units in _DIGRAPHS:
            m = pattern.match(rest)
            if m:
                out.append((viseme, units))
                i += len(m.group(0))
                matched = True
                break
        if matched:
            continue
        single = _SINGLE.get(lower[i])
        if single:
            # Merge doubled letters into one longer viseme instead of a re-hit.
            if out and out[-1][0] == single[0]:
                out[-1] = (single[0], out[-1][1] + single[1] * 0.5)
            else:
                out.append(single)
        i += 1
    return out


def _frames_for_window(word: str, start: float, end: float) -> list[dict]:
    """Distribute a word's visemes across its real [start, end] audio window."""
    seq = word_to_visemes(word)
    total = sum(units for _, units in seq)
    span = end - start
    if total <= 0 or span <= 0:
        return []
    per_unit = span / total
    frames: list[dict] = []
    t = start
    for viseme, units in seq:
        d = units * per_unit
        frames.append({
            "value": viseme,
            "offset": round(t, 4),
            "duration": round(d, 4),
            "weight": _PEAK.get(viseme, 0.8),
        })
        t += d
    return frames


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Duration of a WAV byte string from its header (0.0 on parse failure)."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            rate = wf.getframerate()
            return wf.getnframes() / rate if rate else 0.0
    except Exception:  # noqa: BLE001 — undecodable header
        return 0.0


def _align_enabled() -> bool:
    return os.getenv("IRA_LIPSYNC_ALIGN", "true").strip().lower() not in ("false", "0", "no")


def _whisper_word_frames(wav_bytes: bytes) -> list[dict] | None:
    """Forced alignment: transcribe IRA's own synthesized audio with word
    timestamps and expand each recognized word into viseme frames.

    Returns None when whisper (or its deps) is unavailable so the caller can
    fall back to estimation. Reuses the cached HTTP STT model — no extra
    model in memory.
    """
    try:
        from voice.stt import DEFAULT_HTTP_WHISPER_MODEL, _decode_to_16k_mono, _load_model

        audio = _decode_to_16k_mono(wav_bytes)
        model = _load_model(DEFAULT_HTTP_WHISPER_MODEL, "cpu", "int8")
        segments, _info = model.transcribe(
            audio,
            language="en" if os.getenv("IRA_TTS_LANG", "en").startswith("en") else None,
            beam_size=1,          # alignment pass — speed over the last % accuracy
            vad_filter=False,     # TTS output has no long silences to skip
            word_timestamps=True,
        )
        frames: list[dict] = []
        for seg in segments:
            for w in seg.words or []:
                text = (w.word or "").strip()
                if text:
                    frames.extend(_frames_for_window(text, float(w.start), float(w.end)))
        return frames if frames else None
    except Exception as e:  # noqa: BLE001 — any missing dep / model failure
        logger.warning(f"Whisper alignment unavailable ({type(e).__name__}); using estimated timings.")
        return None


def _estimated_frames(text: str, duration: float) -> list[dict]:
    """Stretch the request text's visemes over the WAV's real duration —
    word gaps and sentence punctuation become silence beats."""
    if duration <= 0:
        return []
    items: list[tuple[str, float]] = []
    for token in re.findall(r"[A-Za-z']+|[.,;:!?…—-]|\s+", text):
        if token.isspace():
            items.append(("sil", 0.6))
        elif re.fullmatch(r"[.,;:!?…—-]", token):
            items.append(("sil", 2.0 if token in ".!?…" else 1.2))
        else:
            items.extend(word_to_visemes(token))
    items.append(("sil", 1.5))
    total = sum(u for _, u in items)
    if total <= 0:
        return []
    per_unit = duration / total
    frames: list[dict] = []
    t = 0.0
    for viseme, units in items:
        d = units * per_unit
        frames.append({
            "value": viseme,
            "offset": round(t, 4),
            "duration": round(d, 4),
            "weight": _PEAK.get(viseme, 0.8),
        })
        t += d
    return frames


def viseme_timestamps(text: str, wav_bytes: bytes) -> tuple[list[dict], str]:
    """Produce viseme timing frames for a synthesized WAV.

    Returns (frames, timing_source) where frames are
    ``{"value", "offset", "duration", "weight"}`` dicts in seconds from audio
    start — exactly the shape the frontend's ``timelineFromTimestamps`` ingests
    — and timing_source is ``"whisper"`` or ``"estimated"``.
    """
    if _align_enabled():
        frames = _whisper_word_frames(wav_bytes)
        if frames:
            return frames, "whisper"
    return _estimated_frames(text, wav_duration_seconds(wav_bytes)), "estimated"
