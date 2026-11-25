"""IRA Care Heart, learned path: choose the pulse among spectral-peak candidates.

A wellbeing trend signal only. It is NOT a medical device, it does not diagnose, and no
alert or alarm may be raised on its output alone. Accuracy was measured on the public
PPG-DaLiA dataset (15 adults aged 30.6 +/- 9.6, an Empatica E4 wristband), never on the IRA
watch and never on older adults; see docs/ai/heart/MODEL_CARD.md.

How it works, per 8 s window:

1. The classical tracker (care/heart_estimate.py) runs first. If it refuses the input (too
   short, flat, saturated, not finite, bad accelerometer), so does this: no guess.
2. Candidates: the top local maxima of the raw PPG spectrum and of the motion-suppressed,
   harmonic-scored spectrum, plus the classical estimate, de-duplicated.
3. Each candidate gets 19 features (:data:`FEATURES`), computed here and nowhere else: the
   training script (scripts/training/heart_ppg_dalia.py) imports this module, so training
   and inference cannot drift apart.
4. A gradient-boosted tree ensemble gives each candidate the probability that it is within
   5 bpm of the true rate. Selection is causal: probability x a Gaussian (sigma 15 bpm)
   around the previous selected value.
5. The answer is "no reliable estimate" when the selected candidate's probability is below
   :data:`MIN_PROBABILITY`.

The trees are read from a plain JSON file (format ``ira-gbt-v1``), never from a pickle: the
loader checks the format, sizes, feature list, index ranges and that every child comes after
its parent (so evaluation always ends), and refuses anything else. With no model file, or an
invalid one, :class:`LearnedHeartTracker` answers with the classical estimator and says so
(``method="classical"``). Depends only on numpy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np

from care import heart_estimate as he

log = logging.getLogger(__name__)

FORMAT = "ira-gbt-v1"
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "heart_selector.json"
MAX_MODEL_BYTES = 4 * 1024 * 1024
MAX_TREES = 1000
MAX_NODES_PER_TREE = 4096

TOP_K = 3                     # candidates from each of the raw and the suppressed spectrum
DEDUP_BINS = 6                # candidates closer than this many grid bins (0.047 Hz) are one
TRACK_SIGMA_BPM = 15.0
#: Below this probability (that the chosen candidate is within 5 bpm) there is no estimate.
#: 0.5 means "more likely right than wrong"; it was fixed before looking at its coverage.
MIN_PROBABILITY = 0.5

FEATURES = ["cand_bpm", "ppg_raw", "ppg_suppressed", "harmonic_score", "acc_amp", "acc_amp_half", "acc_amp_double",
            "ppg_raw_double", "ppg_raw_half", "log_motion_rms_g", "suppressed", "rank_raw", "rank_score",
            "minus_classical", "abs_minus_classical", "minus_prev_classical", "abs_minus_prev_classical",
            "classical_confidence", "n_candidates"]


class ModelError(ValueError):
    """The model file is not a valid ``ira-gbt-v1`` ensemble."""


# ── features ─────────────────────────────────────────────────────────────────

def _grid():
    n_bins = int(np.floor(2 * he.BAND_HZ[1] / he.GRID_HZ)) + 2
    grid = np.arange(n_bins) * he.GRID_HZ
    band = (grid >= he.BAND_HZ[0]) & (grid <= he.BAND_HZ[1])
    return n_bins, grid, band


def _local_maxima(x, band):
    idx = np.where(band[1:-1] & (x[1:-1] >= x[:-2]) & (x[1:-1] > x[2:]))[0] + 1
    return idx[np.argsort(x[idx])[::-1]]


def candidate_features(x: np.ndarray, acc: np.ndarray, classical: he.HeartEstimate,
                       prev_classical: Optional[float], *, bvp_hz: float = he.BVP_HZ,
                       acc_hz: float = he.ACC_HZ, acc_scale_g: float = 1.0):
    """Candidates (bpm) and their feature rows for one checked window.

    ``x`` is the PPG window as returned by the classical input check; ``acc`` the
    accelerometer window (samples x axes); ``classical`` the classical tracker's answer for
    this window and ``prev_classical`` its candidate for the previous one.
    """
    n_bins, grid, band = _grid()
    raw = he._power_spectrum(x, bvp_hz, n_bins)
    raw = raw / max(raw[band].max(), 1e-300)
    a = np.asarray(acc, float)
    _, _, sup, score, suppressed = he._spectra(x, bvp_hz, a, acc_hz, acc_scale_g)
    aa = a * acc_scale_g
    accp = sum(he._power_spectrum(aa[:, j], acc_hz, n_bins) for j in range(aa.shape[1]))
    acc_amp = np.sqrt(accp / max(accp[band].max(), 1e-300))
    bp = np.stack([he.bandpass_fft(aa[:, j], acc_hz) for j in range(aa.shape[1])], axis=1)
    motion = float(np.sqrt(np.mean(np.sum(bp ** 2, axis=1))))
    raw_peaks, score_peaks = _local_maxima(raw, band), _local_maxima(score, band)
    ks = list(raw_peaks[:TOP_K]) + list(score_peaks[:TOP_K])
    if classical.candidate_bpm is not None:
        ks.append(int(np.argmin(np.abs(grid * 60 - classical.candidate_bpm))))
    uniq: list[int] = []
    for k in ks:
        if all(abs(k - u) > DEDUP_BINS for u in uniq):
            uniq.append(k)
    raw_rank = {k: j for j, k in enumerate(raw_peaks)}
    sc_rank = {k: j for j, k in enumerate(score_peaks)}
    cls = classical.candidate_bpm if classical.candidate_bpm is not None else np.nan
    prv = prev_classical if prev_classical is not None else np.nan

    def interp(arr, ff):
        return float(np.interp(ff, grid, arr, right=0.0))

    rows, bpms = [], []
    for k in uniq:
        f = grid[k]
        fb = f * 60.0
        rows.append([fb, raw[k], sup[k], score[k], acc_amp[k], interp(acc_amp, f / 2), interp(acc_amp, 2 * f),
                     interp(raw, 2 * f), interp(raw, f / 2), np.log10(motion + 1e-6), float(suppressed),
                     raw_rank.get(k, 20), sc_rank.get(k, 20), fb - cls, abs(fb - cls), fb - prv, abs(fb - prv),
                     classical.confidence, len(uniq)])
        bpms.append(fb)
    # float32, exactly as the training rows were stored
    return np.array(bpms, np.float32), np.array(rows, np.float32).reshape(len(rows), len(FEATURES))


# ── the tree ensemble ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Tree:
    feature: np.ndarray       # int, -1 at a leaf
    threshold: np.ndarray     # float; go left when x <= threshold
    missing_left: np.ndarray  # bool; where a NaN goes
    left: np.ndarray
    right: np.ndarray
    value: np.ndarray         # leaf values (raw log-odds contributions)


class TreeEnsemble:
    """A binary gradient-boosted tree classifier read from ``ira-gbt-v1`` JSON."""

    def __init__(self, baseline: float, trees: list, features: list[str], meta: dict):
        self.baseline = baseline
        self.trees = trees
        self.features = features
        self.meta = meta

    @classmethod
    def from_dict(cls, d) -> "TreeEnsemble":
        if not isinstance(d, dict) or d.get("format") != FORMAT:
            raise ModelError(f"not an {FORMAT} model")
        if d.get("features") != FEATURES:
            raise ModelError("feature list does not match this code")
        baseline = d.get("baseline")
        if not isinstance(baseline, (int, float)) or isinstance(baseline, bool) or not math.isfinite(baseline):
            raise ModelError("baseline must be a finite number")
        raw_trees = d.get("trees")
        if not isinstance(raw_trees, list) or not 1 <= len(raw_trees) <= MAX_TREES:
            raise ModelError("trees must be a list of 1 to %d trees" % MAX_TREES)
        trees = [cls._tree(t, i) for i, t in enumerate(raw_trees)]
        meta = {k: v for k, v in d.items() if k not in ("trees",)}
        return cls(float(baseline), trees, list(FEATURES), meta)

    @staticmethod
    def _tree(t, i) -> _Tree:
        keys = ("feature", "threshold", "missing_left", "left", "right", "value")
        if not isinstance(t, dict) or any(not isinstance(t.get(k), list) for k in keys):
            raise ModelError(f"tree {i}: needs lists {keys}")
        n = len(t["feature"])
        if not 1 <= n <= MAX_NODES_PER_TREE or any(len(t[k]) != n for k in keys):
            raise ModelError(f"tree {i}: node arrays must have one equal length, 1 to {MAX_NODES_PER_TREE}")
        try:
            feature = np.array(t["feature"], dtype=np.int64)
            left = np.array(t["left"], dtype=np.int64)
            right = np.array(t["right"], dtype=np.int64)
            threshold = np.array(t["threshold"], dtype=np.float64)
            value = np.array(t["value"], dtype=np.float64)
            missing_left = np.array(t["missing_left"], dtype=bool)
        except (TypeError, ValueError, OverflowError) as e:
            raise ModelError(f"tree {i}: bad node values ({e})") from None
        if any(not isinstance(v, bool) for v in t["missing_left"]) or any(
                isinstance(v, bool) or not isinstance(v, int) for k in ("feature", "left", "right") for v in t[k]):
            raise ModelError(f"tree {i}: wrong value types")
        leaf = feature == -1
        split = ~leaf
        idx = np.arange(n)
        if np.any((feature < -1) | (feature >= len(FEATURES))):
            raise ModelError(f"tree {i}: feature index out of range")
        if np.any(np.isnan(threshold[split])) or not np.all(np.isfinite(value)):
            raise ModelError(f"tree {i}: thresholds must be numbers and values finite")
        # children come after their parent and inside the tree: evaluation always ends
        if np.any(split & ((left <= idx) | (right <= idx) | (left >= n) | (right >= n))):
            raise ModelError(f"tree {i}: child index out of order or range")
        if np.any(leaf & ((left != -1) | (right != -1))):
            raise ModelError(f"tree {i}: a leaf has children")
        return _Tree(feature, threshold, missing_left, left, right, value)

    def raw_score(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2 or X.shape[1] != len(self.features):
            raise ValueError(f"expected rows of {len(self.features)} features, got shape {X.shape}")
        out = np.full(len(X), self.baseline)
        rows = np.arange(len(X))
        for t in self.trees:
            k = np.zeros(len(X), dtype=np.int64)
            # every step moves each row to a higher node index, so this ends within len(nodes) steps
            for _ in range(len(t.feature)):
                f = t.feature[k]
                active = f != -1
                if not active.any():
                    break
                v = X[rows[active], f[active]]
                ka = k[active]
                go_left = np.where(np.isnan(v), t.missing_left[ka], v <= t.threshold[ka])
                k[active] = np.where(go_left, t.left[ka], t.right[ka])
            out += t.value[k]
        return out

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Probability of the positive class ("within 5 bpm") for each row."""
        return 1.0 / (1.0 + np.exp(-self.raw_score(X)))


def read_model(path: Union[str, Path] = DEFAULT_MODEL_PATH) -> tuple[TreeEnsemble, str, int]:
    """Read and check a model file once: the ensemble, the SHA-256 of the exact bytes read and
    their length (the provenance a result can carry). Raises :class:`ModelError` (or OSError)
    when it is not valid."""
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_MODEL_BYTES:
        raise ModelError(f"model file is {size} bytes, over the {MAX_MODEL_BYTES} limit")
    raw = p.read_bytes()
    if len(raw) > MAX_MODEL_BYTES:
        raise ModelError(f"model file is {len(raw)} bytes, over the {MAX_MODEL_BYTES} limit")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ModelError(f"not JSON ({e})") from None
    return TreeEnsemble.from_dict(d), hashlib.sha256(raw).hexdigest(), len(raw)


def load_model(path: Union[str, Path] = DEFAULT_MODEL_PATH) -> TreeEnsemble:
    """Read and check a model file. Raises :class:`ModelError` (or OSError) when it is not valid."""
    return read_model(path)[0]


def try_load_model(path: Union[str, Path] = DEFAULT_MODEL_PATH) -> Optional[TreeEnsemble]:
    """The model, or None (logged) when it is absent or invalid: callers then use the classical path."""
    try:
        return load_model(path)
    except FileNotFoundError:
        log.info("heart: no learned model at %s; using the classical estimator", path)
    except (ModelError, OSError) as e:
        log.warning("heart: learned model at %s refused (%s); using the classical estimator", path, e)
    return None


# ── streaming estimator ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LearnedEstimate:
    """One window's answer. ``bpm`` is None when there is no reliable estimate."""

    bpm: Optional[float]
    confidence: float                    # selected candidate's probability (learned) or spectral confidence
    method: str                          # "learned" or "classical"
    reason: Optional[str] = None
    candidate_bpm: Optional[float] = None
    classical: Optional[he.HeartEstimate] = None

    @property
    def reliable(self) -> bool:
        return self.bpm is not None


class LearnedHeartTracker:
    """Track heart rate over consecutive windows with the learned candidate selector.

    ``model`` may be a loaded :class:`TreeEnsemble`, a path, or None for the default file.
    When no valid model is available, or a window has no accelerometer, every answer is the
    classical tracker's, with ``method="classical"``.
    """

    def __init__(self, model: Union[TreeEnsemble, str, Path, None] = None, *, bvp_hz: float = he.BVP_HZ,
                 acc_hz: float = he.ACC_HZ, acc_scale_g: float = 1.0, min_probability: float = MIN_PROBABILITY):
        if model is None or isinstance(model, (str, Path)):
            model = try_load_model(model if model is not None else DEFAULT_MODEL_PATH)
        self.model: Optional[TreeEnsemble] = model
        self.bvp_hz, self.acc_hz, self.acc_scale_g = bvp_hz, acc_hz, acc_scale_g
        self.min_probability = min_probability
        self.classical = he.HeartTracker(bvp_hz=bvp_hz, acc_hz=acc_hz, acc_scale_g=acc_scale_g)
        self._prev_classical: Optional[float] = None
        self._prev_selected: Optional[float] = None

    @property
    def uses_learned_model(self) -> bool:
        return self.model is not None

    def _classical_only(self, r: he.HeartEstimate) -> LearnedEstimate:
        return LearnedEstimate(bpm=r.bpm, confidence=r.confidence, method="classical", reason=r.reason,
                               candidate_bpm=r.candidate_bpm, classical=r)

    def update(self, bvp, acc=None) -> LearnedEstimate:
        r = self.classical.update(bvp, acc)
        if self.model is None or acc is None:
            return self._classical_only(r)
        if r.reason and r.reason != "low_confidence":
            # the input itself was refused: no candidates, no guess, state unchanged
            return LearnedEstimate(bpm=None, confidence=0.0, method="learned", reason=r.reason, classical=r)
        x, _ = he._check_bvp(bvp, self.bvp_hz)
        a, _ = he._check_acc(acc, self.acc_hz, len(x) / self.bvp_hz)
        bpms, X = candidate_features(x, a, r, self._prev_classical, bvp_hz=self.bvp_hz, acc_hz=self.acc_hz,
                                     acc_scale_g=self.acc_scale_g)
        self._prev_classical = r.candidate_bpm
        if len(bpms) == 0:
            return LearnedEstimate(bpm=None, confidence=0.0, method="learned", reason="no_candidates", classical=r)
        p = self.model.predict_proba(X)
        sc = p.copy()
        if self._prev_selected is not None:
            sc = sc * np.exp(-0.5 * ((bpms.astype(np.float64) - self._prev_selected) / TRACK_SIGMA_BPM) ** 2)
        j = int(np.argmax(sc))
        chosen = float(bpms[j])
        self._prev_selected = chosen
        bpm = round(chosen, 1)
        prob = float(p[j])
        if prob < self.min_probability:
            return LearnedEstimate(bpm=None, confidence=prob, method="learned", reason="low_confidence",
                                   candidate_bpm=bpm, classical=r)
        return LearnedEstimate(bpm=bpm, confidence=prob, method="learned", candidate_bpm=bpm, classical=r)
