"""Audio decoding for speech-to-text without FFmpeg.

IRA's speech recogniser (faster-whisper) wants 16 kHz mono float32 samples. faster-whisper's
own decoder reads files through PyAV, whose wheels bundle FFmpeg built with the GPL encoders
libx264 and libx265. The owner decided that PyAV does not ship (docs/security/LICENCES.md), so
this module decodes what IRA receives with the standard library and numpy only.

What it reads: RIFF WAV (and RF64 for files over 4 GB) holding linear PCM at 8, 16, 24 or 32
bits, or IEEE float at 32 or 64 bits, in any number of channels and at any sample rate from
4 kHz to 384 kHz whose conversion filter stays bounded (every rate a recorder uses; not, for
example, 383,999 Hz), including WAVE_FORMAT_EXTENSIBLE headers with those sub-formats. Channels are
averaged to mono and the rate is converted to 16 kHz with a windowed-sinc polyphase filter.

What sends it audio, and in which format:

* the watch and the phone recognise speech on the device and send text, never audio;
* the dashboard does not record or upload audio;
* the wake-word listener (voice/wakeword.py) wraps 16-bit mono PCM in WAV;
* the lip-sync alignment pass (voice/lipsync.py) reads IRA's own synthesised WAV;
* the LiveKit plugin (voice/stt.py) hands raw 48 kHz PCM straight to the recogniser.

Anything else (MP3, AAC/M4A, Ogg, Opus, WebM, FLAC, video) is refused with
``UnsupportedAudio``, which names the format it found. It is never guessed at: decoding a
compressed file as PCM would hand the recogniser noise, and the recogniser would transcribe
the noise.
"""

from __future__ import annotations

import io
import math
import os
import struct
from typing import BinaryIO

import numpy as np

#: The rate faster-whisper's feature extractor expects.
WHISPER_SAMPLE_RATE = 16_000

_FORMAT_PCM = 0x0001
_FORMAT_IEEE_FLOAT = 0x0003
_FORMAT_EXTENSIBLE = 0xFFFE
#: The first two bytes of the sub-format GUID in a WAVE_FORMAT_EXTENSIBLE header carry the
#: format code; the other 14 bytes are this fixed suffix.
_KSDATAFORMAT_SUFFIX = bytes.fromhex("000000001000800000aa00389b71")

_MIN_RATE = 4_000
_MAX_RATE = 384_000
_MAX_CHANNELS = 32
#: Chunks read before the data chunk. A real WAV has a handful (fmt, fact, LIST, bext, JUNK,
#: ds64); a file of tens of thousands of empty chunks only makes the parser loop.
_MAX_CHUNKS = 1_024
#: The longest conversion filter this module will design (coefficients, all phases). The
#: filter grows with the larger of the two terms of the rate ratio in lowest terms, so a rate
#: the header names, such as 383,999 Hz (ratio 16,000/383,999), would otherwise ask for a
#: 12-million-tap filter: about 1 GB of memory and seconds of CPU before one sample is read
#: (security review S8). Every common rate needs at most 20,480; 4,001 Hz needs 512,000.
_MAX_FILTER_TAPS = 1 << 19

#: Leading bytes of the formats people are likely to send instead of WAV, so the refusal
#: can say what the file is rather than only what it is not.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"fLaC", "a FLAC file"),
    (0, b"OggS", "an Ogg file (Vorbis or Opus)"),
    (0, b"\x1a\x45\xdf\xa3", "a WebM or Matroska file"),
    (0, b"ID3", "an MP3 file"),
    (4, b"ftyp", "an MP4 or M4A file"),
    (0, b"#!AMR", "an AMR file"),
    (0, b"FORM", "an AIFF file"),
    (0, b"caff", "a Core Audio (CAF) file"),
    (0, b"RIFX", "a big-endian WAV (RIFX) file"),
)


class UnsupportedAudio(ValueError):
    """The upload is not audio this server decodes.

    ``kind`` says what it is as a phrase that fits "It is …": "an MP3 file", or "a WAV file
    this server cannot read (8-bit A-law)".
    """

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"unsupported audio: {kind}; send uncompressed WAV (PCM)")


def _bad_wav(detail: str) -> UnsupportedAudio:
    return UnsupportedAudio(f"a WAV file this server cannot read ({detail})")


class AudioTooLong(ValueError):
    """The recording is longer than the caller will transcribe.

    ``seconds`` is its length when it is known, else None.
    """

    def __init__(self, seconds: float | None, limit: float):
        super().__init__(f"audio longer than {limit:.0f} s"
                         + (f" ({seconds:.0f} s)" if seconds is not None else ""))
        self.seconds = seconds
        self.limit = limit


def sniff(blob: bytes) -> str:
    """A short name for what ``blob`` looks like, for messages and logs."""
    head = bytes(blob[:16])
    if head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        return "a WAV file"
    for offset, magic, name in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return name
    # MPEG audio frame sync: 11 set bits. An MP3 without an ID3 tag starts here.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "an MP3 or AAC stream"
    if not head:
        return "an empty file"
    return "a file in a format this server does not recognise"


# ── WAV ──────────────────────────────────────────────────────────────────────

def _chunks(blob: memoryview, start: int, rf64_sizes: dict[bytes, int]):
    """``(chunk id, payload offset, declared size)`` for each RIFF chunk after ``start``."""
    pos = start
    end = len(blob)
    count = 0
    while pos + 8 <= end:
        count += 1
        if count > _MAX_CHUNKS:
            raise _bad_wav(f"more than {_MAX_CHUNKS} chunks before the data")
        cid = bytes(blob[pos:pos + 4])
        size = struct.unpack_from("<I", blob, pos + 4)[0]
        if size == 0xFFFFFFFF and cid in rf64_sizes:
            size = rf64_sizes[cid]
        yield cid, pos + 8, size
        pos += 8 + size + (size & 1)     # chunks are word-aligned


def _parse_wav(blob: bytes) -> tuple[int, int, int, int, memoryview]:
    """``(format code, channels, rate, bits per sample, sample bytes)`` from a WAV file."""
    view = memoryview(blob)
    if len(view) < 12 or bytes(view[8:12]) != b"WAVE" or bytes(view[:4]) not in (b"RIFF", b"RF64"):
        raise UnsupportedAudio(sniff(blob))
    rf64_sizes: dict[bytes, int] = {}
    fmt = None
    data = None
    for cid, off, size in _chunks(view, 12, rf64_sizes):
        if cid == b"ds64" and size >= 24:
            # RF64: the real RIFF, data and sample counts, as 64-bit numbers.
            if off + 16 > len(view):
                raise _bad_wav("a truncated ds64 chunk")
            rf64_sizes[b"data"] = struct.unpack_from("<Q", view, off + 8)[0]
        elif cid == b"fmt ":
            if size < 16 or off + 16 > len(view):
                raise _bad_wav("a truncated format chunk")
            fmt = struct.unpack_from("<HHIIHH", view, off)
            if fmt[0] == _FORMAT_EXTENSIBLE:
                if size < 40 or off + 40 > len(view):
                    raise _bad_wav("a truncated extensible format chunk")
                guid = bytes(view[off + 24:off + 40])
                if guid[2:] != _KSDATAFORMAT_SUFFIX:
                    raise _bad_wav("an unknown sub-format")
                fmt = (struct.unpack_from("<H", guid)[0],) + fmt[1:]
        elif cid == b"data":
            # A recorder that streams its output may leave the size as 0 or 0xFFFFFFFF, or
            # stop before writing all it declared. What is present is what there is.
            stop = len(view) if size in (0, 0xFFFFFFFF) else min(off + size, len(view))
            data = view[off:stop]
            break
    if fmt is None:
        raise _bad_wav("no format chunk")
    if data is None:
        raise _bad_wav("no data chunk")
    code, channels, rate, _byte_rate, _block_align, bits = fmt
    if code == _FORMAT_PCM and bits not in (8, 16, 24, 32):
        raise _bad_wav(f"{bits}-bit PCM")
    if code == _FORMAT_IEEE_FLOAT and bits not in (32, 64):
        raise _bad_wav(f"{bits}-bit float")
    if code not in (_FORMAT_PCM, _FORMAT_IEEE_FLOAT):
        # 0x0002 ADPCM, 0x0006 A-law, 0x0007 mu-law, 0x0055 MP3-in-WAV and the rest.
        raise _bad_wav(f"compressed audio, format code 0x{code:04x}")
    if not 1 <= channels <= _MAX_CHANNELS:
        raise _bad_wav(f"{channels} channels")
    if not _MIN_RATE <= rate <= _MAX_RATE:
        raise _bad_wav(f"{rate} Hz")
    return code, channels, rate, bits, data


def _to_float_mono(code: int, channels: int, bits: int, data: memoryview) -> np.ndarray:
    """Interleaved samples to float32 mono in [-1, 1]."""
    width = bits // 8
    frames = len(data) // (width * channels)
    raw = data[:frames * width * channels]
    if code == _FORMAT_IEEE_FLOAT:
        samples = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    elif bits == 8:          # WAV's 8-bit PCM is unsigned, centred on 128
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif bits == 16:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        samples = ints.astype(np.float32) / 8388608.0
    else:                    # 32-bit integer
        samples = (np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)


# ── Sample-rate conversion ───────────────────────────────────────────────────

def _polyphase_bank(up: int, down: int, half_taps: int = 16) -> tuple[np.ndarray, int]:
    """A Kaiser-windowed sinc low-pass, split into ``up`` phases, and its delay.

    The cut-off sits at the lower of the two Nyquist frequencies, so converting down removes
    what the new rate cannot carry instead of folding it back as aliasing.
    ``bank[p, k]`` is the filter coefficient ``h[p + k*up]``.
    """
    cutoff = 1.0 / max(up, down)                       # as a fraction of the upsampled Nyquist
    taps_per_phase = 2 * half_taps * max(1, math.ceil(down / up))
    length = taps_per_phase * up
    # An odd number of taps, so the centre (the delay) falls on a whole sample; the last
    # slot is a zero that only rounds the length up to whole phases.
    odd = length - 1
    n = np.arange(odd) - (odd - 1) // 2
    h = cutoff * np.sinc(cutoff * n) * np.kaiser(odd, 8.6)
    h = np.append(h * (up / h.sum()), 0.0)             # unity gain after zero-stuffing
    return h.reshape(taps_per_phase, up).T.astype(np.float32), (odd - 1) // 2


def _filter_taps(rate: int, target: int) -> int:
    """How many coefficients ``_polyphase_bank`` would design to convert ``rate`` to ``target``."""
    g = math.gcd(rate, target)
    up, down = target // g, rate // g
    return 2 * 16 * max(1, math.ceil(down / up)) * up


def _check_convertible(rate: int, target: int) -> None:
    """Refuse, before any work, a conversion whose filter would be unreasonably large."""
    if rate != target and _filter_taps(rate, target) > _MAX_FILTER_TAPS:
        raise _bad_wav(f"{rate} Hz, a rate this server does not convert to {target} Hz")


def resample(samples: np.ndarray, rate: int, target: int = WHISPER_SAMPLE_RATE) -> np.ndarray:
    """Convert float32 mono ``samples`` from ``rate`` to ``target`` Hz.

    Rational polyphase conversion: conceptually, insert ``up - 1`` zeros between samples,
    low-pass, keep every ``down``-th sample. Output ``m`` is
    ``sum_k x[base - k] * h[phase + k*up]`` where ``base, phase = divmod(m*down + delay, up)``,
    computed in blocks so a ten-minute recording never needs more than a few MB at once.
    """
    if rate == target or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    _check_convertible(rate, target)
    g = math.gcd(rate, target)
    up, down = target // g, rate // g
    bank, delay = _polyphase_bank(up, down)
    taps = bank.shape[1]
    out_len = (samples.size * up + down - 1) // down
    pad = taps
    padded = np.concatenate([np.zeros(pad, np.float32), samples.astype(np.float32, copy=False),
                             np.zeros(pad, np.float32)])
    out = np.empty(out_len, dtype=np.float32)
    back = np.arange(taps, dtype=np.int64)
    step = max(1024, 2_000_000 // taps)                 # about 8 MB per gathered block
    for start in range(0, out_len, step):
        m = np.arange(start, min(start + step, out_len), dtype=np.int64)
        base, phase = np.divmod(m * down + delay, up)
        window = padded[(base + pad)[:, None] - back[None, :]]
        out[start:start + m.size] = np.einsum("ij,ij->i", window, bank[phase])
    return out


# ── Entry points ─────────────────────────────────────────────────────────────

def decode_to_16k_mono(blob: bytes, max_seconds: float | None = None) -> np.ndarray:
    """Decode an uploaded WAV to 16 kHz mono float32.

    Raises ``UnsupportedAudio`` for anything that is not WAV this module reads, and, with
    ``max_seconds``, ``AudioTooLong`` before converting a longer recording.
    """
    code, channels, rate, bits, data = _parse_wav(blob)
    _check_convertible(rate, WHISPER_SAMPLE_RATE)
    frames = len(data) // ((bits // 8) * channels)
    seconds = frames / rate
    if max_seconds is not None and seconds > max_seconds:
        raise AudioTooLong(seconds, max_seconds)
    return resample(_to_float_mono(code, channels, bits, data), rate, WHISPER_SAMPLE_RATE)


def _read_all(input_file: str | os.PathLike | BinaryIO | bytes) -> bytes:
    if isinstance(input_file, (bytes, bytearray, memoryview)):
        return bytes(input_file)
    if isinstance(input_file, (str, os.PathLike)):
        with open(input_file, "rb") as f:
            return f.read()
    if isinstance(input_file, io.IOBase) or hasattr(input_file, "read"):
        return input_file.read()
    raise TypeError(f"cannot read audio from {type(input_file).__name__}")


def decode_audio(input_file, sampling_rate: int = WHISPER_SAMPLE_RATE, split_stereo: bool = False):
    """faster-whisper's ``decode_audio`` signature, served by this module's WAV reader.

    ``voice.whisper_engine`` installs this in place of ``faster_whisper.audio`` so the
    recogniser never imports PyAV. ``split_stereo`` returns the first two channels separately,
    as faster-whisper's does.
    """
    code, channels, rate, bits, data = _parse_wav(_read_all(input_file))
    _check_convertible(rate, sampling_rate)
    if not split_stereo:
        return resample(_to_float_mono(code, channels, bits, data), rate, sampling_rate)
    if channels < 2:
        raise _bad_wav("one channel, where two were asked for")
    width = bits // 8
    frames = len(data) // (width * channels)
    flat = _to_float_mono(code, 1, bits, data[:frames * width * channels]).reshape(-1, channels)
    return (resample(np.ascontiguousarray(flat[:, 0]), rate, sampling_rate),
            resample(np.ascontiguousarray(flat[:, 1]), rate, sampling_rate))


def pad_or_trim(array, length: int = 3000, *, axis: int = -1):
    """Pad with zeros or cut ``array`` to ``length`` along ``axis`` (the encoder's 3000 frames).

    The same contract as faster-whisper's helper of this name, which its transcriber imports
    from ``faster_whisper.audio``.
    """
    if array.shape[axis] > length:
        array = array.take(indices=range(length), axis=axis)
    if array.shape[axis] < length:
        widths = [(0, 0)] * array.ndim
        widths[axis] = (0, length - array.shape[axis])
        array = np.pad(array, widths)
    return array
