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

ROOT = Path(__file__).resolve().parents[2]
# The shipped modules themselves: the candidate features are computed by the same code at
# training and at inference (care/heart_learned.py), so the two cannot drift apart.
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
from care import heart_estimate as he  # noqa: E402
from care import heart_learned as hl  # noqa: E402

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
LEARNED_TRACK_SIGMA_BPM = hl.TRACK_SIGMA_BPM
LEARNED_PARAMS = {"max_iter": 200, "learning_rate": 0.1, "max_leaf_nodes": 31, "l2_regularization": 1.0,
                  "random_state": SEED}



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

        # learned-model candidates and features (care/heart_learned.py, as at inference)
        x, why = he._check_bvp(b, BVP_HZ)
        if why:
            continue
        bpms, X = hl.candidate_features(x, np.asarray(a, float), r, prev_classical, bvp_hz=BVP_HZ, acc_hz=ACC_HZ,
                                        acc_scale_g=scale)
        for fb, row in zip(bpms, X):
            feats.append(row)
            ys.append(int(abs(float(fb) - label[i]) <= 5.0))
            groups_w.append(i)
            cand_bpm.append(fb)
        prev_classical = r.candidate_bpm
    return {"subject": subject, "n": n, "label": label, "activity": acts, "out": out,
            "refusals": refusals, "t_classical_s": t_classical,
            "X": np.array(feats, np.float32).reshape(-1, len(hl.FEATURES)), "y": np.array(ys, np.int8),
            "win": np.array(groups_w, np.int32), "cand_bpm": np.array(cand_bpm, np.float32)}


FEATURES = hl.FEATURES


def select_learned(p, win, cand_bpm, n):
    """Causal tracking over the classifier's candidate probabilities."""
    est = np.full(n, np.nan)
    prob = np.full(n, np.nan)
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
        prob[i] = float(p[s + j])
        prev = est[i]
    return est, prob


def export_ensemble(clf) -> dict:
    """The trained classifier as plain ``ira-gbt-v1`` JSON (care/heart_learned.py reads it)."""
    if clf.n_trees_per_iteration_ != 1 or list(clf.classes_) != [0, 1]:
        raise SystemExit("expected a binary classifier with one tree per iteration")
    trees = []
    for (pred,) in clf._predictors:
        nd = pred.nodes
        if nd["is_categorical"].any():
            raise SystemExit("categorical splits are not part of ira-gbt-v1")
        leaf = nd["is_leaf"].astype(bool)
        thr = nd["num_threshold"].astype(float)
        if not np.all(np.isfinite(thr[~leaf])):
            raise SystemExit("non-finite split threshold")
        trees.append({
            "feature": [-1 if lf else int(f) for lf, f in zip(leaf, nd["feature_idx"])],
            "threshold": [0.0 if lf else float(t) for lf, t in zip(leaf, thr)],
            "missing_left": [bool(m) and not lf for lf, m in zip(leaf, nd["missing_go_to_left"])],
            "left": [-1 if lf else int(v) for lf, v in zip(leaf, nd["left"])],
            "right": [-1 if lf else int(v) for lf, v in zip(leaf, nd["right"])],
            "value": [float(v) if lf else 0.0 for lf, v in zip(leaf, nd["value"])],
        })
    return {"format": hl.FORMAT, "features": list(hl.FEATURES),
            "baseline": float(np.asarray(clf._baseline_prediction).reshape(-1)[0]), "trees": trees}


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
    ap.add_argument("--model-out", type=Path, default=None,
                    help="also train on all 15 subjects and write the shipped model (ira-gbt-v1 JSON) here")
    ap.add_argument("--fixture-out", type=Path, default=None,
                    help="with --model-out: write rows and the trainer's probabilities for the equivalence test")
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
    learned, learned_prob, oracle, fold_info, train_times = {}, {}, {}, {}, []
    model_bytes = None
    for s in SUBJECTS:
        tr = [per[o] for o in SUBJECTS if o != s]
        X = np.vstack([r["X"] for r in tr])
        y = np.concatenate([r["y"] for r in tr])
        clf = HistGradientBoostingClassifier(**LEARNED_PARAMS)
        t1 = time.time()
        clf.fit(X, y)
        train_times.append(time.time() - t1)
        te = per[s]
        t1 = time.perf_counter()
        p = clf.predict_proba(te["X"])[:, 1]
        est, prob = select_learned(p, te["win"], te["cand_bpm"], te["n"])
        learned_prob[s] = prob
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

    # learned: probability of the selected candidate against error (MIN_PROBABILITY is fixed in code)
    lp = np.concatenate([learned_prob[s] for s in SUBJECTS])
    el = np.concatenate([np.abs(learned[s] - per[s]["label"]) for s in SUBJECTS])
    learned_coverage = {}
    for t in (0.0, 0.3, 0.4, hl.MIN_PROBABILITY, 0.6, 0.7):
        mk = np.nan_to_num(lp, nan=-1.0) >= t
        learned_coverage[f"{t:.1f}"] = {"share_of_windows": round(float(mk.mean()), 4),
                                        "mae_bpm_pooled": round(float(np.nanmean(el[mk])), 2) if mk.any() else None}
    shipped = None
    if args.model_out is not None:
        X = np.vstack([per[s]["X"] for s in SUBJECTS])
        y = np.concatenate([per[s]["y"] for s in SUBJECTS])
        clf = HistGradientBoostingClassifier(**LEARNED_PARAMS).fit(X, y)
        doc_model = export_ensemble(clf)
        doc_model["trained_on"] = "PPG-DaLiA, all 15 subjects (CC BY 4.0); metrics are the leave-one-subject-out ones"
        doc_model["params"] = {k: v for k, v in LEARNED_PARAMS.items()}
        text = json.dumps(doc_model, separators=(",", ":")) + "\n"
        args.model_out.parent.mkdir(parents=True, exist_ok=True)
        args.model_out.write_text(text, encoding="utf-8")
        ens = hl.load_model(args.model_out)
        p_skl = clf.predict_proba(X)[:, 1]
        p_np = ens.predict_proba(X)
        max_diff = float(np.max(np.abs(p_skl - p_np)))
        if max_diff > 1e-9:
            raise SystemExit(f"exported model disagrees with the trainer: max |dp| = {max_diff}")
        shipped = {"path": str(args.model_out.relative_to(ROOT)) if args.model_out.is_relative_to(ROOT) else str(args.model_out),
                   "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "trees": len(doc_model["trees"]), "nodes": int(sum(len(t["feature"]) for t in doc_model["trees"])),
                   "train_rows": int(len(y)), "max_abs_probability_difference_numpy_vs_sklearn": max_diff}
        log(f"shipped model: {shipped}")
        if args.fixture_out is not None:
            rng = np.random.default_rng(SEED)
            pick = rng.choice(len(X), 48, replace=False)
            nan_rows = np.where(np.isnan(X).any(axis=1))[0][:8]
            pick = np.concatenate([pick, nan_rows])
            fx = {"note": "rows of training features and the trainer's probabilities for them; "
                          "the numpy evaluator must reproduce them",
                  "model_sha256": shipped["sha256"],
                  "rows": [[None if np.isnan(v) else float(v) for v in X[i]] for i in pick],
                  "probability": [float(p_skl[i]) for i in pick]}
            args.fixture_out.write_text(json.dumps(fx, indent=1) + "\n", encoding="utf-8")

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
        "learned_probability_coverage": learned_coverage,
        "learned_min_probability": hl.MIN_PROBABILITY,
        "shipped_learned_model": shipped,
        "learned_model": {"type": "sklearn HistGradientBoostingClassifier over spectral-peak candidates",
                          "params": LEARNED_PARAMS,
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
