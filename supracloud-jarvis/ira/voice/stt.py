"""
Faster-Whisper STT: the sovereign HTTP path (POST /api/v1/voice/transcribe) and
the LiveKit Agents plugin.

Device: CPU (preserves GPU VRAM for LLM inference)
Language: auto-detect on every utterance

── Which checkpoint ─────────────────────────────────────────────────────────

The HTTP path's model is chosen by ``resolve_whisper_model``: ``WHISPER_MODEL``
when set, otherwise by the same hardware tier that picks the chat model
(``IRA_MODEL_PROFILE``) and the CPUs this process may use. Measured on a 4-vCPU
Xeon, int8, beam 5, 3–5 s utterances (2026-10-18):

  base             ~2 s    misheard "Hello IRA" as "Though I array"
  small            ~5 s    (≈6.4 s on 2 threads) English WER 0–0.08, multilingual
  distil-large-v3  ~17–20 s (≈28 s on 2 threads) same English WER, English-only

so ``small`` is the CPU default and ``base`` is kept for weak machines.

── Where the weights come from (MOD-02) ────────────────────────────────────

faster-whisper resolves a bare name like "small" to a Hub repository at
``main``. IRA never lets it: every loadable name maps to a pin in
``core/models/artefacts.py`` and the fetch passes that revision. A name with no
pin is refused (``STTUnavailable``), not fetched at whatever the Hub serves.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from core.runtime.vendor_telemetry import switch_off_vendor_telemetry

# faster-whisper's voice-activity filter (vad_filter=True below) imports ONNX
# Runtime, which reports to Microsoft at import unless this switch is already
# set. main.py and worker/main.py set it first; this covers any other process
# that transcribes, such as the LiveKit voice host (voice/agent.py) and
# `python -m voice.stt`.
switch_off_vendor_telemetry()

# faster-whisper (the STT engine) and livekit-agents (the plugin base) live only on
# the voice host. The sovereign HTTP path (transcribe_audio_bytes → POST
# /api/v1/voice/transcribe) and CI must import this module without them, so import
# both softly; the heavy work fails gracefully if a dep is genuinely missing.
try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover - only where faster-whisper isn't installed
    WhisperModel = None

try:
    from livekit import rtc
    from livekit.agents import stt
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
    _STTBase = stt.STT
    _LIVEKIT_AVAILABLE = True
except Exception:  # pragma: no cover - only on hosts without livekit-agents
    rtc = stt = None
    DEFAULT_API_CONNECT_OPTIONS = APIConnectOptions = None
    _STTBase = object
    _LIVEKIT_AVAILABLE = False

from voice.language import normalise_lang

logger = logging.getLogger("ira.stt")

_STT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")

# Audio constants — LiveKit sends 48kHz, Whisper expects 16kHz
LIVEKIT_SAMPLE_RATE = 48_000
WHISPER_SAMPLE_RATE = 16_000
DOWNSAMPLE_RATIO = LIVEKIT_SAMPLE_RATE // WHISPER_SAMPLE_RATE  # = 3

# VAD tuning — a larger min-silence waits longer before treating a pause as the end
# of speech, so accented / half-finished utterances aren't clipped mid-sentence.
_VAD_MIN_SILENCE_MS = int(os.getenv("WHISPER_VAD_MIN_SILENCE_MS", "700"))
_VAD_MIN_SPEECH_MS = int(os.getenv("WHISPER_VAD_MIN_SPEECH_MS", "200"))


class STTUnavailable(RuntimeError):
    """Local STT cannot run on this host: faster-whisper is not installed, the
    requested checkpoint is not pinned, or the model failed to load.

    A server-side condition, so the HTTP route answers 503. It used to surface
    as 422 "Could not decode or transcribe the audio" with retryable=false —
    telling the browser its recording was bad when the engine was missing.
    """


class AudioTooLong(ValueError):
    """The recording is longer than the caller will transcribe.

    ``seconds`` is its length when the file states it, else None: decoding
    stops as soon as the limit is passed, so the full length is not known.
    """

    def __init__(self, seconds: float | None, limit: float):
        super().__init__(f"audio longer than {limit:.0f} s"
                         + (f" ({seconds:.0f} s)" if seconds is not None else ""))
        self.seconds = seconds
        self.limit = limit


#: Every checkpoint ``WHISPER_MODEL`` may name, mapped to its pin in
#: ``core/models/artefacts.py``. Anything else is refused rather than fetched.
WHISPER_ARTEFACTS: dict[str, str] = {
    "base": "whisper_base",
    "small": "whisper_small",
    "distil-large-v3": "whisper_distil_large_v3",   # English-only
    "large-v3": "whisper_large_v3",                 # multilingual; slow on CPU
}

#: CPU default, and the lighter choice for weak machines. See the module
#: docstring for the measurements behind both.
DEFAULT_CPU_WHISPER = "small"
LOW_RESOURCE_WHISPER = "base"
#: Below this many usable CPUs ``small`` measured ~6.4 s per short utterance.
_MIN_CPUS_FOR_DEFAULT = 4


def stt_engine_installed() -> bool:
    """True when faster-whisper imported, i.e. transcription can be attempted."""
    return WhisperModel is not None


def _usable_cpus() -> int:
    """CPUs this process may run on — the affinity mask where the OS has one,
    because a container's cpu_count() can report the host's cores."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def resolve_whisper_model(env=None, cpus: int | None = None) -> str:
    """The checkpoint the HTTP path loads.

    1. ``WHISPER_MODEL`` when set and non-empty — an explicit operator choice.
    2. ``base`` on the ``low_resource`` profile or with fewer than 4 usable CPUs.
    3. ``small`` otherwise.

    Tied to ``IRA_MODEL_PROFILE`` because that is the knob that already sizes
    the chat model to the machine; a box set to ``low_resource`` for its LLM is
    the same box that cannot afford a slower recogniser.
    """
    env = os.environ if env is None else env
    explicit = (env.get("WHISPER_MODEL") or "").strip()
    if explicit:
        return explicit
    try:
        from reasoning.model_profiles import active_profile_name
        profile = active_profile_name(env)
    except Exception:  # noqa: BLE001 — a profile lookup must never break STT
        profile = (env.get("IRA_MODEL_PROFILE") or "").strip()
    cpus = _usable_cpus() if cpus is None else cpus
    if profile == "low_resource" or cpus < _MIN_CPUS_FOR_DEFAULT:
        return LOW_RESOURCE_WHISPER
    return DEFAULT_CPU_WHISPER


def _whisper_source(name: str) -> tuple[str, str | None]:
    """``(repository or local path, revision)`` for a checkpoint name.

    A local directory (an air-gapped, pre-converted model) is loaded as-is: it
    is not fetched, so there is nothing to pin. Every other name must be pinned.
    """
    if os.path.isdir(name):
        return name, None
    key = WHISPER_ARTEFACTS.get(name)
    if key is None:
        raise STTUnavailable(
            f"WHISPER_MODEL={name!r} is not a pinned checkpoint. IRA loads only "
            f"{sorted(WHISPER_ARTEFACTS)} (or a local model directory); to add "
            f"one, pin it in core/models/artefacts.py and allowlist it (MOD-02).")
    from core.models.artefacts import UnpinnedArtefact, identifier_for, revision_for
    try:
        return identifier_for(key), revision_for(key)
    except UnpinnedArtefact as exc:
        raise STTUnavailable(str(exc)) from exc


# Fix #124: thread-safe Whisper model cache — same double-checked locking pattern
# as _load_kokoro in tts.py, preventing duplicate model loads on concurrent calls.
# Fix #129: model download errors are caught and re-raised with a descriptive
# message so operators know to check disk space and network connectivity rather
# than seeing a bare exception from the Hugging Face downloader.
_whisper_model: WhisperModel | None = None
_whisper_loaded: bool = False
_whisper_lock = threading.Lock()


def _load_model(model_size: str, device: str, compute_type: str) -> WhisperModel:
    """Load and cache the Whisper model (Fix #124: thread-safe, Fix #129: clear errors).

    Raises ``STTUnavailable`` (a RuntimeError) when faster-whisper is missing, the
    name is not pinned, or the load failed.
    """
    global _whisper_model, _whisper_loaded
    if WhisperModel is None:
        raise STTUnavailable("faster-whisper is not installed — pip install -r requirements.txt")
    if _whisper_loaded:             # Fast path
        if _whisper_model is None:
            raise STTUnavailable(
                f"Whisper {model_size} is unavailable — check startup logs for the download error."
            )
        return _whisper_model
    with _whisper_lock:
        if _whisper_loaded:         # Race guard
            if _whisper_model is None:
                raise STTUnavailable(
                    f"Whisper {model_size} is unavailable — check startup logs for the download error."
                )
            return _whisper_model
        # Refusing an unpinned name is a configuration error, not a load attempt:
        # raised before `_whisper_loaded` is set, so fixing the env and retrying
        # does not need a restart.
        source, revision = _whisper_source(model_size)
        logger.info(f"Loading Whisper {model_size} ({source}@{revision or 'local'}) "
                    f"on {device} ({compute_type})...")
        try:
            _whisper_model = WhisperModel(
                source, device=device, compute_type=compute_type, revision=revision)
            logger.info("Whisper model loaded.")
        except Exception as e:
            # Fix #129: surface download/init errors with actionable context so the
            # operator knows what to fix (disk space, network, CUDA availability).
            logger.error(
                f"Whisper {model_size} failed to load: {type(e).__name__}. "
                f"Ensure network access is available for the initial model download "
                f"and that the host has sufficient disk space (~3 GB for large-v3)."
            )
            _whisper_model = None
        finally:
            _whisper_loaded = True
        if _whisper_model is None:
            raise STTUnavailable(
                f"Whisper {model_size} is unavailable — see above error for details."
            )
        return _whisper_model


def _downsample(audio_bytes: bytes) -> np.ndarray:
    """Convert 48kHz int16 PCM bytes to 16kHz float32 array for Whisper.

    Uses scipy.signal.resample_poly instead of naive decimation (pcm[::3]).
    Naive slice-based decimation causes aliasing artifacts because it skips
    samples without first applying a low-pass anti-aliasing filter — this
    degrades STT accuracy on high-frequency speech sounds (sibilants, etc.).
    resample_poly applies a polyphase anti-aliasing FIR filter automatically.
    """
    pcm = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        from scipy.signal import resample_poly
        return resample_poly(pcm, up=1, down=DOWNSAMPLE_RATIO).astype(np.float32)
    except ImportError:
        # Graceful fallback if scipy is not installed — log and use basic decimation
        logger.warning("scipy not installed; using aliasing-prone decimation. Add scipy to requirements.")
        return pcm[::DOWNSAMPLE_RATIO]


def _transcribe_sync(
    audio: np.ndarray,
    model: WhisperModel,
    language: str | None,
) -> tuple[str, str, float]:
    """
    Run Whisper transcription synchronously (called from thread pool).
    Returns: (transcript, detected_lang, confidence)
    """
    segments, info = model.transcribe(
        audio,
        language=language,           # None = auto-detect (multilingual)
        beam_size=5,
        vad_filter=True,             # Skip silent segments
        # Tuned so brief pauses / trailing-off don't clip the utterance mid-sentence.
        vad_parameters={
            "min_silence_duration_ms": _VAD_MIN_SILENCE_MS,
            "min_speech_duration_ms": _VAD_MIN_SPEECH_MS,
        },
        word_timestamps=False,
    )
    transcript = " ".join(seg.text.strip() for seg in segments).strip()
    detected_lang = normalise_lang(info.language)
    confidence = float(info.language_probability)
    return transcript, detected_lang, confidence


# ── Sovereign HTTP transcription (browser-native voice; no LiveKit) ────────────
# Browser MediaRecorder posts webm/opus; the acceptance path posts WAV. Both decode
# here. The checkpoint is resolved once, at import, by resolve_whisper_model():
# WHISPER_MODEL wins; otherwise `small` on CPU, `base` on weak machines. It used
# to default to distil-large-v3, which measured ~17–20 s per short utterance on a
# 4-vCPU box and is English-only. For spoken Tamil/Hindi on a fast machine set
# WHISPER_MODEL=large-v3.
DEFAULT_HTTP_WHISPER_MODEL = resolve_whisper_model()


def _decode_to_16k_mono(blob: bytes, max_seconds: float | None = None) -> np.ndarray:
    """Decode an uploaded audio blob (wav / webm-opus / ogg / mp3 …) to 16 kHz mono
    float32 via faster-whisper's PyAV-backed decoder — handles the browser's
    MediaRecorder output and plain WAV alike, resampling to 16 kHz.

    With ``max_seconds``, raises ``AudioTooLong`` instead of decoding past it.
    The whole file is otherwise held in memory: 50 MB of low-bitrate MP3 is
    hours of audio, gigabytes once decoded.
    """
    if max_seconds is None:
        from faster_whisper.audio import decode_audio
        return decode_audio(io.BytesIO(blob), sampling_rate=WHISPER_SAMPLE_RATE)
    return _decode_bounded(blob, max_seconds)


def _decode_bounded(blob: bytes, max_seconds: float) -> np.ndarray:
    """``decode_audio`` with a length limit: the stated length is checked
    first, then the decoded samples are counted as they arrive."""
    import gc

    import av

    limit = int(max_seconds * WHISPER_SAMPLE_RATE)
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=WHISPER_SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    count = 0

    def keep(frames) -> None:
        nonlocal count
        for out in frames:
            samples = out.to_ndarray().reshape(-1)
            count += samples.size
            if count > limit:
                raise AudioTooLong(None, max_seconds)
            chunks.append(samples)

    try:
        with av.open(io.BytesIO(blob), mode="r", metadata_errors="ignore") as container:
            stated = container.duration / av.time_base if container.duration else None
            if stated is not None and stated > max_seconds:
                raise AudioTooLong(stated, max_seconds)
            frames = iter(container.decode(audio=0))
            while True:
                try:
                    frame = next(frames)
                except StopIteration:
                    break
                except av.error.InvalidDataError:   # as decode_audio: skip a bad frame
                    continue
                keep(resampler.resample(frame))
            keep(resampler.resample(None))          # flush
    finally:
        del resampler
        gc.collect()   # decode_audio does the same: PyAV's resampler is not freed otherwise
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe_audio_bytes(
    blob: bytes,
    model_size: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
    max_seconds: float | None = None,
) -> tuple[str, str, float, bytes]:
    """Transcribe an uploaded audio blob locally for POST /api/v1/voice/transcribe.

    Returns (text, detected_lang, confidence, pcm16) where pcm16 is the 16 kHz/16-bit
    mono PCM the biometric owner-gate consumes — so the blob is decoded exactly once
    and the same audio drives both transcription and the owner check.

    Raises ``STTUnavailable`` when the engine cannot run here (→ 503); any other
    exception is about the audio itself (→ 422). With ``max_seconds``, a longer
    recording raises ``AudioTooLong`` before the model runs.
    """
    if not stt_engine_installed():
        raise STTUnavailable("faster-whisper is not installed — pip install -r requirements.txt")
    audio = _decode_to_16k_mono(blob, max_seconds)
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    model = _load_model(model_size or DEFAULT_HTTP_WHISPER_MODEL, device, compute_type)
    text, detected_lang, confidence = _transcribe_sync(audio, model, language)
    return text, detected_lang, confidence, pcm16


class IRAFasterWhisperSTT(_STTBase):
    """
    LiveKit Agents 1.x STT plugin backed by Faster-Whisper (non-streaming/recognize).

    1.x change: the 0.x `recognize()` override + `SpeechEvent`-by-hand path is replaced
    by overriding `_recognize_impl(buffer, *, language, conn_options)`; the base class'
    public `recognize()` wraps it. Multilingual with automatic language detection.

    For lower latency, set WHISPER_MODEL=small (multilingual) or distil-large-v3
    (English-only). Default large-v3 keeps the Indian/EU multilingual support.
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cpu",
        # int8 is fastest on CPU with acceptable accuracy loss
        compute_type: str = "int8",
        language: str | None = None,  # None = auto-detect
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        # 1.x: the AgentSession owns the audio, so the STT stashes each utterance's
        # 16kHz/16-bit mono PCM here for the biometric gate to read (4.4).
        self.last_utterance_pcm16: bytes = b""

    def _get_model(self) -> WhisperModel:
        return _load_model(self._model_size, self._device, self._compute_type)

    async def _recognize_impl(
        self,
        buffer: rtc.AudioFrame | list[rtc.AudioFrame],
        *,
        language: str | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        t0 = time.monotonic()

        # 1.x: collapse the buffered frames into a single frame, then to bytes.
        frame = rtc.combine_audio_frames(buffer)
        audio_array = _downsample(bytes(frame.data))

        # Stash 16kHz/16-bit mono PCM for the biometric gate (4.4) to verify this turn.
        self.last_utterance_pcm16 = (
            np.clip(audio_array, -1.0, 1.0) * 32767.0
        ).astype(np.int16).tobytes()

        if len(audio_array) < 1600:  # < 0.1s — too short, skip
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text="", language="en", confidence=0.0)],
            )

        model = self._get_model()
        lang_hint = language or self._language

        loop = asyncio.get_running_loop()
        transcript, detected_lang, confidence = await loop.run_in_executor(
            _STT_EXECUTOR,
            _transcribe_sync,
            audio_array,
            model,
            lang_hint,
        )

        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"STT: lang={detected_lang} conf={confidence:.2f} "
            f"len={len(transcript)} latency={latency_ms}ms"
        )

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    text=transcript,
                    language=detected_lang,
                    confidence=confidence,
                )
            ],
        )


# ── Standalone smoke test (run on the dev workstation) ─────────────────────────────
# Usage:  python -m voice.stt <path-to-audio.wav>
# Loads the model, transcribes the file, and prints the text. Verifies the
# faster-whisper path end-to-end without LiveKit.
if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m voice.stt <audio.wav>")
        raise SystemExit(2)
    import soundfile as sf  # type: ignore
    wav, sr = sf.read(sys.argv[1], dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != WHISPER_SAMPLE_RATE:
        from scipy.signal import resample_poly
        wav = resample_poly(wav, WHISPER_SAMPLE_RATE, sr).astype(np.float32)
    _model = _load_model("small", "cpu", "int8")
    text, lang, conf = _transcribe_sync(wav.astype(np.float32), _model, None)
    print(f"[{lang} conf={conf:.2f}] {text}")
