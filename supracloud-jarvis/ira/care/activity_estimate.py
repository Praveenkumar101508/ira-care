"""IRA Care Activity: what the wrist is doing, from one window of the watch's accelerometer
and gyroscope.

A wellbeing context signal only ("mostly sitting this afternoon", "walked after lunch").
It is NOT a medical device, it does not diagnose, it is not a fall detector, and no alert
or alarm may be raised on its output alone. Accuracy was measured on the public WISDM
Smartphone and Smartwatch Activity and Biometrics dataset (51 adult volunteers, an LG G
Watch on the dominant wrist), never on the IRA watch and never with older adults; see
docs/ai/activity/MODEL_CARD.md for the numbers and what is still to be validated.

It depends only on numpy. The trained weights are a small multilayer perceptron stored as
JSON (``care/models/activity_model.json``); nothing is unpickled.

1. Refuse input that cannot carry an activity: wrong shape, not finite, too short, flat
   (a stuck sensor), saturated (clipped at full scale) or with an implausible gravity
   magnitude (wrong units). A refusal returns ``activity=None`` with a reason; the
   function never guesses.
2. Resample both sensors to 20 Hz by linear interpolation and keep the most recent 10 s.
3. Compute hand-made features (time-domain statistics, band powers, dominant frequency,
   spectral entropy, periodicity, axis correlations, the gravity direction).
4. Standardise, run the MLP, softmax.
5. Below ``min_confidence`` the answer is ``"unknown"`` (not confident); the most likely
   class is still reported as ``candidate``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

#: The rate the features are computed at (the WISDM watch sensors were polled at 20 Hz).
FS_HZ = 20.0
#: Window length the model was trained on, seconds. Longer input keeps the last 10 s.
WINDOW_S = 10.0
#: Shortest window accepted, seconds.
MIN_SECONDS = 8.0
#: FFT length for the spectral features (zero-padded).
NFFT = 256
#: Frequency bands for the spectral features, Hz.
BANDS_HZ = ((0.3, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 10.0))
#: Lags searched for the periodicity feature, seconds (steps and arm swings).
AUTOCORR_LAGS_S = (0.3, 2.0)
#: Mean accelerometer magnitude must lie in this range, m/s^2 (gravity is 9.8). A watch
#: reporting in g (about 1.0) or in milli-g (about 1000) is refused rather than misread.
#: The upper bound is wide because vigorous motion raises the mean magnitude: on WISDM,
#: jogging windows reach 38 m/s^2 (a first bound of 16 refused 169 genuine windows).
GRAVITY_RANGE = (4.0, 50.0)
#: A sensor whose every axis varies by less than this is stuck (a real sensor at rest
#: still shows noise). Accelerometer m/s^2, gyroscope rad/s.
FLAT_ACC = 1e-6
FLAT_GYRO = 1e-7
#: Saturation: more than this share of one axis's samples sit exactly on that axis's
#: extreme, and the extreme is large (so a quiet, quantised signal at rest is not counted).
SATURATION_SHARE = 0.10
SATURATION_MIN_ACC = 19.6   # 2 g, m/s^2
SATURATION_MIN_GYRO = 5.0   # rad/s
#: Below this probability the answer is "unknown". Set before the evaluation was run, not
#: tuned on it; the coverage table in docs/ai/activity/results.json shows the trade-off.
MIN_CONFIDENCE = 0.5

UNKNOWN = "unknown"
MODEL_PATH = Path(__file__).resolve().parent / "models" / "activity_model.json"
MODEL_FORMAT = "ira-care-activity-mlp/1"

_SIGNALS = ("acc_x", "acc_y", "acc_z", "acc_mag", "gyro_x", "gyro_y", "gyro_z", "gyro_mag")
_PER_SIGNAL = (["mean", "std", "p10", "p50", "p90", "mean_abs_diff"]
               + [f"logpow_{lo:g}_{hi:g}hz" for lo, hi in BANDS_HZ]
               + ["dominant_hz", "spectral_entropy", "autocorr_peak"])
FEATURE_NAMES = tuple([f"{s}_{f}" for s in _SIGNALS for f in _PER_SIGNAL]
                      + ["gravity_x", "gravity_y", "gravity_z",
                         "acc_corr_xy", "acc_corr_xz", "acc_corr_yz",
                         "gyro_corr_xy", "gyro_corr_xz", "gyro_corr_yz"])


@dataclass(frozen=True)
class ActivityEstimate:
    """One window's answer.

    ``activity`` is a class name, ``"unknown"`` when the model is not confident, or None
    when the input was refused (no estimate at all; ``reason`` says why)."""

    activity: Optional[str]
    confidence: float
    reason: Optional[str] = None
    candidate: Optional[str] = None
    probabilities: dict = field(default_factory=dict)

    @property
    def reliable(self) -> bool:
        return self.activity is not None and self.activity != UNKNOWN


def _refuse(reason: str) -> ActivityEstimate:
    return ActivityEstimate(activity=None, confidence=0.0, reason=reason)


# ── input checks and resampling ──────────────────────────────────────────────

def _as_xyz(x) -> Optional[np.ndarray]:
    try:
        a = np.asarray(x, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if a.ndim != 2 or a.shape[1] != 3:
        return None
    return a


def _resample(x: np.ndarray, fs: float) -> np.ndarray:
    if abs(fs - FS_HZ) < 1e-9:
        return x
    t_in = np.arange(len(x)) / fs
    n_out = int(np.floor((len(x) - 1) / fs * FS_HZ)) + 1
    t_out = np.arange(n_out) / FS_HZ
    return np.stack([np.interp(t_out, t_in, x[:, j]) for j in range(3)], axis=1)


def _is_saturated(x: np.ndarray, min_extreme: float) -> bool:
    for j in range(3):
        col = x[:, j]
        for ext in (col.max(), col.min()):
            if abs(ext) >= min_extreme and np.mean(col == ext) > SATURATION_SHARE:
                return True
    return False


def check_window(accel, gyro, *, fs_hz: float = FS_HZ, gyro_fs_hz: Optional[float] = None):
    """Return ``(acc, gyro, None)`` resampled to 20 Hz and cut to the last 10 s, or
    ``(None, None, reason)`` when the window is refused."""
    gyro_fs_hz = fs_hz if gyro_fs_hz is None else gyro_fs_hz
    if not (np.isfinite(fs_hz) and np.isfinite(gyro_fs_hz) and fs_hz > 0 and gyro_fs_hz > 0):
        return None, None, "bad_rate"
    a, g = _as_xyz(accel), _as_xyz(gyro)
    if a is None:
        return None, None, "bad_accelerometer_shape"
    if g is None:
        return None, None, "bad_gyroscope_shape"
    if not (np.isfinite(a).all() and np.isfinite(g).all()):
        return None, None, "not_finite"
    if len(a) / fs_hz < MIN_SECONDS or len(g) / gyro_fs_hz < MIN_SECONDS:
        return None, None, "too_short"
    if np.ptp(a, axis=0).max() < FLAT_ACC:
        return None, None, "flat_accelerometer"
    if np.ptp(g, axis=0).max() < FLAT_GYRO:
        return None, None, "flat_gyroscope"
    if _is_saturated(a, SATURATION_MIN_ACC):
        return None, None, "saturated_accelerometer"
    if _is_saturated(g, SATURATION_MIN_GYRO):
        return None, None, "saturated_gyroscope"
    gravity = float(np.linalg.norm(a, axis=1).mean())
    if not GRAVITY_RANGE[0] <= gravity <= GRAVITY_RANGE[1]:
        return None, None, "implausible_gravity"
    a, g = _resample(a, fs_hz), _resample(g, gyro_fs_hz)
    n = min(len(a), len(g), int(round(WINDOW_S * FS_HZ)))
    if n < int(np.ceil(MIN_SECONDS * FS_HZ)) - 1:
        return None, None, "too_short"
    return a[-n:], g[-n:], None


# ── features ─────────────────────────────────────────────────────────────────

def _signal_features(s: np.ndarray) -> np.ndarray:
    """``s`` is (n, k): k signals sampled at 20 Hz. Returns (k, len(_PER_SIGNAL))."""
    n = len(s)
    mean = s.mean(0)
    std = s.std(0)
    p10, p50, p90 = np.percentile(s, [10, 50, 90], axis=0)
    mad = np.abs(np.diff(s, axis=0)).mean(0)
    d = s - mean
    w = np.hanning(n)[:, None]
    spec = np.abs(np.fft.rfft(d * w, n=NFFT, axis=0)) ** 2 / (w ** 2).sum()
    freqs = np.fft.rfftfreq(NFFT, 1.0 / FS_HZ)
    bands = [np.log10(spec[(freqs >= lo) & (freqs < hi)].sum(0) + 1e-8) for lo, hi in BANDS_HZ]
    sel = (freqs >= BANDS_HZ[0][0]) & (freqs < 5.0)
    dominant = freqs[sel][np.argmax(spec[sel], axis=0)]
    ent_sel = freqs >= BANDS_HZ[0][0]
    p = spec[ent_sel] / np.maximum(spec[ent_sel].sum(0), 1e-300)
    entropy = -(p * np.log(np.maximum(p, 1e-300))).sum(0) / np.log(ent_sel.sum())
    # normalised autocorrelation through the FFT (zero-padded, so not circular)
    m = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(d, n=m, axis=0)
    ac = np.fft.irfft(f * np.conj(f), n=m, axis=0)[:n]
    ac = ac / np.maximum(ac[0], 1e-12)
    lo, hi = int(AUTOCORR_LAGS_S[0] * FS_HZ), int(AUTOCORR_LAGS_S[1] * FS_HZ)
    acpeak = ac[lo:hi + 1].max(0)
    return np.stack([mean, std, p10, p50, p90, mad, *bands, dominant, entropy, acpeak], axis=1)


def _corr3(x: np.ndarray) -> list:
    d = x - x.mean(0)
    sd = np.sqrt((d ** 2).mean(0))
    out = []
    for i, j in ((0, 1), (0, 2), (1, 2)):
        den = sd[i] * sd[j]
        out.append(float((d[:, i] * d[:, j]).mean() / den) if den > 1e-12 else 0.0)
    return out


def window_features(acc: np.ndarray, gyro: np.ndarray) -> np.ndarray:
    """Features of one checked window (20 Hz, same length). Order: ``FEATURE_NAMES``."""
    acc = np.asarray(acc, np.float64)
    gyro = np.asarray(gyro, np.float64)
    sig = np.concatenate([acc, np.linalg.norm(acc, axis=1, keepdims=True),
                          gyro, np.linalg.norm(gyro, axis=1, keepdims=True)], axis=1)
    per = _signal_features(sig).reshape(-1)
    gm = acc.mean(0)
    grav = gm / max(float(np.linalg.norm(gm)), 1e-9)
    return np.concatenate([per, grav, _corr3(acc), _corr3(gyro)])


# ── model ────────────────────────────────────────────────────────────────────

class ActivityModel:
    """A multilayer perceptron read from JSON: standardise, ReLU hidden layers, softmax."""

    def __init__(self, spec: dict) -> None:
        if spec.get("format") != MODEL_FORMAT:
            raise ValueError(f"not an activity model: format {spec.get('format')!r}")
        self.classes = list(spec["classes"])
        names = tuple(spec["feature_names"])
        if names != FEATURE_NAMES:
            raise ValueError("the model's features do not match this module's")
        self.mean = np.asarray(spec["standardise"]["mean"], np.float64)
        self.scale = np.asarray(spec["standardise"]["scale"], np.float64)
        self.layers = [(np.asarray(L["W"], np.float64), np.asarray(L["b"], np.float64))
                       for L in spec["layers"]]
        self.min_confidence = float(spec.get("min_confidence", MIN_CONFIDENCE))
        self.spec = spec
        width = len(FEATURE_NAMES)
        if self.mean.shape != (width,) or self.scale.shape != (width,) or (self.scale <= 0).any():
            raise ValueError("bad standardisation block")
        for W, b in self.layers:
            if W.ndim != 2 or W.shape[0] != width or b.shape != (W.shape[1],):
                raise ValueError("bad layer shapes")
            width = W.shape[1]
        if width != len(self.classes):
            raise ValueError("the output layer does not match the classes")
        if not all(np.isfinite(W).all() and np.isfinite(b).all() for W, b in self.layers):
            raise ValueError("non-finite weights")

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "ActivityModel":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        h = (np.atleast_2d(np.asarray(features, np.float64)) - self.mean) / self.scale
        for k, (W, b) in enumerate(self.layers):
            h = h @ W + b
            if k < len(self.layers) - 1:
                h = np.maximum(h, 0.0)
        h = h - h.max(axis=1, keepdims=True)
        e = np.exp(h)
        return e / e.sum(axis=1, keepdims=True)


@lru_cache(maxsize=1)
def default_model() -> ActivityModel:
    return ActivityModel.load(MODEL_PATH)


def estimate_activity(accel, gyro, *, fs_hz: float = FS_HZ, gyro_fs_hz: Optional[float] = None,
                      model: Optional[ActivityModel] = None,
                      min_confidence: Optional[float] = None) -> ActivityEstimate:
    """Classify one window. ``accel`` is (n, 3) in m/s^2 and ``gyro`` is (m, 3) in rad/s,
    both from the wrist, uniformly sampled at ``fs_hz`` (and ``gyro_fs_hz``)."""
    a, g, why = check_window(accel, gyro, fs_hz=fs_hz, gyro_fs_hz=gyro_fs_hz)
    if why:
        return _refuse(why)
    model = model or default_model()
    thr = model.min_confidence if min_confidence is None else float(min_confidence)
    p = model.predict_proba(window_features(a, g))[0]
    k = int(np.argmax(p))
    probs = {c: round(float(v), 4) for c, v in zip(model.classes, p)}
    conf = float(p[k])
    if conf < thr:
        return ActivityEstimate(activity=UNKNOWN, confidence=conf, reason="low_confidence",
                                candidate=model.classes[k], probabilities=probs)
    return ActivityEstimate(activity=model.classes[k], confidence=conf,
                            candidate=model.classes[k], probabilities=probs)
