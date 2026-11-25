# IRA CARE: IRA Care Heart, heart rate from wrist PPG, evaluated on PPG-DaLiA.
#
#   python -I scripts/training/heart_ppg_dalia.py --data-dir /path/outside/the/repo \
#       --out docs/ai/heart/results.json
#
# PPG-DaLiA (UCI dataset 495, CC BY 4.0): Reiss, Indlekofer, Schmidt, Van Laerhoven,
# "Deep PPG: Large-Scale Heart Rate Estimation with Convolutional Neural Networks",
# Sensors 2019, 19(14), 3079. 15 subjects, Empatica E4 on the non-dominant wrist:
# BVP (PPG) at 64 Hz, ACC at 32 Hz (in g in SX.pkl), ECG-derived heart rate per 8 s window
# shifted by 2 s ('label'), activity ids 0..8 at 4 Hz ('activity').
#
# What it does:
#   1. downloads the archive if it is absent and checks its SHA-256;
#   2. streams each SX.pkl out of the nested zip (no 20 GB extraction) through an
#      ALLOWLISTED unpickler (only numpy array reconstruction and builtin containers; the
#      dataset is untrusted data, and pickle can otherwise run code), and caches the
#      wrist BVP, wrist ACC, labels and activity as .npz (loaded with allow_pickle=False);
#   3. windows exactly as the labels: window i is BVP[128 i : 128 i + 512] and
#      ACC[64 i : 64 i + 256];
#   4. leave-one-subject-out over all 15 subjects for:
#        naive        largest PPG spectral peak, no accelerometer, no tracking (control)
#        classical    care/heart_estimate.py HeartTracker: band-pass, ACC motion
#                     suppression, harmonic support, temporal tracking (no training, so
#                     LOSO is trivially subject-independent); plus two ablations
#        learned      candidate-peak selection: a gradient-boosted classifier scores the
#                     spectral peaks of each window (features from PPG and ACC spectra),
#                     trained on 14 subjects, tested on the 15th, with causal tracking
#   5. writes per-subject and overall MAE (bpm), the activity breakdown, runtime and
#      model size as JSON.
#
# Nothing here is a medical claim, and none of it is a measurement on the IRA watch.
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pickle
import platform
import random
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

SEED = 20261125
random.seed(SEED)
np.random.seed(SEED)

URL = "https://archive.ics.uci.edu/static/public/495/ppg+dalia.zip"
ZIP_SHA256 = "5772387956e34e2e2dc4c2ddbeb98cb70569d5112fa4c13ee98a17680b84a1f3"
SUBJECTS = [f"S{i}" for i in range(1, 16)]
BVP_HZ, ACC_HZ, ACT_HZ = 64, 32, 4
WIN_S, SHIFT_S = 8, 2
ACTIVITIES = {0: "transient", 1: "sitting", 2: "stairs", 3: "table_soccer", 4: "cycling",
              5: "driving", 6: "lunch", 7: "walking", 8: "working"}
PUBLISHED = {
    "method": "Deep PPG CNN ensemble (Reiss et al., Sensors 2019)",
    "mae_bpm": 7.65,
    "note": "as reported, not reproduced: the paper's leave-one-subject-out MAE on PPG-DaLiA. "
            "The paper's site (mdpi.com) was not reachable from the build machine, and neither "
            "the dataset readme nor the UCI page states the number, so it was not re-checked here",
}
LEARNED_TRACK_SIGMA_BPM = 15.0
TOP_K = 3  # candidates taken from each of the raw and the suppressed spectrum

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "heart_estimate", ROOT / "supracloud-jarvis" / "ira" / "care" / "heart_estimate.py")
he = importlib.util.module_from_spec(_spec)
sys.modules["heart_estimate"] = he  # dataclasses look their module up here
_spec.loader.exec_module(he)


def log(msg):
    print(msg, flush=True)


# ── data ─────────────────────────────────────────────────────────────────────

class _AllowlistUnpickler(pickle.Unpickler):
    """Only what a pickled dict of numpy arrays needs. Anything else is refused."""

    ALLOWED = {
        ("numpy.core.multiarray", "_reconstruct"), ("numpy.core.multiarray", "scalar"),
        ("numpy._core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "scalar"),
        ("numpy", "ndarray"), ("numpy", "dtype"),
        ("builtins", "dict"), ("builtins", "list"), ("builtins", "tuple"),
        ("__builtin__", "dict"), ("__builtin__", "list"), ("__builtin__", "tuple"),
    }

    def find_class(self, module, name):
        if (module, name) not in self.ALLOWED:
            raise pickle.UnpicklingError(f"refused global {module}.{name}")
        return super().find_class(module, name)


def download(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    z = data_dir / "ppg_dalia.zip"
    if not z.exists():
        log(f"downloading {URL}")
        urllib.request.urlretrieve(URL, z)
    h = hashlib.sha256()
    with open(z, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    if h.hexdigest() != ZIP_SHA256:
        raise SystemExit(f"checksum mismatch for {z}: {h.hexdigest()}")
    return z


def _cache_one(args):
    zpath, subject, cache = args
    out = Path(cache) / f"{subject}.npz"
    if out.exists():
        return subject, "cached"
    with zipfile.ZipFile(zpath) as outer, outer.open("data.zip") as inner_f:
        with zipfile.ZipFile(inner_f) as inner, inner.open(f"PPG_FieldStudy/{subject}/{subject}.pkl") as f:
            d = _AllowlistUnpickler(io.BufferedReader(f, 1 << 22), encoding="latin1").load()
    w = d["signal"]["wrist"]
    np.savez(out, bvp=np.asarray(w["BVP"], np.float64).reshape(-1),
             acc=np.asarray(w["ACC"], np.float64).reshape(-1, 3),
             label=np.asarray(d["label"], np.float64).reshape(-1),
             activity=np.asarray(d["activity"], np.float64).reshape(-1))
    return subject, "extracted"


def load_subject(cache: Path, subject: str):
    with np.load(cache / f"{subject}.npz", allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def windows(d):
    bvp, acc, label, act = d["bvp"], d["acc"], d["label"], d["activity"]
    n = len(label)
    n = min(n, (len(bvp) - WIN_S * BVP_HZ) // (SHIFT_S * BVP_HZ) + 1,
            (len(acc) - WIN_S * ACC_HZ) // (SHIFT_S * ACC_HZ) + 1)
    acts = []
    for i in range(n):
        a = act[i * SHIFT_S * ACT_HZ: i * SHIFT_S * ACT_HZ + WIN_S * ACT_HZ].astype(int)
        acts.append(int(np.bincount(a).argmax()) if len(a) else 0)
    return n, label[:n], np.array(acts)


def bvp_win(d, i):
    return d["bvp"][i * SHIFT_S * BVP_HZ: i * SHIFT_S * BVP_HZ + WIN_S * BVP_HZ]


def acc_win(d, i):
    return d["acc"][i * SHIFT_S * ACC_HZ: i * SHIFT_S * ACC_HZ + WIN_S * ACC_HZ]


# ── per-subject work: classical variants and learned-model features ──────────

def _local_maxima(x, band):
    idx = np.where(band[1:-1] & (x[1:-1] >= x[:-2]) & (x[1:-1] > x[2:]))[0] + 1
    return idx[np.argsort(x[idx])[::-1]]


def subject_pass(args):
    cache, subject = args
    d = load_subject(Path(cache), subject)
    n, label, acts = windows(d)
    # The readme gives the E4's raw ACC.csv in 1/64 g, but in SX.pkl the wrist ACC is
    # already in g (a wrist at rest reads a magnitude of about 1.0), so no rescaling.
    scale = 1.0
    tracker = he.HeartTracker(acc_scale_g=scale)
    tracker_noacc = he.HeartTracker()
    out = {k: np.full(n, np.nan) for k in ("naive", "classical", "classical_no_acc", "classical_no_tracking", "conf")}
    refusals = {}
    feats, ys, groups_w, cand_bpm = [], [], [], []
    t_classical = 0.0
    n_bins = int(np.floor(2 * he.BAND_HZ[1] / he.GRID_HZ)) + 2
    grid = np.arange(n_bins) * he.GRID_HZ
    band = (grid >= he.BAND_HZ[0]) & (grid <= he.BAND_HZ[1])
    prev_classical = None
    for i in range(n):
        b, a = bvp_win(d, i), acc_win(d, i)
        out["naive"][i] = he.naive_spectral_peak_bpm(b) or np.nan
        t0 = time.perf_counter()
        r = tracker.update(b, a)
        t_classical += time.perf_counter() - t0
        if r.reason and r.reason != "low_confidence":
            refusals[r.reason] = refusals.get(r.reason, 0) + 1
        out["classical"][i] = r.candidate_bpm if r.candidate_bpm is not None else np.nan
        out["conf"][i] = r.confidence
        out["classical_no_acc"][i] = tracker_noacc.update(b).candidate_bpm or np.nan
        out["classical_no_tracking"][i] = he.estimate_heart_rate(b, a, acc_scale_g=scale).candidate_bpm or np.nan

        # learned-model candidates and features
        x, why = he._check_bvp(b, BVP_HZ)
        if why:
            continue
        raw = he._power_spectrum(x, BVP_HZ, n_bins)
        raw = raw / max(raw[band].max(), 1e-300)
        _, _, sup, score, suppressed = he._spectra(x, BVP_HZ, np.asarray(a, float), ACC_HZ, scale)
        aa = np.asarray(a, float) * scale
        accp = sum(he._power_spectrum(aa[:, j], ACC_HZ, n_bins) for j in range(3))
        acc_amp = np.sqrt(accp / max(accp[band].max(), 1e-300))
        bp = np.stack([he.bandpass_fft(aa[:, j], ACC_HZ) for j in range(3)], axis=1)
        motion = float(np.sqrt(np.mean(np.sum(bp ** 2, axis=1))))
        ks = list(_local_maxima(raw, band)[:TOP_K]) + list(_local_maxima(score, band)[:TOP_K])
        if r.candidate_bpm is not None:
            ks.append(int(np.argmin(np.abs(grid * 60 - r.candidate_bpm))))
        uniq = []
        for k in ks:
            if all(abs(k - u) > 6 for u in uniq):
                uniq.append(k)
        raw_rank = {k: j for j, k in enumerate(_local_maxima(raw, band))}
        sc_rank = {k: j for j, k in enumerate(_local_maxima(score, band))}
        cls = r.candidate_bpm if r.candidate_bpm is not None else np.nan
        prv = prev_classical if prev_classical is not None else np.nan
        for k in uniq:
            f = grid[k]
            fb = f * 60.0
            interp = lambda arr, ff: float(np.interp(ff, grid, arr, right=0.0))  # noqa: E731
            feats.append([fb, raw[k], sup[k], score[k], acc_amp[k], interp(acc_amp, f / 2), interp(acc_amp, 2 * f),
                          interp(raw, 2 * f), interp(raw, f / 2), np.log10(motion + 1e-6), float(suppressed),
                          raw_rank.get(k, 20), sc_rank.get(k, 20), fb - cls, abs(fb - cls), fb - prv, abs(fb - prv),
                          r.confidence, len(uniq)])
            ys.append(int(abs(fb - label[i]) <= 5.0))
            groups_w.append(i)
            cand_bpm.append(fb)
        prev_classical = r.candidate_bpm
    return {"subject": subject, "n": n, "label": label, "activity": acts, "out": out,
            "refusals": refusals, "t_classical_s": t_classical,
            "X": np.array(feats, np.float32), "y": np.array(ys, np.int8),
            "win": np.array(groups_w, np.int32), "cand_bpm": np.array(cand_bpm, np.float32)}


FEATURES = ["cand_bpm", "ppg_raw", "ppg_suppressed", "harmonic_score", "acc_amp", "acc_amp_half", "acc_amp_double",
            "ppg_raw_double", "ppg_raw_half", "log_motion_rms_g", "suppressed", "rank_raw", "rank_score",
            "minus_classical", "abs_minus_classical", "minus_prev_classical", "abs_minus_prev_classical",
            "classical_confidence", "n_candidates"]


def select_learned(p, win, cand_bpm, n):
    """Causal tracking over the classifier's candidate probabilities."""
    est = np.full(n, np.nan)
    order = np.argsort(win, kind="stable")
    win, p, cand_bpm = win[order], p[order], cand_bpm[order]
    starts = np.searchsorted(win, np.arange(n))
    ends = np.searchsorted(win, np.arange(n), side="right")
    prev = None
    for i in range(n):
        s, e = starts[i], ends[i]
        if s == e:
            est[i] = prev if prev is not None else np.nan
            continue
        sc = p[s:e].astype(np.float64)
        if prev is not None:
            sc = sc * np.exp(-0.5 * ((cand_bpm[s:e] - prev) / LEARNED_TRACK_SIGMA_BPM) ** 2)
        j = int(np.argmax(sc))
        est[i] = float(cand_bpm[s + j])
        prev = est[i]
    return est


def mae(est, label):
    m = np.isfinite(est)
    return float(np.mean(np.abs(est[m] - label[m]))) if m.any() else None, int((~m).sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get(
        "IRA_PPG_DALIA_DIR", Path.home() / ".cache" / "ira" / "ppg_dalia")),
        help="where the archive and the per-subject cache live; keep it outside the repository")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "ai" / "heart" / "results.json")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    t_start = time.time()
    from sklearn import __version__ as skl_version
    from sklearn.ensemble import HistGradientBoostingClassifier

    z = download(args.data_dir)
    cache = args.data_dir / "cache"
    cache.mkdir(exist_ok=True)
    with ProcessPoolExecutor(args.workers) as ex:
        for s, how in ex.map(_cache_one, [(str(z), s, str(cache)) for s in SUBJECTS]):
            log(f"{s}: {how}")
    t_load = time.time() - t_start

    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as ex:
        per = {r["subject"]: r for r in ex.map(subject_pass, [(str(cache), s) for s in SUBJECTS])}
    t_pass = time.time() - t0
    log(f"per-subject passes done in {t_pass:.0f} s")

    # learned model, LOSO
    t0 = time.time()
    learned, oracle, fold_info, train_times = {}, {}, {}, []
    model_bytes = None
    for s in SUBJECTS:
        tr = [per[o] for o in SUBJECTS if o != s]
        X = np.vstack([r["X"] for r in tr])
        y = np.concatenate([r["y"] for r in tr])
        clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, max_leaf_nodes=31,
                                             l2_regularization=1.0, random_state=SEED)
        t1 = time.time()
        clf.fit(X, y)
        train_times.append(time.time() - t1)
        te = per[s]
        t1 = time.perf_counter()
        p = clf.predict_proba(te["X"])[:, 1]
        est = select_learned(p, te["win"], te["cand_bpm"], te["n"])
        t_pred = time.perf_counter() - t1
        learned[s] = est
        # diagnostic: the best any selector could do with these candidates
        best = np.full(te["n"], np.nan)
        for w, c in zip(te["win"], te["cand_bpm"]):
            if np.isnan(best[w]) or abs(c - te["label"][w]) < abs(best[w] - te["label"][w]):
                best[w] = c
        oracle[s] = best
        fold_info[s] = {"train_rows": int(len(y)), "train_positive_share": round(float(y.mean()), 4),
                        "predict_s": round(t_pred, 3)}
        if model_bytes is None:
            model_bytes = len(pickle.dumps(clf))  # our own in-memory model, never loaded back
            n_nodes = int(sum(len(pred.nodes) for it in clf._predictors for pred in it))
        log(f"learned fold {s}: MAE {mae(est, te['label'])[0]:.2f}")
    t_learned = time.time() - t0

    methods = {
        "naive": {s: per[s]["out"]["naive"] for s in SUBJECTS},
        "classical": {s: per[s]["out"]["classical"] for s in SUBJECTS},
        "classical_no_acc": {s: per[s]["out"]["classical_no_acc"] for s in SUBJECTS},
        "classical_no_tracking": {s: per[s]["out"]["classical_no_tracking"] for s in SUBJECTS},
        "learned": learned,
        "candidate_oracle": oracle,
    }
    results = {}
    for name, est in methods.items():
        per_subject = {}
        all_err, all_act = [], []
        for s in SUBJECTS:
            m, missing = mae(est[s], per[s]["label"])
            per_subject[s] = {"mae_bpm": round(m, 2), "windows": int(per[s]["n"]), "no_estimate": missing}
            e = np.abs(est[s] - per[s]["label"])
            all_err.append(e)
            all_act.append(per[s]["activity"])
        e = np.concatenate(all_err)
        act = np.concatenate(all_act)
        subj = np.array([v["mae_bpm"] for v in per_subject.values()])
        by_act = {}
        for aid, aname in ACTIVITIES.items():
            mk = (act == aid) & np.isfinite(e)
            if mk.any():
                by_act[aname] = {"mae_bpm": round(float(e[mk].mean()), 2), "windows": int(mk.sum())}
        results[name] = {
            "mae_bpm_mean_of_subjects": round(float(subj.mean()), 2),
            "mae_bpm_std_of_subjects": round(float(subj.std()), 2),
            "mae_bpm_pooled_windows": round(float(np.nanmean(e)), 2),
            "per_subject": per_subject,
            "by_activity_pooled": by_act,
        }

    # confidence vs error for the shipped classical path
    conf = np.concatenate([per[s]["out"]["conf"] for s in SUBJECTS])
    ec = np.concatenate([np.abs(per[s]["out"]["classical"] - per[s]["label"]) for s in SUBJECTS])
    coverage = {}
    for t in (0.0, 0.2, 0.3, 0.4, 0.5, 0.6):
        mk = conf >= t
        coverage[f"{t:.1f}"] = {"share_of_windows": round(float(mk.mean()), 4),
                                "mae_bpm_pooled": round(float(ec[mk].mean()), 2) if mk.any() else None}

    n_windows = int(sum(per[s]["n"] for s in SUBJECTS))
    t_classical = sum(per[s]["t_classical_s"] for s in SUBJECTS)
    refusals = {}
    for s in SUBJECTS:
        for k, v in per[s]["refusals"].items():
            refusals[k] = refusals.get(k, 0) + v
    doc = {
        "dataset": {"name": "PPG-DaLiA", "source": "https://archive.ics.uci.edu/dataset/495/ppg+dalia",
                    "archive_sha256": ZIP_SHA256, "licence": "CC BY 4.0",
                    "citation": "A. Reiss, I. Indlekofer, P. Schmidt, K. Van Laerhoven. Deep PPG: Large-Scale "
                                "Heart Rate Estimation with Convolutional Neural Networks. Sensors 2019, 19(14), 3079.",
                    "subjects": len(SUBJECTS), "windows": n_windows,
                    "windowing": "8 s windows, 2 s shift, aligned with the dataset's 'label' array"},
        "protocol": "leave-one-subject-out over all 15 subjects; MAE in bpm against the ECG-derived label; "
                    "a missing estimate is excluded and counted in no_estimate",
        "seed": SEED,
        "methods": results,
        "published_reference": PUBLISHED,
        "classical_confidence_coverage": coverage,
        "classical_input_refusals": refusals,
        "learned_model": {"type": "sklearn HistGradientBoostingClassifier over spectral-peak candidates",
                          "params": {"max_iter": 200, "learning_rate": 0.1, "max_leaf_nodes": 31,
                                     "l2_regularization": 1.0, "random_state": SEED},
                          "features": FEATURES, "candidate_label": "within 5 bpm of the reference",
                          "tracking_sigma_bpm": LEARNED_TRACK_SIGMA_BPM,
                          "serialised_bytes_fold_S1": model_bytes, "tree_nodes_fold_S1": n_nodes,
                          "folds": fold_info},
        "classical_parameters": {k: getattr(he, k) for k in (
            "BAND_HZ", "MIN_SECONDS", "GRID_HZ", "MOTION_GATE_G", "SUPPRESSION_FLOOR", "SUPPRESSION_HALF_WIDTH_HZ",
            "HARMONIC_WEIGHT", "TRACK_SIGMA_BPM", "CONFIDENCE_HALF_WIDTH_HZ", "SATURATION_SHARE", "MIN_CONFIDENCE")},
        "runtime": {
            "classical_ms_per_window": round(1000 * t_classical / n_windows, 3),
            "classical_module_bytes": (ROOT / "supracloud-jarvis/ira/care/heart_estimate.py").stat().st_size,
            "classical_trained_parameters": 0,
            "learned_train_s_per_fold_mean": round(float(np.mean(train_times)), 2),
            "learned_predict_and_select_ms_per_window": round(
                1000 * sum(v["predict_s"] for v in fold_info.values()) / n_windows, 3),
            "data_load_s": round(t_load, 1), "per_subject_passes_s": round(t_pass, 1),
            "learned_loso_s": round(t_learned, 1), "total_s": round(time.time() - t_start, 1),
            "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, no GPU, {args.workers} worker processes",
            "versions": {"python": platform.python_version(), "numpy": np.__version__, "scikit-learn": skl_version},
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    for name, r in results.items():
        log(f"{name:24s} MAE {r['mae_bpm_mean_of_subjects']:6.2f} +/- {r['mae_bpm_std_of_subjects']:5.2f} "
            f"(pooled {r['mae_bpm_pooled_windows']:6.2f})")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
