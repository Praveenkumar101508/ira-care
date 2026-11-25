# IRA CARE: IRA Care Activity, wrist activity recognition, evaluated on WISDM.
#
#   python3 -I scripts/training/activity_wisdm.py --data-dir /path/outside/the/repo \
#       --out docs/ai/activity/results.json \
#       --model-out supracloud-jarvis/ira/care/models/activity_model.json
#
# WISDM Smartphone and Smartwatch Activity and Biometrics Dataset (UCI dataset 507,
# DOI 10.24432/C5HK59, CC BY 4.0): Weiss, G. (2019). 51 subjects, 18 activities of 3
# minutes each, phone in the pocket and an LG G Watch on the dominant wrist, accelerometer
# (m/s^2) and gyroscope (rad/s) polled at a nominal 20 Hz. Raw lines are
# "subject,activity,timestamp_ns,x,y,z;".
#
# Only the WATCH accelerometer and gyroscope are read; the phone files are never opened.
# The subject id is used for one thing only, keeping every subject's windows in one fold.
# Nothing here identifies, or tries to identify, a person (the dataset also supports
# biometrics; that is a stated non-goal).
#
# What it does:
#   1. downloads the archive if it is absent and checks its SHA-256;
#   2. streams each watch file out of the nested zip (no extraction) and caches it as .npz
#      (loaded with allow_pickle=False);
#   3. per subject and activity: drops exact duplicate timestamps (some blocks are
#      recorded twice), resamples both sensors onto one 20 Hz grid by timestamp (some
#      blocks were logged at 50 or 100 Hz), and cuts non-overlapping 10 s windows that
#      have no gap over 0.25 s; every window goes through care/activity_estimate.py's
#      input checks, and its features are that module's features;
#   4. maps the 18 activities to 6 care classes (walking, stairs, sitting, standing,
#      eating_drinking, other);
#   5. 10-fold grouped cross-validation by subject (each subject in exactly one test
#      fold) for: the majority class, features + logistic regression, and the MLP that
#      ships; each fold's MLP is exported to JSON and the numpy evaluator is checked
#      against scikit-learn on that fold's held-out windows;
#   6. trains the shipped MLP on all subjects, exports it, and stores synthetic reference
#      windows' features and probabilities in the JSON for the unit test;
#   7. writes per-class, per-fold and per-subject metrics, the confusion matrices, the
#      confidence/coverage table, latency and model size as JSON.
#
# Nothing here is a medical claim, and none of it is a measurement on the IRA watch.
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import random
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# One BLAS thread per process: the folds run in parallel processes instead, which is much
# faster for these small matrices and keeps the arithmetic order fixed.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_v] = "1"

import numpy as np  # noqa: E402

SEED = 20261125
random.seed(SEED)
np.random.seed(SEED)

URL = ("https://archive.ics.uci.edu/static/public/507/"
       "wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset.zip")
ZIP_SHA256 = "6e0147f181d8f5275918db65eeb9dc842a0185d37cb6b2f30cbf74ee6e205469"
SUBJECTS = list(range(1600, 1651))
ACTIVITY_NAMES = {"A": "walking", "B": "jogging", "C": "stairs", "D": "sitting", "E": "standing",
                  "F": "typing", "G": "brushing_teeth", "H": "eating_soup", "I": "eating_chips",
                  "J": "eating_pasta", "K": "drinking", "L": "eating_sandwich", "M": "kicking",
                  "O": "catch", "P": "dribbling", "Q": "writing", "R": "clapping", "S": "folding_clothes"}
CARE_CLASS = {"A": "walking", "C": "stairs", "D": "sitting", "E": "standing",
              "H": "eating_drinking", "I": "eating_drinking", "J": "eating_drinking",
              "K": "eating_drinking", "L": "eating_drinking"}
CLASSES = ["walking", "stairs", "sitting", "standing", "eating_drinking", "other"]
N_FOLDS = 10
MAX_GAP_S = 0.25
MLP_PARAMS = {"hidden_layer_sizes": (64, 32), "alpha": 1e-3, "learning_rate_init": 1e-3,
              "batch_size": 256, "max_iter": 200, "random_state": SEED}
LR_PARAMS = {"C": 1.0, "class_weight": "balanced", "max_iter": 3000}
COVERAGE_THRESHOLDS = (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "activity_estimate", ROOT / "supracloud-jarvis" / "ira" / "care" / "activity_estimate.py")
ae = importlib.util.module_from_spec(_spec)
sys.modules["activity_estimate"] = ae  # dataclasses look their module up here
_spec.loader.exec_module(ae)
WIN = int(ae.WINDOW_S * ae.FS_HZ)


def log(msg):
    print(msg, flush=True)


# ── data ─────────────────────────────────────────────────────────────────────

def download(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    z = data_dir / "wisdm.zip"
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


def _parse(text: str, subject: int):
    codes, ts, xyz = [], [], []
    for line in text.split("\n"):
        line = line.strip().rstrip(";")
        if not line:
            continue
        p = line.split(",")
        if len(p) != 6 or int(p[0]) != subject or p[1] not in ACTIVITY_NAMES:
            raise ValueError(f"unexpected line for {subject}: {line[:80]!r}")
        codes.append(ord(p[1]))
        ts.append(int(p[2]))
        xyz.append((float(p[3]), float(p[4]), float(p[5])))
    return np.array(codes, np.uint8), np.array(ts, np.int64), np.array(xyz, np.float64)


def _cache_one(args):
    zpath, subject, cache = args
    out = Path(cache) / f"{subject}.npz"
    if out.exists():
        return subject, "cached"
    arrays = {}
    with zipfile.ZipFile(zpath) as outer, outer.open("wisdm-dataset.zip") as inner_f:
        with zipfile.ZipFile(io.BufferedReader(inner_f, 1 << 22)) as inner:
            for sensor in ("accel", "gyro"):
                name = f"wisdm-dataset/raw/watch/{sensor}/data_{subject}_{sensor}_watch.txt"
                c, t, x = _parse(inner.read(name).decode("ascii"), subject)
                arrays[f"{sensor}_code"], arrays[f"{sensor}_ts"], arrays[f"{sensor}_xyz"] = c, t, x
    np.savez(out, **arrays)
    return subject, "extracted"


def load_subject(cache: Path, subject: int):
    with np.load(cache / f"{subject}.npz", allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def _dedupe(ts, xyz):
    order = np.argsort(ts, kind="stable")
    ts, xyz = ts[order], xyz[order]
    keep = np.r_[True, np.diff(ts) > 0]
    return ts[keep], xyz[keep]


def _gap_to_nearest(grid, ts):
    j = np.clip(np.searchsorted(ts, grid), 1, len(ts) - 1)
    return np.minimum(np.abs(grid - ts[j - 1]), np.abs(ts[j] - grid))


def subject_windows(args):
    """Windows, features and bookkeeping for one subject (watch only)."""
    cache, subject = args
    d = load_subject(Path(cache), subject)
    feats, labels, codes = [], [], []
    stats = {"blocks": 0, "duplicate_rows_dropped": 0, "windows_with_gaps": 0, "refusals": {},
             "native_rate_hz": {}}
    fingerprints = {}
    for code in sorted(set(d["accel_code"].tolist()) & set(d["gyro_code"].tolist())):
        letter = chr(code)
        streams = []
        for sensor in ("accel", "gyro"):
            m = d[f"{sensor}_code"] == code
            ts, xyz = _dedupe(d[f"{sensor}_ts"][m], d[f"{sensor}_xyz"][m])
            stats["duplicate_rows_dropped"] += int(m.sum() - len(ts))
            streams.append((ts / 1e9, xyz))
        stats["blocks"] += 1
        rate = round(1.0 / float(np.median(np.diff(streams[0][0]))))
        stats["native_rate_hz"][str(rate)] = stats["native_rate_hz"].get(str(rate), 0) + 1
        fingerprints[letter] = hashlib.sha256(np.round(streams[0][1][:400], 4).tobytes()).hexdigest()[:16]
        t0 = max(s[0][0] for s in streams)
        t1 = min(s[0][-1] for s in streams)
        if t1 - t0 < ae.WINDOW_S:
            continue
        grid = t0 + np.arange(int((t1 - t0) * ae.FS_HZ) + 1) / ae.FS_HZ
        res, gap = [], np.zeros(len(grid))
        for ts, xyz in streams:
            res.append(np.stack([np.interp(grid, ts, xyz[:, j]) for j in range(3)], axis=1))
            gap = np.maximum(gap, _gap_to_nearest(grid, ts))
        for w in range(len(grid) // WIN):
            sl = slice(w * WIN, (w + 1) * WIN)
            if gap[sl].max() > MAX_GAP_S:
                stats["windows_with_gaps"] += 1
                continue
            a, g, why = ae.check_window(res[0][sl], res[1][sl])
            if why:
                stats["refusals"][why] = stats["refusals"].get(why, 0) + 1
                continue
            feats.append(ae.window_features(a, g))
            labels.append(CLASSES.index(CARE_CLASS.get(letter, "other")))
            codes.append(letter)
    return {"subject": subject, "X": np.array(feats, np.float64), "y": np.array(labels, np.int64),
            "code": np.array(codes), "stats": stats, "fingerprints": fingerprints}


# ── models and export ────────────────────────────────────────────────────────

def balanced_indices(y, rng):
    """Every class oversampled (with replacement) up to the largest class's count."""
    counts = np.bincount(y, minlength=len(CLASSES))
    out = []
    for c in range(len(CLASSES)):
        idx = np.where(y == c)[0]
        out.append(idx)
        if len(idx) and counts[c] < counts.max():
            out.append(rng.choice(idx, counts.max() - counts[c], replace=True))
    return np.sort(np.concatenate(out))


def fit_mlp(X, y):
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    rng = np.random.default_rng(SEED)
    idx = balanced_indices(y, rng)
    scaler = StandardScaler().fit(X)
    mlp = MLPClassifier(**MLP_PARAMS)
    mlp.fit(scaler.transform(X[idx]), y[idx])
    return scaler, mlp


def mlp_proba(scaler, mlp, X):
    return mlp.predict_proba(scaler.transform(X))


def export_mlp(scaler, mlp, extra=None) -> dict:
    assert list(mlp.classes_) == list(range(len(CLASSES)))
    spec = {
        "format": ae.MODEL_FORMAT,
        "classes": CLASSES,
        "unknown_label": ae.UNKNOWN,
        "min_confidence": ae.MIN_CONFIDENCE,
        "input": {"fs_hz": ae.FS_HZ, "window_s": ae.WINDOW_S, "accelerometer_units": "m/s^2",
                  "gyroscope_units": "rad/s", "placement": "wrist (watch)"},
        "feature_names": list(ae.FEATURE_NAMES),
        "standardise": {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()},
        "activation": "relu",
        "output": "softmax",
        "layers": [{"W": W.tolist(), "b": b.tolist()} for W, b in zip(mlp.coefs_, mlp.intercepts_)],
    }
    spec.update(extra or {})
    return spec


def synthetic_windows():
    """Synthetic wrist windows (no dataset content) whose features and probabilities are
    stored in the shipped JSON so the unit test can check the evaluator exactly."""
    rng = np.random.default_rng(SEED)
    t = np.arange(WIN) / ae.FS_HZ
    out = []
    for kind, hz, amp in [("still", 0.0, 0.0), ("still", 0.0, 0.0), ("swing", 0.9, 2.0), ("swing", 1.8, 4.0),
                          ("swing", 2.6, 6.0), ("swing", 1.0, 1.0), ("tilted", 0.0, 0.0),
                          ("noise", 0.0, 3.0), ("swing", 3.5, 8.0), ("tilted_swing", 1.5, 3.0),
                          ("noise", 0.0, 0.5), ("swing", 0.5, 1.5)]:
        g = np.array([0.0, 0.0, 9.81]) if not kind.startswith("tilted") else np.array([6.0, 2.0, 7.5])
        acc = np.tile(g, (WIN, 1)) + 0.02 * rng.standard_normal((WIN, 3))
        gyro = 0.01 * rng.standard_normal((WIN, 3))
        if "swing" in kind:
            ph = rng.uniform(0, 2 * np.pi)
            acc[:, 0] += amp * np.sin(2 * np.pi * hz * t + ph)
            acc[:, 1] += 0.4 * amp * np.sin(2 * np.pi * hz * t + ph + 1.0)
            gyro[:, 2] += 0.3 * amp * np.cos(2 * np.pi * hz * t + ph)
        if kind == "noise":
            acc += amp * rng.standard_normal((WIN, 3))
            gyro += 0.3 * amp * rng.standard_normal((WIN, 3))
        a, gg, why = ae.check_window(acc, gyro)
        assert why is None, (kind, why)
        out.append(ae.window_features(a, gg))
    return np.array(out)


# ── metrics ──────────────────────────────────────────────────────────────────

def per_class(y, pred):
    from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support
    p, r, f, s = precision_recall_fscore_support(y, pred, labels=range(len(CLASSES)), zero_division=0)
    return {
        "per_class": {c: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4),
                          "f1": round(float(f[i]), 4), "support": int(s[i])} for i, c in enumerate(CLASSES)},
        "macro_f1": round(float(f1_score(y, pred, labels=range(len(CLASSES)), average="macro", zero_division=0)), 4),
        "accuracy": round(float(np.mean(y == pred)), 4),
        "confusion_matrix": {"rows_true_columns_predicted": CLASSES,
                             "counts": confusion_matrix(y, pred, labels=range(len(CLASSES))).tolist()},
    }


def macro_f1(y, pred, present_only=False):
    """Macro-F1 over the six classes; with ``present_only``, over the classes that occur in
    ``y`` (a subject without, say, stairs windows is not scored 0 for stairs)."""
    from sklearn.metrics import f1_score
    labels = sorted(set(y.tolist())) if present_only else range(len(CLASSES))
    return float(f1_score(y, pred, labels=labels, average="macro", zero_division=0))


def run_fold(args):
    """One grouped fold: majority, logistic regression and the MLP, trained on the other
    folds' subjects; the MLP is exported to JSON and read back through the numpy evaluator."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    X, y, fold, k = args
    tr, te = fold != k, fold == k
    pred = {}
    t1 = time.time()
    maj = int(np.bincount(y[tr], minlength=len(CLASSES)).argmax())
    pred["majority"] = np.full(int(te.sum()), maj, np.int64)
    lr = make_pipeline(StandardScaler(), LogisticRegression(**LR_PARAMS))
    lr.fit(X[tr], y[tr])
    pred["logistic_regression"] = lr.predict(X[te])
    t_lr = time.time() - t1
    t1 = time.time()
    scaler, mlp = fit_mlp(X[tr], y[tr])
    t_mlp = time.time() - t1
    p_skl = mlp_proba(scaler, mlp, X[te])
    model = ae.ActivityModel(json.loads(json.dumps(export_mlp(scaler, mlp))))
    p_json = model.predict_proba(X[te])
    diff = float(np.abs(p_json - p_skl).max())
    agree = float(np.mean(p_json.argmax(1) == p_skl.argmax(1)))
    if diff > 1e-6 or agree < 1.0:
        raise SystemExit(f"fold {k}: the JSON model does not reproduce scikit-learn ({diff}, {agree})")
    pred["mlp"] = p_json.argmax(1)
    info = {"fold": k, "train_windows": int(tr.sum()), "test_windows": int(te.sum()),
            "majority_class": CLASSES[maj],
            "macro_f1": {m: round(macro_f1(y[te], pred[m]), 4) for m in pred},
            "json_vs_sklearn_max_abs_prob_diff": diff, "json_vs_sklearn_argmax_agreement": agree,
            "mlp_iterations": int(mlp.n_iter_),
            "train_s": {"logistic_regression": round(t_lr, 2), "mlp": round(t_mlp, 2)}}
    return {"pred": pred, "p_json": p_json, "info": info}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get(
        "IRA_WISDM_DIR", Path.home() / ".cache" / "ira" / "wisdm")),
        help="where the archive and the per-subject cache live; keep it outside the repository")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "ai" / "activity" / "results.json")
    ap.add_argument("--model-out", type=Path,
                    default=ROOT / "supracloud-jarvis" / "ira" / "care" / "models" / "activity_model.json")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    t_start = time.time()
    from sklearn import __version__ as skl_version

    z = download(args.data_dir)
    cache = args.data_dir / "cache"
    cache.mkdir(exist_ok=True)
    with ProcessPoolExecutor(args.workers) as ex:
        for s, how in ex.map(_cache_one, [(str(z), s, str(cache)) for s in SUBJECTS]):
            pass
    t_load = time.time() - t_start
    log(f"cache ready in {t_load:.0f} s")

    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as ex:
        per = {r["subject"]: r for r in ex.map(subject_windows, [(str(cache), s) for s in SUBJECTS])}
    t_windows = time.time() - t0
    subjects = [s for s in SUBJECTS if len(per[s]["y"])]
    log(f"windows: {sum(len(per[s]['y']) for s in subjects)} from {len(subjects)} subjects in {t_windows:.0f} s")

    # Data-quality check: identical watch recordings under two subject ids would leak
    # across folds. Reported, and both subjects are then kept in the same fold.
    seen, identical = {}, []
    for s in SUBJECTS:
        for letter, fp in per[s]["fingerprints"].items():
            if (letter, fp) in seen:
                identical.append([seen[(letter, fp)], s, letter])
            seen.setdefault((letter, fp), s)
    linked = {}
    for a, b, _ in identical:
        linked.setdefault(b, linked.get(a, a))

    # Folds: a seeded permutation of subjects dealt into 10 groups (linked subjects together).
    rng = np.random.default_rng(SEED)
    roots = [s for s in subjects if s not in linked]
    order = [roots[i] for i in rng.permutation(len(roots))]
    fold_of = {s: i % N_FOLDS for i, s in enumerate(order)}
    for s in subjects:
        if s in linked:
            fold_of[s] = fold_of[linked[s]]

    X = np.vstack([per[s]["X"] for s in subjects])
    y = np.concatenate([per[s]["y"] for s in subjects])
    code = np.concatenate([per[s]["code"] for s in subjects])
    subj = np.concatenate([np.full(len(per[s]["y"]), s) for s in subjects])
    fold = np.array([fold_of[s] for s in subj])

    oof = {"majority": np.zeros(len(y), np.int64), "logistic_regression": np.zeros(len(y), np.int64),
           "mlp": np.zeros(len(y), np.int64)}
    mlp_p = np.zeros((len(y), len(CLASSES)))
    fold_info = []
    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as ex:
        results = list(ex.map(run_fold, [(X, y, fold, k) for k in range(N_FOLDS)]))
    t_folds = time.time() - t0
    for k, r in enumerate(results):
        te = fold == k
        for m in oof:
            oof[m][te] = r["pred"][m]
        mlp_p[te] = r["p_json"]
        info = r["info"]
        info["test_subjects"] = sorted(int(s) for s in set(subj[te]))
        fold_info.append(info)
        log(f"fold {k}: macro-F1 majority {info['macro_f1']['majority']:.3f} "
            f"LR {info['macro_f1']['logistic_regression']:.3f} MLP {info['macro_f1']['mlp']:.3f} "
            f"(json diff {info['json_vs_sklearn_max_abs_prob_diff']:.1e})")

    methods = {}
    for m, pred in oof.items():
        r = per_class(y, pred)
        fm = np.array([f["macro_f1"][m] for f in fold_info])
        r["macro_f1_mean_of_folds"] = round(float(fm.mean()), 4)
        r["macro_f1_std_of_folds"] = round(float(fm.std()), 4)
        methods[m] = r
    per_subject = {}
    for s in subjects:
        mk = subj == s
        per_subject[str(s)] = {"fold": int(fold_of[s]), "windows": int(mk.sum()),
                               "classes_present": [CLASSES[c] for c in sorted(set(y[mk].tolist()))],
                               **{f"{m}_macro_f1": round(macro_f1(y[mk], oof[m][mk], present_only=True), 4)
                                  for m in ("logistic_regression", "mlp")},
                               "mlp_accuracy": round(float(np.mean(y[mk] == oof["mlp"][mk])), 4)}
    subj_f1 = np.array([v["mlp_macro_f1"] for v in per_subject.values()])

    # the 18 original activities: where the MLP puts each of them
    by_activity = {}
    for letter, name in ACTIVITY_NAMES.items():
        mk = code == letter
        if mk.any():
            counts = np.bincount(oof["mlp"][mk], minlength=len(CLASSES))
            by_activity[name] = {"care_class": CARE_CLASS.get(letter, "other"), "windows": int(mk.sum()),
                                 "predicted_share": {c: round(float(counts[i] / mk.sum()), 3)
                                                     for i, c in enumerate(CLASSES)}}

    # confidence against coverage ("unknown" below the threshold)
    conf = mlp_p.max(1)
    coverage = {}
    for thr in COVERAGE_THRESHOLDS:
        mk = conf >= thr
        coverage[f"{thr:.1f}"] = {
            "share_answered": round(float(mk.mean()), 4),
            "accuracy_when_answered": round(float(np.mean(y[mk] == oof["mlp"][mk])), 4) if mk.any() else None,
            "macro_f1_when_answered": round(macro_f1(y[mk], oof["mlp"][mk]), 4) if mk.any() else None}

    # the shipped model: all subjects, same recipe
    t1 = time.time()
    scaler, mlp = fit_mlp(X, y)
    t_final = time.time() - t1
    syn = synthetic_windows()
    spec = export_mlp(scaler, mlp)
    spec["reference"] = {
        "note": "synthetic windows made by scripts/training/activity_wisdm.py (no dataset content); "
                "probabilities are scikit-learn's predict_proba for this model",
        "features": syn.tolist(), "probabilities": mlp_proba(scaler, mlp, syn).tolist(),
        "tolerance": 1e-9}
    spec["provenance"] = {
        "dataset": "WISDM Smartphone and Smartwatch Activity and Biometrics Dataset (UCI 507), "
                   "watch accelerometer and gyroscope only",
        "licence": "CC BY 4.0",
        "citation": "Weiss, G. (2019). WISDM Smartphone and Smartwatch Activity and Biometrics Dataset "
                    "[Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5HK59.",
        "trained_on_subjects": len(subjects), "trained_on_windows": int(len(y)),
        "trainer": "scikit-learn MLPClassifier", "params": {k: list(v) if isinstance(v, tuple) else v
                                                             for k, v in MLP_PARAMS.items()},
        "training_script": "scripts/training/activity_wisdm.py",
        "model_card": "docs/ai/activity/MODEL_CARD.md"}
    text = json.dumps(spec, separators=(",", ":")) + "\n"
    args.model_out.parent.mkdir(parents=True, exist_ok=True)
    args.model_out.write_text(text, encoding="utf-8")
    shipped = ae.ActivityModel.load(args.model_out)
    full_diff = float(np.abs(shipped.predict_proba(X) - mlp_proba(scaler, mlp, X)).max())
    ref_diff = float(np.abs(shipped.predict_proba(np.array(spec["reference"]["features"]))
                            - np.array(spec["reference"]["probabilities"])).max())
    if full_diff > 1e-6 or ref_diff > 1e-9:
        raise SystemExit(f"the shipped JSON does not reproduce scikit-learn ({full_diff}, {ref_diff})")
    n_params = int(sum(W.size + b.size for W, b in zip(mlp.coefs_, mlp.intercepts_))
                   + 2 * len(ae.FEATURE_NAMES))

    # latency of the shipped path on raw 20 Hz windows, single process: checks, features, MLP
    lat_rng = np.random.default_rng(SEED)
    raw = []
    t = np.arange(WIN) / ae.FS_HZ
    for _ in range(300):  # synthetic windows: timing only, the content is irrelevant
        acc = np.tile([0.0, 0.0, 9.81], (WIN, 1)) + lat_rng.standard_normal((WIN, 3))
        gyro = 0.3 * np.sin(2 * np.pi * 1.5 * t)[:, None] + 0.05 * lat_rng.standard_normal((WIN, 3))
        raw.append((acc, gyro))
    for acc, gyro in raw[:20]:
        ae.estimate_activity(acc, gyro, model=shipped)
    t1 = time.perf_counter()
    for acc, gyro in raw:
        ae.estimate_activity(acc, gyro, model=shipped)
    lat_total = (time.perf_counter() - t1) / len(raw)
    feats_only = np.array([ae.window_features(a, g) for a, g in raw])
    t1 = time.perf_counter()
    for _ in range(10):
        for row in feats_only:
            shipped.predict_proba(row)
    lat_mlp = (time.perf_counter() - t1) / (10 * len(raw))

    refusals, dup, gaps, rates = {}, 0, 0, {}
    for s in SUBJECTS:
        st = per[s]["stats"]
        dup += st["duplicate_rows_dropped"]
        gaps += st["windows_with_gaps"]
        for k, v in st["refusals"].items():
            refusals[k] = refusals.get(k, 0) + v
        for k, v in st["native_rate_hz"].items():
            rates[k] = rates.get(k, 0) + v
    doc = {
        "dataset": {"name": "WISDM Smartphone and Smartwatch Activity and Biometrics Dataset",
                    "source": "https://archive.ics.uci.edu/dataset/507/"
                              "wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset",
                    "doi": "10.24432/C5HK59", "archive_sha256": ZIP_SHA256, "licence": "CC BY 4.0",
                    "licence_checked": "2026-11-25",
                    "citation": spec["provenance"]["citation"],
                    "inputs": "watch accelerometer (m/s^2) and watch gyroscope (rad/s) only; phone not used",
                    "subjects_with_windows": len(subjects), "windows": int(len(y)),
                    "windowing": "per subject and activity: duplicate timestamps dropped, both sensors "
                                 "resampled to one 20 Hz grid, non-overlapping 10 s windows with no gap "
                                 f"over {MAX_GAP_S} s",
                    "duplicate_rows_dropped": dup, "windows_dropped_for_gaps": gaps,
                    "windows_refused_by_input_checks": refusals,
                    "blocks_by_native_rate_hz": rates,
                    "identical_recordings_across_subject_ids": identical},
        "classes": {"care_classes": CLASSES,
                    "mapping": {ACTIVITY_NAMES[k]: CARE_CLASS.get(k, "other") for k in ACTIVITY_NAMES},
                    "window_counts": {c: int((y == i).sum()) for i, c in enumerate(CLASSES)},
                    "unknown_output": f"'{ae.UNKNOWN}' when the top probability is below {ae.MIN_CONFIDENCE}"},
        "protocol": f"{N_FOLDS}-fold cross-validation grouped by subject (a seeded permutation of subjects "
                    "dealt into folds; every subject is tested exactly once, never trained on in that fold). "
                    "Metrics are over the pooled out-of-fold predictions, forced to the top class "
                    "(no 'unknown'); the coverage table shows the effect of the threshold.",
        "seed": SEED,
        "methods": methods,
        "folds": fold_info,
        "per_subject": per_subject,
        "mlp_per_subject_macro_f1": {"mean": round(float(subj_f1.mean()), 4), "std": round(float(subj_f1.std()), 4),
                                     "min": round(float(subj_f1.min()), 4), "max": round(float(subj_f1.max()), 4)},
        "mlp_by_original_activity": by_activity,
        "mlp_confidence_coverage": coverage,
        "shipped_model": {"path": "supracloud-jarvis/ira/care/models/activity_model.json",
                          "bytes": len(text.encode("utf-8")),
                          "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                          "parameters": n_params, "features": len(ae.FEATURE_NAMES),
                          "architecture": f"{len(ae.FEATURE_NAMES)} features, standardised -> "
                                          + " -> ".join(str(h) for h in MLP_PARAMS["hidden_layer_sizes"])
                                          + f" ReLU -> {len(CLASSES)} softmax",
                          "params": spec["provenance"]["params"], "iterations": int(mlp.n_iter_),
                          "json_vs_sklearn_max_abs_prob_diff_all_windows": full_diff,
                          "reference_max_abs_prob_diff": ref_diff,
                          "logistic_regression_params": LR_PARAMS},
        "runtime": {
            "shipped_ms_per_window_checks_features_mlp": round(1000 * lat_total, 3),
            "shipped_ms_per_window_mlp_only": round(1000 * lat_mlp, 4),
            "latency_note": "one process, numpy only, 300 synthetic 10 s windows at 20 Hz after 20 warm-up calls",
            "data_load_s": round(t_load, 1), "windowing_s": round(t_windows, 1),
            "cross_validation_s": round(t_folds, 1),
            "final_train_s": round(t_final, 1), "total_s": round(time.time() - t_start, 1),
            "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, no GPU, {args.workers} worker processes",
            "versions": {"python": platform.python_version(), "numpy": np.__version__, "scikit-learn": skl_version},
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    for m, r in methods.items():
        log(f"{m:22s} macro-F1 {r['macro_f1']:.4f} (folds {r['macro_f1_mean_of_folds']:.4f} "
            f"+/- {r['macro_f1_std_of_folds']:.4f}) accuracy {r['accuracy']:.4f}")
    log(f"model {len(text)} bytes, {n_params} parameters; {1000 * lat_total:.2f} ms per window")
    log(f"wrote {args.out} and {args.model_out}")


if __name__ == "__main__":
    sys.exit(main())
