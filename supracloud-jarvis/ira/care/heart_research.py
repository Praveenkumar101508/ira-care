"""IRA Care Heart, research mode: raw PPG and accelerometer in, heart-rate estimates out.

RESEARCH ONLY. This runs the heart-rate estimators on a recorded research signal (an excerpt
of the public PPG-DaLiA dataset) or on a simulated one. It is never a measurement of a person
in care, and the code is built so that it cannot become one (D-086):

* a result's ``source`` is ``simulated`` or ``research_recording``; the research path refuses
  ``device`` input, and a result cannot be constructed with any other source;
* nothing here touches the database: the research route takes no person, opens no
  connection and writes nothing; the readings table's own CHECK constraint
  (``care_vitals.source``) refuses both research sources anyway;
* nothing in the alert path (continuous rules, monitoring, ask-first, escalation, SOS, the
  repository, the worker) imports this module, and a test fails if one starts to.

Why research mode exists: current Wear OS watches give an app finished heart-rate values, not
raw PPG (requirement F3.d, blocked), so the learned candidate selector cannot run in the
product. This module lets the full pipeline (data contract, signal quality, both estimators,
confidence and provenance) be built, tested and shown now, on signals that are clearly not
anyone's vital signs.

Pipeline, per 8 s window (2 s apart, the PPG-DaLiA label windows):

1. **Data contract** (:func:`validate_recording`): sampling rates in range, sample and
   timestamp counts agree, timestamps strictly increasing and consistent with the declared
   rate, accelerometer shape and unit known, PPG and accelerometer overlapping in time. A
   recording that breaks the contract is refused as a whole (:class:`ContractError`).
2. **Windowing by time**, from the later of the two streams' starts (which must be within
   :data:`MAX_START_OFFSET_S` of each other, or the recording is ``misaligned``). A window
   with a gap in either stream is unavailable (``gap``); a window with missing (null)
   samples is ``missing_samples``.
3. **Signal quality**: the classical estimator's input checks (too short, not finite, flat,
   saturated/clipped) are reused, not repeated; the band-limited accelerometer energy is
   measured, and above :data:`MOTION_REJECT_G` the window is ``motion_artefact``. The
   classical spectral concentration is reported as the quality index.
4. **Inference**: :class:`care.heart_learned.LearnedHeartTracker` (which runs the classical
   :class:`care.heart_estimate.HeartTracker` inside it). The learned answer is reported when
   it is reliable. When the learned selector ran but is unsure, the window is "unavailable"
   (``learned_low_confidence``), not answered by the classical estimator (D-094). The classical
   answer, marked as a fallback, is reported only where the learned selector cannot run (no
   model, no accelerometer). Otherwise "unavailable" with the reason. An unavailable window
   never carries a number in ``bpm``.
5. **Provenance** on every result: pipeline version, the learned model's format, SHA-256,
   size and training data, the classical estimator's settings, and where the recording came
   from.

Thresholds here (:data:`MOTION_REJECT_G`, the rate tolerance, the gap factor) were fixed
before the held-out evaluation and were not tuned on it; see docs/ai/heart/MODEL_CARD.md.
"""
from __future__ import annotations

import functools
import json
import math
import resource
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np

from care import heart_estimate as he
from care import heart_learned as hl

PIPELINE_VERSION = "ira-heart-research-1"

#: Every heart reading's provenance is one of these. Only the first two can come from here.
SOURCES = ("simulated", "research_recording", "device")
RESEARCH_SOURCES = frozenset({"simulated", "research_recording"})

NOTICE = ("Research: not a measurement of any person in care. The signal is a recorded research "
          "excerpt or a simulation. Nothing is stored in anyone's record and nothing here can "
          "raise an alert.")

# ── the data contract ────────────────────────────────────────────────────────

WINDOW_S = 8.0
SHIFT_S = 2.0
#: Accepted sampling rates, Hz. The band-pass reaches 3.5 Hz, so both need well over 7 Hz.
PPG_HZ_RANGE = (20.0, 512.0)
ACC_HZ_RANGE = (10.0, 512.0)
#: Timestamps' median interval must match the declared rate within this share.
RATE_TOLERANCE = 0.02
#: An interval longer than this many sample periods is a gap.
GAP_FACTOR = 1.5
#: Accelerometer units and the factor to g.
ACC_UNITS = {"g": 1.0, "m/s2": 1.0 / 9.80665, "e4_raw": 1.0 / 64.0}
#: PPG and accelerometer must start within this many seconds of each other: a larger offset
#: means two clocks, not one recording.
MAX_START_OFFSET_S = 1.0
#: The longest recording one call processes, seconds, and the most samples per stream.
MAX_SECONDS = 600.0
MAX_SAMPLES = 80_000
#: Band-limited (0.5-3.5 Hz) accelerometer RMS, in g, above which a window is a motion
#: artefact. Fixed before evaluation: about the arm swing of brisk walking is 0.2-0.4 g, so
#: 0.5 g rejects only vigorous motion. The estimators still see the window (tracking state).
MOTION_REJECT_G = 0.5

#: Reasons a window has no estimate.
HARD_REASONS = ("gap", "missing_samples")
REASON_TEXT = {
    "gap": "a gap in the signal",
    "misaligned": "PPG and accelerometer not aligned in time",
    "missing_samples": "missing samples",
    "too_short": "too short",
    "not_finite": "samples that are not numbers",
    "flat": "a flat signal",
    "saturated": "a clipped (saturated) signal",
    "acc_too_short": "accelerometer window too short",
    "acc_not_finite": "accelerometer samples that are not numbers",
    "bad_shape": "the wrong signal shape",
    "bad_acc_shape": "the wrong accelerometer shape",
    "motion_artefact": "too much movement",
    "low_confidence": "no reliable peak",
    "no_candidates": "no candidate peaks",
    "learned_low_confidence": "the learned selector was not confident enough (the classical estimate is not reported instead)",
    "learned_no_candidates": "the learned selector found no candidate peak (the classical estimate is not reported instead)",
}
#: When the learned selector ran but gave no reliable answer, the window is unavailable with one of
#: these reasons, never the classical estimate (D-094).
LEARNED_UNSURE = {"low_confidence": "learned_low_confidence", "no_candidates": "learned_no_candidates"}

DATASET = {
    "name": "PPG-DaLiA",
    "source": "https://archive.ics.uci.edu/dataset/495/ppg+dalia",
    "doi": "10.24432/C53890",
    "licence": "CC BY 4.0",
    "citation": "A. Reiss, I. Indlekofer, P. Schmidt, K. Van Laerhoven. Deep PPG: Large-Scale Heart Rate "
                "Estimation with Convolutional Neural Networks. Sensors 2019, 19(14), 3079.",
}


class ContractError(ValueError):
    """The recording breaks the data contract. ``code`` names the rule."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Stream:
    samples: np.ndarray     # (n,) for PPG, (n, 3) for the accelerometer; NaN marks a missing sample
    fs_hz: float
    t: np.ndarray           # seconds, strictly increasing, one per sample
    unit: str


@dataclass(frozen=True)
class Recording:
    source: str
    ppg: Stream
    acc: Optional[Stream]
    acc_scale_g: float
    name: Optional[str] = None
    reference_bpm: Optional[np.ndarray] = None   # one per window, when the recording has one
    info: dict = field(default_factory=dict)


def _rate(value, lo_hi, what) -> float:
    try:
        fs = float(value)
    except (TypeError, ValueError):
        raise ContractError("bad_rate", f"{what} sampling rate must be a number") from None
    if not math.isfinite(fs) or not lo_hi[0] <= fs <= lo_hi[1]:
        raise ContractError("bad_rate", f"{what} sampling rate must be {lo_hi[0]:g} to {lo_hi[1]:g} Hz")
    return fs


def _samples(values, what, axes: Optional[int], max_samples: Optional[int]) -> np.ndarray:
    try:
        if isinstance(values, np.ndarray):
            x = values.astype(np.float64)
        else:
            x = np.array([np.nan if v is None else v for v in values] if axes is None else
                         [[np.nan if c is None else c for c in row] for row in values], dtype=np.float64)
    except (TypeError, ValueError):
        raise ContractError("bad_samples", f"{what} samples must be numbers") from None
    if axes is None and x.ndim != 1:
        raise ContractError("bad_samples", f"{what} samples must be a list of numbers")
    if axes is not None and (x.ndim != 2 or x.shape[1] != axes):
        raise ContractError("bad_samples", f"{what} samples must be rows of {axes} axes")
    if max_samples is not None and len(x) > max_samples:
        raise ContractError("too_long", f"{what} has more than {max_samples} samples")
    if np.isinf(x).any():
        raise ContractError("bad_samples", f"{what} samples must be finite or null")
    return x


def _timeline(spec: Mapping, n: int, fs: float, what: str) -> np.ndarray:
    ts = spec.get("timestamps_s")
    if ts is None:
        t0 = float(spec.get("t0_s", 0.0) or 0.0)
        if not math.isfinite(t0):
            raise ContractError("bad_timestamps", f"{what} start time must be finite")
        return t0 + np.arange(n) / fs
    t = np.asarray(ts, dtype=np.float64)
    if t.ndim != 1 or len(t) != n:
        raise ContractError("timestamp_count", f"{what} needs one timestamp per sample ({n}), got {len(t)}")
    if not np.all(np.isfinite(t)):
        raise ContractError("bad_timestamps", f"{what} timestamps must be finite")
    d = np.diff(t)
    if np.any(d <= 0):
        raise ContractError("not_monotonic", f"{what} timestamps must be strictly increasing")
    if len(d):
        median = float(np.median(d))
        if abs(median * fs - 1.0) > RATE_TOLERANCE:
            raise ContractError("rate_mismatch", f"{what} timestamps say {1.0 / median:.2f} Hz, declared {fs:g} Hz")
    return t


def validate_recording(rec: Mapping[str, Any], *, name: Optional[str] = None,
                       max_seconds: Optional[float] = MAX_SECONDS, max_samples: Optional[int] = MAX_SAMPLES) -> Recording:
    """Check a recording against the data contract and return it as arrays.

    ``rec``: ``source`` (``simulated`` or ``research_recording``), ``ppg`` (``fs_hz``,
    ``samples``, optional ``unit``, ``t0_s`` or ``timestamps_s``), optional ``acc`` (the same,
    with rows of three axes and ``unit`` in :data:`ACC_UNITS`) and optional ``reference``
    (``bpm``: one reference heart rate per window, for recorded data that has one).
    ``max_seconds`` and ``max_samples`` bound one call (None lifts them, for offline evaluation).
    """
    source = rec.get("source")
    if source == "device":
        raise ContractError("device_source", "the research pipeline does not take device data")
    if source not in RESEARCH_SOURCES:
        raise ContractError("bad_source", "source must be simulated or research_recording")
    p = rec.get("ppg")
    if not isinstance(p, Mapping) or "samples" not in p:
        raise ContractError("no_ppg", "a PPG stream is required")
    p_fs = _rate(p.get("fs_hz"), PPG_HZ_RANGE, "PPG")
    p_x = _samples(p["samples"], "PPG", None, max_samples)
    ppg = Stream(p_x, p_fs, _timeline(p, len(p_x), p_fs, "PPG"), str(p.get("unit") or "arbitrary"))
    acc, scale = None, 1.0
    a = rec.get("acc")
    if a is not None:
        if not isinstance(a, Mapping) or "samples" not in a:
            raise ContractError("bad_acc", "the accelerometer stream needs samples")
        unit = str(a.get("unit") or "g")
        if unit not in ACC_UNITS:
            raise ContractError("bad_unit", f"accelerometer unit must be one of {sorted(ACC_UNITS)}")
        scale = ACC_UNITS[unit]
        a_fs = _rate(a.get("fs_hz"), ACC_HZ_RANGE, "accelerometer")
        a_x = _samples(a["samples"], "accelerometer", 3, max_samples)
        acc = Stream(a_x, a_fs, _timeline(a, len(a_x), a_fs, "accelerometer"), unit)
    for s, what in ((ppg, "PPG"), (acc, "accelerometer")):
        if max_seconds is not None and s is not None and len(s.t) and s.t[-1] - s.t[0] > max_seconds:
            raise ContractError("too_long", f"{what} is longer than {max_seconds:g} s")
    if acc is not None and len(ppg.t) and len(acc.t) and abs(float(ppg.t[0]) - float(acc.t[0])) > MAX_START_OFFSET_S:
        raise ContractError("misaligned", f"PPG and accelerometer start {abs(float(ppg.t[0]) - float(acc.t[0])):.2f} s "
                                          f"apart (at most {MAX_START_OFFSET_S:g} s): not one timebase")
    start, end = _span(ppg, acc)
    if end - start < WINDOW_S - 1e-9:
        raise ContractError("too_short" if acc is None or _overlaps(ppg, acc) else "no_overlap",
                            f"the streams must overlap for at least one {WINDOW_S:g} s window")
    ref = None
    r = rec.get("reference")
    if isinstance(r, Mapping) and r.get("bpm") is not None:
        ref = np.array([np.nan if v is None else v for v in r["bpm"]], dtype=np.float64)
    info = {k: rec[k] for k in ("label", "dataset", "licence", "attribution", "changes", "excerpt", "note")
            if k in rec}
    if ref is not None:
        info["reference_kind"] = str(r.get("kind") or "supplied with the recording")
    return Recording(source, ppg, acc, scale, name=name, reference_bpm=ref, info=info)


def _overlaps(ppg: Stream, acc: Stream) -> bool:
    return min(ppg.t[-1], acc.t[-1]) > max(ppg.t[0], acc.t[0])


def _span(ppg: Stream, acc: Optional[Stream]) -> tuple[float, float]:
    start = float(ppg.t[0]) if len(ppg.t) else 0.0
    end = float(ppg.t[-1] + 1.0 / ppg.fs_hz) if len(ppg.t) else 0.0
    if acc is not None and len(acc.t):
        start = max(start, float(acc.t[0]))
        end = min(end, float(acc.t[-1] + 1.0 / acc.fs_hz))
    return start, end


def window_starts(rec: Recording) -> np.ndarray:
    start, end = _span(rec.ppg, rec.acc)
    n = int(math.floor((end - start - WINDOW_S) / SHIFT_S + 1e-9)) + 1
    return start + SHIFT_S * np.arange(max(n, 0))


def _cut(s: Stream, t0: float):
    """The samples of ``s`` in [t0, t0 + WINDOW_S), and whether the window has a gap."""
    half = 0.5 / s.fs_hz
    i0 = int(np.searchsorted(s.t, t0 - half, "left"))
    i1 = int(np.searchsorted(s.t, t0 + WINDOW_S - half, "left"))
    x, t = s.samples[i0:i1], s.t[i0:i1]
    expected = int(round(WINDOW_S * s.fs_hz))
    gap = (len(t) < expected * (1 - RATE_TOLERANCE) or len(t) == 0
           or (len(t) > 1 and float(np.max(np.diff(t))) > GAP_FACTOR / s.fs_hz)
           or float(t[0]) - t0 > GAP_FACTOR / s.fs_hz
           or (t0 + WINDOW_S) - float(t[-1]) > (GAP_FACTOR + 1) / s.fs_hz)
    return x, t, gap


# ── signal quality ───────────────────────────────────────────────────────────

def motion_rms_g(acc_g: np.ndarray, acc_hz: float) -> float:
    """Band-limited (the heart-rate band) accelerometer RMS in g: the motion energy."""
    bp = np.stack([he.bandpass_fft(acc_g[:, j], acc_hz) for j in range(acc_g.shape[1])], axis=1)
    return float(np.sqrt(np.mean(np.sum(bp ** 2, axis=1))))


def clipped_share(x: np.ndarray) -> float:
    """Share of samples on the window's minimum or maximum (1.0 for a flat window)."""
    lo, hi = float(np.min(x)), float(np.max(x))
    tol = 1e-6 * (hi - lo)
    return float((np.count_nonzero(x <= lo + tol) + np.count_nonzero(x >= hi - tol)) / max(len(x), 1))


# ── provenance ───────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=4)
def _load(path: str):
    try:
        ens, sha, size = hl.read_model(path)
    except FileNotFoundError:
        return None, {"available": False, "reason": "no model file"}
    except (hl.ModelError, OSError) as e:
        return None, {"available": False, "reason": f"model refused: {e}"}
    meta = ens.meta
    return ens, {
        "available": True,
        "version": f"heart_selector@{sha[:12]}",
        "format": meta.get("format"),
        "sha256": sha,
        "bytes": size,
        "trees": len(ens.trees),
        "trained_on": meta.get("trained_on"),
        "dataset": DATASET["name"],
        "dataset_licence": DATASET["licence"],
        "min_probability": hl.MIN_PROBABILITY,
    }


def model_provenance(path: Optional[str] = None):
    """The learned model (or None) and its provenance record."""
    return _load(str(path or hl.DEFAULT_MODEL_PATH))


def classical_provenance() -> dict:
    return {"module": "care.heart_estimate", "trained_parameters": 0, "min_confidence": he.MIN_CONFIDENCE,
            "band_hz": list(he.BAND_HZ), "motion_gate_g": he.MOTION_GATE_G}


# ── results ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowResult:
    index: int
    t_start_s: float
    status: str                       # "estimate" or "unavailable"
    bpm: Optional[float]              # None unless status == "estimate"
    method: Optional[str]             # "learned" or "classical" when there is an estimate
    confidence: Optional[float]
    confidence_kind: Optional[str]    # "probability_within_5_bpm" or "spectral_concentration"
    reason: Optional[str]             # why unavailable
    fallback: Optional[str]           # why the classical answer was used instead of the learned one
    quality: dict
    classical: Optional[dict]
    learned: Optional[dict]
    reference_bpm: Optional[float]
    processing_ms: float

    def __post_init__(self):
        if self.status not in ("estimate", "unavailable"):
            raise ValueError("status must be estimate or unavailable")
        if (self.status == "estimate") != (self.bpm is not None):
            raise ValueError("an estimate has a number; an unavailable window never does")
        if self.status == "unavailable" and not self.reason:
            raise ValueError("an unavailable window says why")

    def to_dict(self) -> dict:
        return {
            "index": self.index, "t_start_s": round(self.t_start_s, 3), "t_end_s": round(self.t_start_s + WINDOW_S, 3),
            "status": self.status, "bpm": self.bpm, "method": self.method,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "confidence_kind": self.confidence_kind, "reason": self.reason,
            "reason_text": REASON_TEXT.get(self.reason) if self.reason else None,
            "fallback": self.fallback, "quality": self.quality, "classical": self.classical, "learned": self.learned,
            "reference_bpm": self.reference_bpm, "processing_ms": round(self.processing_ms, 3),
        }


@dataclass(frozen=True)
class ResearchResult:
    """One recording's research result. Never a vital sign: see the module docstring."""

    source: str
    windows: tuple[WindowResult, ...]
    provenance: dict
    contract: dict
    resources: dict
    preview: dict
    name: Optional[str] = None
    is_measurement: bool = False
    may_alert: bool = False

    def __post_init__(self):
        if self.source not in RESEARCH_SOURCES:
            raise ValueError(f"a research result's source is simulated or research_recording, not {self.source!r}")
        if self.is_measurement or self.may_alert:
            raise ValueError("a research result is never a measurement and never alerts")

    def summary(self) -> dict:
        w = self.windows
        est = [x for x in w if x.status == "estimate"]
        reasons: dict[str, int] = {}
        for x in w:
            if x.reason:
                reasons[x.reason] = reasons.get(x.reason, 0) + 1
        out = {"windows": len(w), "estimates": len(est), "unavailable": len(w) - len(est),
               "unavailable_by_reason": reasons,
               "learned_estimates": sum(1 for x in est if x.method == "learned"),
               "classical_fallbacks": sum(1 for x in est if x.method == "classical")}
        refs = [(x, x.reference_bpm) for x in w if x.reference_bpm is not None]
        if refs:
            def mae(vals):
                e = [abs(v - r) for v, r in vals if v is not None]
                return (round(float(np.mean(e)), 2), len(e)) if e else (None, 0)
            out["against_reference"] = {
                "note": f"against the recording's own reference ({self.provenance.get('recording', {}).get('reference_kind')}); "
                        "a research check, not accuracy on a person",
                "reported": dict(zip(("mae_bpm", "windows"), mae([(x.bpm, r) for x, r in refs]))),
                "classical_all_windows": dict(zip(("mae_bpm", "windows"), mae(
                    [((x.classical or {}).get("candidate_bpm"), r) for x, r in refs]))),
                "learned_all_windows": dict(zip(("mae_bpm", "windows"), mae(
                    [((x.learned or {}).get("candidate_bpm"), r) for x, r in refs]))),
            }
        return out

    def to_dict(self) -> dict:
        return {
            "kind": "heart_research_result",
            "source": self.source, "name": self.name,
            "is_measurement": False, "may_alert": False, "stored": False,
            "notice": NOTICE,
            "provenance": self.provenance, "contract": self.contract, "resources": self.resources,
            "summary": self.summary(), "windows": [x.to_dict() for x in self.windows], "preview": self.preview,
        }


def _num(v, nd=1):
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


def _preview(rec: Recording, max_points: int = 1200) -> dict:
    """A light, downsampled copy of the signal for a chart (min/max per bucket keeps peaks)."""
    x, t = rec.ppg.samples, rec.ppg.t
    step = max(1, int(math.ceil(len(x) / max_points)))
    pts = []
    for i in range(0, len(x), step):
        seg = x[i:i + step]
        v = float(np.nanmean(seg)) if np.isfinite(seg).any() else None
        pts.append([round(float(t[i]), 3), None if v is None else round(v, 3)])
    out = {"ppg": pts, "ppg_unit": rec.ppg.unit}
    if rec.acc is not None:
        a, ta = rec.acc.samples * rec.acc_scale_g, rec.acc.t
        mag = np.sqrt(np.sum(a ** 2, axis=1))
        step = max(1, int(math.ceil(len(mag) / max_points)))
        out["acc_magnitude_g"] = [[round(float(ta[i]), 3),
                                   None if not np.isfinite(mag[i:i + step]).any() else round(float(np.nanmean(mag[i:i + step])), 4)]
                                  for i in range(0, len(mag), step)]
    return out


_NO_MODEL = Path("/nonexistent/ira-heart-research-no-model.json")


def run(rec: Recording, *, model_path: Optional[str] = None, use_learned: bool = True,
        measure_memory: bool = False) -> ResearchResult:
    """Run the research pipeline over every window of a validated recording."""
    if rec.source not in RESEARCH_SOURCES:     # a Recording built by hand, not by validate_recording
        raise ContractError("bad_source", "the research pipeline takes simulated or research recordings only")
    model, model_prov = model_provenance(model_path) if use_learned else (None, {"available": False,
                                                                                "reason": "learned path switched off"})
    acc_hz = rec.acc.fs_hz if rec.acc is not None else he.ACC_HZ
    tracker = hl.LearnedHeartTracker(model if model is not None else _NO_MODEL,
                                     bvp_hz=rec.ppg.fs_hz, acc_hz=acc_hz, acc_scale_g=rec.acc_scale_g)
    tracker.model = model    # exactly the model whose provenance is reported, or none
    if measure_memory:
        tracemalloc.start()
    t_all = time.perf_counter()
    out = []
    starts = window_starts(rec)
    for k, t0 in enumerate(starts):
        t_w = time.perf_counter()
        x, _, gap = _cut(rec.ppg, float(t0))
        a = None
        hard = "gap" if gap else None
        if rec.acc is not None:
            a, _, agap = _cut(rec.acc, float(t0))
            if not hard and agap:
                hard = "gap"
        if not hard and (np.isnan(x).any() or (a is not None and np.isnan(a).any())):
            hard = "missing_samples"
        ref = None
        if rec.reference_bpm is not None and k < len(rec.reference_bpm) and np.isfinite(rec.reference_bpm[k]):
            ref = round(float(rec.reference_bpm[k]), 2)
        if hard:
            out.append(WindowResult(k, float(t0), "unavailable", None, None, None, None, hard, None,
                                    {"usable": False, "reason": hard}, None, None, ref,
                                    1000 * (time.perf_counter() - t_w)))
            continue
        motion = motion_rms_g(np.asarray(a, float) * rec.acc_scale_g, acc_hz) if a is not None and len(a) else None
        quality = {"motion_rms_g": _num(motion, 4), "clipped_share": _num(clipped_share(x), 4) if len(x) else None,
                   "accelerometer": a is not None}
        r = tracker.update(x, a)
        c = r.classical
        classical = None if c is None else {
            "bpm": c.bpm, "candidate_bpm": c.candidate_bpm, "confidence": _num(c.confidence, 4),
            "reason": c.reason, "motion_suppressed": c.motion_suppressed}
        learned = None if r.method != "learned" else {
            "bpm": r.bpm, "candidate_bpm": r.candidate_bpm, "probability": _num(r.confidence, 4), "reason": r.reason}
        refused = c.reason if c is not None and c.reason and c.reason != "low_confidence" else None
        quality["spectral_concentration"] = _num(c.confidence, 4) if c is not None and not refused else None
        reason = refused or ("motion_artefact" if motion is not None and motion > MOTION_REJECT_G else None)
        quality["usable"] = reason is None
        if reason:
            quality["reason"] = reason
            out.append(WindowResult(k, float(t0), "unavailable", None, None, None, None, reason, None, quality,
                                    classical, learned, ref, 1000 * (time.perf_counter() - t_w)))
            continue
        if r.method == "learned" and r.reliable:
            res = ("estimate", r.bpm, "learned", r.confidence, "probability_within_5_bpm", None, None)
        elif r.method == "learned":
            # The learned selector ran and is unsure (or found no candidate). The classical answer is
            # NOT reported in its place (D-094): on held-out PPG-DaLiA and on GalaxyPPG those answers
            # were 16 to 39 bpm out. The classical candidate stays in ``classical`` for inspection.
            res = ("unavailable", None, None, None, None, LEARNED_UNSURE.get(r.reason, "learned_low_confidence"), None)
        elif c is not None and c.reliable:
            # Only where the learned selector cannot run at all: no model, or no accelerometer.
            why = "no_model" if model is None else "no_accelerometer" if a is None else "learned_unavailable"
            res = ("estimate", c.bpm, "classical", c.confidence, "spectral_concentration", None, why)
        else:
            res = ("unavailable", None, None, None, None, r.reason or "low_confidence", None)
        out.append(WindowResult(k, float(t0), *res, quality, classical, learned, ref, 1000 * (time.perf_counter() - t_w)))
    total = time.perf_counter() - t_all
    resources = {"total_ms": round(1000 * total, 2),
                 "per_window_ms_mean": round(1000 * total / max(len(out), 1), 3),
                 "per_window_ms_max": round(max((w.processing_ms for w in out), default=0.0), 3),
                 "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    if measure_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        resources["python_peak_alloc_mib"] = round(peak / (1024 * 1024), 3)
    rec_prov = {"source": rec.source, "sample": rec.name, **rec.info}
    provenance = {"pipeline": PIPELINE_VERSION, "learned_model": model_prov,
                  "classical": classical_provenance(), "recording": rec_prov}
    contract = {"window_s": WINDOW_S, "shift_s": SHIFT_S, "ppg_hz": rec.ppg.fs_hz, "ppg_unit": rec.ppg.unit,
                "acc_hz": rec.acc.fs_hz if rec.acc else None, "acc_unit": rec.acc.unit if rec.acc else None,
                "acc_scale_g": rec.acc_scale_g, "motion_reject_g": MOTION_REJECT_G,
                "seconds": round(float(_span(rec.ppg, rec.acc)[1] - _span(rec.ppg, rec.acc)[0]), 3)}
    return ResearchResult(rec.source, tuple(out), provenance, contract, resources, _preview(rec), name=rec.name)


# ── built-in samples ─────────────────────────────────────────────────────────

SAMPLES_DIR = Path(__file__).resolve().parent / "research_samples"
MAX_SAMPLE_BYTES = 1024 * 1024
SAMPLE_FORMAT = "ira-research-recording-v1"


def simulate(bpm: float = 72.0, seconds: float = 60.0, *, ppg_hz: float = 64.0, acc_hz: float = 32.0,
             swing_hz: Optional[float] = None, swing_g: float = 0.4, artefact: float = 3.0, seed: int = 0,
             faults: bool = False) -> dict:
    """A simulated wrist recording (a pulse with its second harmonic, noise, optional arm swing).

    ``faults=True`` adds, in order: a flat stretch, a clipped stretch, missing samples, a gap
    in the timestamps and vigorous motion, so every "unavailable" reason can be seen.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * ppg_hz)) / ppg_hz
    f = bpm / 60.0
    ppg = np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t + 0.5) + 0.05 * rng.standard_normal(len(t))
    ta = np.arange(int(seconds * acc_hz)) / acc_hz
    acc = np.stack([np.zeros_like(ta), np.zeros_like(ta), np.ones_like(ta)], axis=1) + 0.005 * rng.standard_normal((len(ta), 3))
    if swing_hz:
        ppg = ppg + artefact * np.sin(2 * np.pi * swing_hz * t)
        acc[:, 0] += swing_g * np.sin(2 * np.pi * swing_hz * ta)
        acc[:, 1] += 0.4 * swing_g * np.sin(2 * np.pi * swing_hz * ta + 1.0)
    p_list: list = [round(float(v), 4) for v in ppg]
    a_list: list = [[round(float(v), 4) for v in row] for row in acc]
    p_ts = None
    if faults:
        def seg(a, b, hz):
            return slice(int(a * hz), int(b * hz))
        for i in range(*seg(10, 20, ppg_hz).indices(len(p_list))):
            p_list[i] = 0.5                                             # flat
        for i in range(*seg(22, 32, ppg_hz).indices(len(p_list))):
            p_list[i] = max(-0.4, min(0.4, p_list[i]))                   # clipped at the rails
        for i in range(*seg(34, 35, ppg_hz).indices(len(p_list))):
            p_list[i] = None                                            # missing samples
        fast = 1.5 * np.sin(2 * np.pi * 1.2 * ta)                       # vigorous motion
        for i in range(*seg(50, 60, acc_hz).indices(len(a_list))):
            a_list[i][0] = round(a_list[i][0] + float(fast[i]), 4)
        p_ts = [round(float(v), 6) for v in t]
        cut = seg(40, 42, ppg_hz)                                       # a 2 s gap in the timestamps
        del p_list[cut], p_ts[cut]
    rec = {"source": "simulated",
           "ppg": {"fs_hz": ppg_hz, "unit": "arbitrary", "samples": p_list},
           "acc": {"fs_hz": acc_hz, "unit": "g", "samples": a_list},
           "reference": {"bpm": [bpm] * max(0, int((seconds - WINDOW_S) // SHIFT_S) + 1),
                         "kind": "the simulated pulse rate"}}
    if p_ts is not None:
        rec["ppg"]["timestamps_s"] = p_ts
    return rec


SIMULATED = {
    "simulated_rest": {"title": "Simulated: still wrist, 68 bpm", "make": lambda: simulate(68.0, 60.0, seed=1)},
    "simulated_arm_swing": {"title": "Simulated: arm swing at 1.6 Hz, 75 bpm",
                            "make": lambda: simulate(75.0, 60.0, swing_hz=1.6, seed=2)},
    "simulated_faults": {"title": "Simulated: flat, clipped, missing, gap and heavy motion",
                         "make": lambda: simulate(70.0, 60.0, seed=3, faults=True)},
}


def _recorded_samples() -> dict[str, Path]:
    return {p.stem: p for p in sorted(SAMPLES_DIR.glob("*.json"))}


def list_samples() -> list[dict]:
    out = []
    for name, p in _recorded_samples().items():
        d = _read_sample_file(p)
        out.append({"name": name, "source": "research_recording", "title": d.get("label", name),
                    "seconds": d.get("excerpt", {}).get("seconds"), "dataset": d.get("dataset"),
                    "licence": d.get("licence"), "attribution": d.get("attribution")})
    for name, s in SIMULATED.items():
        out.append({"name": name, "source": "simulated", "title": s["title"], "seconds": 60.0,
                    "dataset": None, "licence": None, "attribution": None})
    return out


@functools.lru_cache(maxsize=8)
def _read_sample_file(p: Path) -> dict:
    raw = p.read_bytes()
    if len(raw) > MAX_SAMPLE_BYTES:
        raise ContractError("too_long", f"sample {p.name} is over {MAX_SAMPLE_BYTES} bytes")
    d = json.loads(raw.decode("utf-8"))
    if d.get("format") != SAMPLE_FORMAT or d.get("source") != "research_recording":
        raise ContractError("bad_sample", f"sample {p.name} is not a {SAMPLE_FORMAT} research recording")
    return d


def load_sample(name: str) -> Recording:
    """A built-in sample by name, validated like any other recording."""
    if name in SIMULATED:
        return validate_recording(SIMULATED[name]["make"](), name=name)
    files = _recorded_samples()
    if name not in files:
        raise KeyError(name)
    return validate_recording(_read_sample_file(files[name]), name=name)
