"""IRA Care Heart: heart rate from one window of wrist PPG, with the wrist accelerometer.

A wellbeing trend signal only. It is NOT a medical device, it does not diagnose, and no
alert or alarm may be raised on its output alone. Accuracy was measured on the public
PPG-DaLiA dataset (15 adults, an Empatica E4 wristband), never on the IRA watch; see
docs/ai/heart/MODEL_CARD.md for the numbers and what is still to be validated.

The method is classical and has no trained weights, so it depends only on numpy:

1. Refuse input that cannot carry a pulse: too short, not finite, flat or saturated.
   A refusal returns ``bpm=None`` with a reason; the function never guesses.
2. Band-pass the PPG (and each accelerometer axis) to 0.5-3.5 Hz (30-210 bpm) by FFT
   masking, then take a Hann-windowed, zero-padded power spectrum on a common grid.
3. Motion-artefact suppression: when the wrist is moving, the normalised accelerometer
   amplitude spectrum marks the arm-swing and step frequencies, and the PPG spectrum is
   attenuated there (multiplied by ``1 - acc``, widened by a running maximum, floored).
4. Harmonic support: a candidate's power is boosted by the power at twice its frequency,
   which favours the pulse's fundamental over its first harmonic.
5. Temporal tracking: with the previous estimate, a Gaussian prior around it is applied.
   :class:`HeartTracker` keeps that state for a stream of windows (8 s, 2 s apart) and
   re-acquires when the tracked peak has been weak for several windows.
6. Confidence is the share of the suppressed in-band spectrum near the chosen peak.
   Below ``min_confidence`` the answer is "no reliable estimate".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

#: Sampling rates the defaults assume (the PPG-DaLiA wristband: BVP 64 Hz, ACC 32 Hz).
BVP_HZ = 64.0
ACC_HZ = 32.0
#: The heart-rate search band, Hz (30 to 210 bpm).
BAND_HZ = (0.5, 3.5)
#: Shortest window accepted, seconds. The reference windows are 8 s.
MIN_SECONDS = 6.0
#: Spectral grid resolution, Hz (about 0.47 bpm).
GRID_HZ = 1.0 / 128.0
#: Band-limited accelerometer RMS, in g, above which motion suppression is applied.
MOTION_GATE_G = 0.01
#: The PPG spectrum is never attenuated below this share at an accelerometer peak.
SUPPRESSION_FLOOR = 0.02
#: Each accelerometer peak suppresses the PPG spectrum this far either side, Hz. The
#: Hann main lobe of an 8 s window is 0.25 Hz wide each side, so a notch only one bin
#: wide would leave a strong artefact's skirt standing above a weak pulse.
SUPPRESSION_HALF_WIDTH_HZ = 0.1
#: Weight of the power at twice the candidate frequency (harmonic support).
HARMONIC_WEIGHT = 0.5
#: Standard deviation of the tracking prior around the previous estimate, bpm.
TRACK_SIGMA_BPM = 12.0
#: Half-width of the region around the peak counted towards the confidence, Hz.
CONFIDENCE_HALF_WIDTH_HZ = 0.15
#: Below this confidence the answer is "no reliable estimate". On PPG-DaLiA (pooled over
#: all 15 subjects, so chosen on the evaluation data, not held out) 0.3 kept 67% of
#: windows at 8.5 bpm MAE against 13.2 bpm for all windows; see docs/ai/heart/results.json.
MIN_CONFIDENCE = 0.3
#: A window is saturated when more than this share of samples sit on the min or max.
SATURATION_SHARE = 0.10


@dataclass(frozen=True)
class HeartEstimate:
    """One window's answer. ``bpm`` is None when there is no reliable estimate."""

    bpm: Optional[float]
    confidence: float
    reason: Optional[str] = None      # why there is no estimate; None when there is one
    motion_suppressed: bool = False
    candidate_bpm: Optional[float] = None  # the peak found, even when below min_confidence

    @property
    def reliable(self) -> bool:
        return self.bpm is not None


def _refuse(reason: str) -> HeartEstimate:
    return HeartEstimate(bpm=None, confidence=0.0, reason=reason)


def bandpass_fft(x: np.ndarray, fs: float, lo: float = BAND_HZ[0], hi: float = BAND_HZ[1]) -> np.ndarray:
    """Zero-phase band-pass by FFT masking (numpy only)."""
    x = np.asarray(x, dtype=np.float64)
    spec = np.fft.rfft(x - x.mean())
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fs)
    spec[(freqs < lo) | (freqs > hi)] = 0.0
    return np.fft.irfft(spec, n=len(x))


def _power_spectrum(x: np.ndarray, fs: float, n_bins: int) -> np.ndarray:
    """Power on the grid k * GRID_HZ, k = 0..n_bins-1, of a band-passed, Hann-windowed signal."""
    nfft = int(round(fs / GRID_HZ))
    if nfft < len(x):  # the grid must be at least as fine as the signal allows
        nfft = int(2 ** np.ceil(np.log2(len(x))))
    y = bandpass_fft(x, fs) * np.hanning(len(x))
    p = np.abs(np.fft.rfft(y, n=nfft)) ** 2
    grid = np.arange(n_bins) * GRID_HZ
    return np.interp(grid, np.fft.rfftfreq(nfft, d=1.0 / fs), p)


def _dilate(x: np.ndarray, half_width_hz: float) -> np.ndarray:
    """A running maximum over +/- half_width_hz on the grid."""
    r = int(round(half_width_hz / GRID_HZ))
    padded = np.pad(x, r, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * r + 1)
    return windows.max(axis=1)


def _is_flat(x: np.ndarray) -> bool:
    return float(np.ptp(x)) <= 1e-9 * max(1.0, float(np.abs(x).max()))


def _is_saturated(x: np.ndarray) -> bool:
    lo, hi = float(x.min()), float(x.max())
    tol = 1e-6 * (hi - lo)
    at_rail = np.count_nonzero(x <= lo + tol) + np.count_nonzero(x >= hi - tol)
    return at_rail > SATURATION_SHARE * len(x)


def _check_bvp(bvp, bvp_hz: float):
    x = np.asarray(bvp, dtype=np.float64)
    if x.ndim == 2 and 1 in x.shape:
        x = x.reshape(-1)
    if x.ndim != 1:
        return None, "bad_shape"
    if len(x) < MIN_SECONDS * bvp_hz:
        return None, "too_short"
    if not np.all(np.isfinite(x)):
        return None, "not_finite"
    if _is_flat(x):
        return None, "flat"
    if _is_saturated(x):
        return None, "saturated"
    return x, None


def _check_acc(acc, acc_hz: float, bvp_seconds: float):
    a = np.asarray(acc, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    if a.ndim != 2 or a.shape[1] > 3:
        return None, "bad_acc_shape"
    if len(a) < 0.75 * bvp_seconds * acc_hz:
        return None, "acc_too_short"
    if not np.all(np.isfinite(a)):
        return None, "acc_not_finite"
    return a, None


def _spectra(x: np.ndarray, bvp_hz: float, acc: Optional[np.ndarray], acc_hz: float,
             acc_scale_g: float):
    """The PPG spectrum after motion suppression, with the harmonic score, on the grid."""
    n_bins = int(np.floor(2 * BAND_HZ[1] / GRID_HZ)) + 2
    grid = np.arange(n_bins) * GRID_HZ
    ppg = _power_spectrum(x, bvp_hz, n_bins)
    band = (grid >= BAND_HZ[0]) & (grid <= BAND_HZ[1])
    peak = ppg[band].max()
    ppg = ppg / peak if peak > 0 else ppg
    suppressed = False
    if acc is not None:
        a = acc * acc_scale_g
        bp = np.stack([bandpass_fft(a[:, i], acc_hz) for i in range(a.shape[1])], axis=1)
        motion_rms = float(np.sqrt(np.mean(np.sum(bp ** 2, axis=1))))
        if motion_rms > MOTION_GATE_G:
            accp = sum(_power_spectrum(a[:, i], acc_hz, n_bins) for i in range(a.shape[1]))
            amax = accp[band].max()
            if amax > 0:
                amp = _dilate(np.sqrt(np.clip(accp / amax, 0.0, None)), SUPPRESSION_HALF_WIDTH_HZ)
                ppg = ppg * np.clip(1.0 - amp, SUPPRESSION_FLOOR, 1.0)
                suppressed = True
    # harmonic support: score(f) = P(f) * (1 + w * P(2f)). Multiplicative, so a weak
    # sub-harmonic (f/2 of a strong pulse peak) cannot win on its harmonic alone.
    double = np.interp(2 * grid, grid, ppg, right=0.0)
    score = ppg * (1.0 + HARMONIC_WEIGHT * double)
    return grid, band, ppg, score, suppressed


def _pick(grid, band, score, prior_bpm: Optional[float]):
    s = score.copy()
    if prior_bpm is not None:
        s = s * np.exp(-0.5 * ((grid * 60.0 - prior_bpm) / TRACK_SIGMA_BPM) ** 2)
    s[~band] = -np.inf
    k = int(np.argmax(s))
    f = grid[k]
    if 0 < k < len(s) - 1 and np.isfinite(s[k - 1]) and np.isfinite(s[k + 1]):
        den = s[k - 1] - 2 * s[k] + s[k + 1]
        if den < 0:  # parabolic interpolation of the peak
            f = f + 0.5 * (s[k - 1] - s[k + 1]) / den * GRID_HZ
    return float(f), k


def _confidence(grid, band, ppg, f: float) -> float:
    total = float(ppg[band].sum())
    if total <= 0:
        return 0.0
    near = band & (np.abs(grid - f) <= CONFIDENCE_HALF_WIDTH_HZ)
    return float(ppg[near].sum() / total)


def estimate_heart_rate(bvp, acc=None, *, bvp_hz: float = BVP_HZ, acc_hz: float = ACC_HZ,
                        acc_scale_g: float = 1.0, prev_bpm: Optional[float] = None,
                        min_confidence: float = MIN_CONFIDENCE) -> HeartEstimate:
    """Estimate heart rate (bpm) from one PPG window and the matching accelerometer window.

    ``bvp``: 1-D PPG samples at ``bvp_hz`` (at least ``MIN_SECONDS``; 8 s is the reference).
    ``acc``: (n, 3) accelerometer samples at ``acc_hz`` covering the same time, or None.
    ``acc_scale_g``: multiply ``acc`` by this to get g (1/9.80665 for m/s^2; 1/64 for the
    E4 wristband's raw units). ``prev_bpm``: the previous window's estimate, for tracking.
    """
    x, why = _check_bvp(bvp, bvp_hz)
    if why:
        return _refuse(why)
    a = None
    if acc is not None:
        a, why = _check_acc(acc, acc_hz, len(x) / bvp_hz)
        if why:
            return _refuse(why)
    grid, band, ppg, score, suppressed = _spectra(x, bvp_hz, a, acc_hz, acc_scale_g)
    if prev_bpm is not None and not np.isfinite(prev_bpm):
        prev_bpm = None
    f, _ = _pick(grid, band, score, prev_bpm)
    conf = _confidence(grid, band, ppg, f)
    bpm = round(f * 60.0, 1)
    if conf < min_confidence:
        return HeartEstimate(bpm=None, confidence=conf, reason="low_confidence",
                             motion_suppressed=suppressed, candidate_bpm=bpm)
    return HeartEstimate(bpm=bpm, confidence=conf, motion_suppressed=suppressed, candidate_bpm=bpm)


class HeartTracker:
    """Track heart rate over consecutive windows (8 s windows, 2 s apart).

    Each window is estimated with a prior around the previous estimate. When the tracked
    peak has scored below ``reacquire_ratio`` of the best peak anywhere in the band for
    ``reacquire_after`` windows in a row, the tracker drops its prior and re-acquires.
    A refused window leaves the state unchanged.
    """

    def __init__(self, *, bvp_hz: float = BVP_HZ, acc_hz: float = ACC_HZ,
                 acc_scale_g: float = 1.0, min_confidence: float = MIN_CONFIDENCE,
                 reacquire_ratio: float = 0.3, reacquire_after: int = 3):
        self.bvp_hz, self.acc_hz, self.acc_scale_g = bvp_hz, acc_hz, acc_scale_g
        self.min_confidence = min_confidence
        self.reacquire_ratio, self.reacquire_after = reacquire_ratio, reacquire_after
        self.prev_bpm: Optional[float] = None
        self._weak = 0

    def update(self, bvp, acc=None) -> HeartEstimate:
        x, why = _check_bvp(bvp, self.bvp_hz)
        if why:
            return _refuse(why)
        a = None
        if acc is not None:
            a, why = _check_acc(acc, self.acc_hz, len(x) / self.bvp_hz)
            if why:
                return _refuse(why)
        grid, band, ppg, score, suppressed = _spectra(x, self.bvp_hz, a, self.acc_hz, self.acc_scale_g)
        f_free, k_free = _pick(grid, band, score, None)
        f = f_free
        if self.prev_bpm is not None:
            f_tr, k_tr = _pick(grid, band, score, self.prev_bpm)
            if score[k_tr] < self.reacquire_ratio * score[k_free]:
                self._weak += 1
            else:
                self._weak = 0
            if self._weak < self.reacquire_after:
                f = f_tr
            else:
                self._weak = 0
        conf = _confidence(grid, band, ppg, f)
        bpm = round(f * 60.0, 1)
        self.prev_bpm = bpm
        if conf < self.min_confidence:
            return HeartEstimate(bpm=None, confidence=conf, reason="low_confidence",
                                 motion_suppressed=suppressed, candidate_bpm=bpm)
        return HeartEstimate(bpm=bpm, confidence=conf, motion_suppressed=suppressed, candidate_bpm=bpm)


def naive_spectral_peak_bpm(bvp, bvp_hz: float = BVP_HZ) -> Optional[float]:
    """The naive baseline: the largest PPG spectral peak in the band, no accelerometer, no
    tracking. Kept for evaluation and as the negative control in the tests; never use it
    as an estimate."""
    x, why = _check_bvp(bvp, bvp_hz)
    if why:
        return None
    n_bins = int(np.floor(BAND_HZ[1] / GRID_HZ)) + 2
    grid = np.arange(n_bins) * GRID_HZ
    p = _power_spectrum(x, bvp_hz, n_bins)
    p[(grid < BAND_HZ[0]) | (grid > BAND_HZ[1])] = -np.inf
    return round(float(grid[int(np.argmax(p))]) * 60.0, 1)
