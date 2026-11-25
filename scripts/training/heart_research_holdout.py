# IRA CARE: IRA Care Heart research pipeline, checked on a held-out PPG-DaLiA subject.
#
#   python3 -I scripts/training/heart_research_holdout.py --data-dir /path/outside/the/repo \
#       --out docs/ai/heart/research_pipeline_eval.json \
#       --excerpt-out supracloud-jarvis/ira/care/research_samples/ppg_dalia_s1_excerpt.json
#
# The shipped learned model was trained on all 15 subjects, so no subject is held out from it.
# To check the research pipeline (care/heart_research.py) on a subject the learned model has
# never seen, this script:
#   1. checks the archive's SHA-256 and builds the per-subject cache exactly as
#      heart_ppg_dalia.py does (that script's own functions are imported, not copied);
#   2. trains the leave-one-subject-out fold model WITHOUT the held-out subject (S1, chosen
#      before any of this was run: the first subject), with the same settings and seed, and
#      exports it to ira-gbt-v1 JSON outside the repository;
#   3. runs the research pipeline over the whole held-out recording with that fold model and
#      compares every window with the evaluation script's own path (the classical tracker and
#      the trainer's probabilities with the same causal selection);
#   4. reports the pipeline's reported answers (quality rejections, classical fallback) against
#      the ECG-derived labels, and runs the in-repository excerpt with the fold model and with
#      the shipped model;
#   5. with --excerpt-out, writes the excerpt: S1 from 1800 s for 300 s (fixed before looking
#      at any result), PPG as in the dataset (two decimals), accelerometer in the E4's raw
#      1/64 g counts (exact: the dataset's g values are multiples of 1/64), and the labels.
#
# PPG-DaLiA: UCI dataset 495, CC BY 4.0 (Reiss, Indlekofer, Schmidt, Van Laerhoven, Sensors 2019,
# 19(14), 3079). Nothing here is a measurement on the IRA watch or on any person in care.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import heart_ppg_dalia as hp  # noqa: E402  (also puts supracloud-jarvis/ira on the path)
from care import heart_learned as hl  # noqa: E402
from care import heart_research as hr  # noqa: E402

ROOT = hp.ROOT
HELD_OUT = "S1"
EXCERPT_START_S, EXCERPT_SECONDS = 1800, 300


def recording_from_subject(d, *, start_s=0, seconds=None, raw_acc=False) -> dict:
    """A research recording (the pipeline's data contract) from a cached subject."""
    b0 = start_s * hp.BVP_HZ
    a0 = start_s * hp.ACC_HZ
    b1 = len(d["bvp"]) if seconds is None else b0 + seconds * hp.BVP_HZ
    a1 = len(d["acc"]) if seconds is None else a0 + seconds * hp.ACC_HZ
    bvp, acc = d["bvp"][b0:b1], d["acc"][a0:a1]
    w0 = start_s // hp.SHIFT_S
    n_w = (min(len(bvp) // hp.BVP_HZ, len(acc) // hp.ACC_HZ) - hp.WIN_S) // hp.SHIFT_S + 1
    label = d["label"][w0:w0 + n_w]
    if raw_acc:
        counts = acc * 64.0
        if not np.allclose(counts, np.round(counts), atol=1e-9):
            raise SystemExit("accelerometer values are not whole 1/64 g counts")
        acc_samples, unit = [[int(v) for v in row] for row in np.round(counts)], "e4_raw"
        ppg_samples = [round(float(v), 2) for v in bvp]
        if np.max(np.abs(np.array(ppg_samples) - bvp)) > 1e-9:
            raise SystemExit("PPG values are not two-decimal numbers; the excerpt would not be exact")
    else:
        acc_samples, unit, ppg_samples = acc, "g", bvp
    return {"source": "research_recording",
            "ppg": {"fs_hz": float(hp.BVP_HZ), "unit": "E4 BVP (arbitrary units)", "t0_s": 0.0, "samples": ppg_samples},
            "acc": {"fs_hz": float(hp.ACC_HZ), "unit": unit, "t0_s": 0.0, "samples": acc_samples},
            "reference": {"bpm": [round(float(v), 4) for v in label],
                          "kind": "ECG-derived mean heart rate per 8 s window (the dataset's label)"}}


def write_excerpt(cache: Path, out: Path) -> dict:
    d = hp.load_subject(cache, HELD_OUT)
    rec = recording_from_subject(d, start_s=EXCERPT_START_S, seconds=EXCERPT_SECONDS, raw_acc=True)
    act = d["activity"][EXCERPT_START_S * hp.ACT_HZ:(EXCERPT_START_S + EXCERPT_SECONDS) * hp.ACT_HZ].astype(int)
    doc = {
        "format": hr.SAMPLE_FORMAT,
        "label": f"PPG-DaLiA subject {HELD_OUT}, {EXCERPT_SECONDS // 60} minutes (recorded research data)",
        "dataset": hr.DATASET["name"], "licence": hr.DATASET["licence"],
        "attribution": (f"{hr.DATASET['citation']} Dataset: Reiss, A., Indlekofer, I., Schmidt, P. (2019), PPG-DaLiA, "
                        f"UCI Machine Learning Repository, doi:{hr.DATASET['doi']}. Licensed under CC BY 4.0 "
                        "(https://creativecommons.org/licenses/by/4.0/)."),
        "changes": (f"An excerpt: subject {HELD_OUT}, wrist BVP and ACC from {EXCERPT_START_S} s for {EXCERPT_SECONDS} s, "
                    "with the matching window labels. Accelerometer written as the E4's raw 1/64 g counts; "
                    "values otherwise unchanged."),
        "excerpt": {"subject": HELD_OUT, "start_s": EXCERPT_START_S, "seconds": EXCERPT_SECONDS,
                    "activities_present": sorted({hp.ACTIVITIES[int(a)] for a in np.unique(act)})},
        "note": "Recorded research data from a public dataset. Not a measurement of any person in care.",
        **rec,
    }
    text = json.dumps(doc, separators=(",", ":")) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"path": str(out.relative_to(ROOT)) if out.is_relative_to(ROOT) else str(out),
            "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def mae(est, label):
    est = np.asarray(est, float)
    m = np.isfinite(est)
    return (round(float(np.mean(np.abs(est[m] - label[m]))), 2) if m.any() else None), int(m.sum())


def summarise(result: hr.ResearchResult, label) -> dict:
    w = result.windows
    n = min(len(w), len(label))
    label = np.asarray(label[:n], float)
    cls = np.array([np.nan if (x.classical or {}).get("candidate_bpm") is None else x.classical["candidate_bpm"] for x in w[:n]])
    lrn = np.array([np.nan if (x.learned or {}).get("candidate_bpm") is None else x.learned["candidate_bpm"] for x in w[:n]])
    rep = np.array([np.nan if x.bpm is None else x.bpm for x in w[:n]])
    rep_learned = np.array([x.bpm if x.method == "learned" else np.nan for x in w[:n]], float)
    rep_classical = np.array([x.bpm if x.method == "classical" else np.nan for x in w[:n]], float)
    s = result.summary()
    return {
        "windows": n,
        "classical_all_windows": dict(zip(("mae_bpm", "windows"), mae(cls, label))),
        "learned_all_windows": dict(zip(("mae_bpm", "windows"), mae(lrn, label))),
        "reported": dict(zip(("mae_bpm", "windows"), mae(rep, label))),
        "reported_share_of_windows": round(float(np.isfinite(rep).mean()), 4),
        "reported_learned": dict(zip(("mae_bpm", "windows"), mae(rep_learned, label))),
        "reported_classical_fallback": dict(zip(("mae_bpm", "windows"), mae(rep_classical, label))),
        "unavailable_by_reason": s["unavailable_by_reason"],
        "resources": result.resources,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "ai" / "heart" / "research_pipeline_eval.json")
    ap.add_argument("--excerpt-out", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    t_start = time.time()
    from sklearn import __version__ as skl_version
    from sklearn.ensemble import HistGradientBoostingClassifier

    z = hp.download(args.data_dir)
    cache = args.data_dir / "cache"
    cache.mkdir(exist_ok=True)
    with ProcessPoolExecutor(args.workers) as ex:
        for s, how in ex.map(hp._cache_one, [(str(z), s, str(cache)) for s in hp.SUBJECTS]):
            hp.log(f"{s}: {how}")
    excerpt = write_excerpt(cache, args.excerpt_out) if args.excerpt_out else None

    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as ex:
        per = {r["subject"]: r for r in ex.map(hp.subject_pass, [(str(cache), s) for s in hp.SUBJECTS])}
    hp.log(f"per-subject passes done in {time.time() - t0:.0f} s")

    train = [per[s] for s in hp.SUBJECTS if s != HELD_OUT]
    X = np.vstack([r["X"] for r in train])
    y = np.concatenate([r["y"] for r in train])
    clf = HistGradientBoostingClassifier(**hp.LEARNED_PARAMS).fit(X, y)
    fold_doc = hp.export_ensemble(clf)
    fold_doc["trained_on"] = f"PPG-DaLiA, the 14 subjects other than {HELD_OUT} (CC BY 4.0); evaluation only, not shipped"
    fold_path = args.data_dir / f"heart_selector_without_{HELD_OUT}.json"
    fold_text = json.dumps(fold_doc, separators=(",", ":")) + "\n"
    fold_path.write_text(fold_text, encoding="utf-8")
    te = per[HELD_OUT]
    p_skl = clf.predict_proba(te["X"])[:, 1]
    p_np = hl.load_model(fold_path).predict_proba(te["X"])
    max_dp = float(np.max(np.abs(p_skl - p_np)))
    if max_dp > 1e-9:
        raise SystemExit(f"exported fold model disagrees with the trainer: {max_dp}")
    script_learned, _ = hp.select_learned(p_skl, te["win"], te["cand_bpm"], te["n"])
    script_classical = te["out"]["classical"]
    label = te["label"]

    d = hp.load_subject(cache, HELD_OUT)
    rec = hr.validate_recording(recording_from_subject(d), name=f"{HELD_OUT} (whole recording)",
                                max_seconds=None, max_samples=None)
    t1 = time.time()
    res = hr.run(rec, model_path=str(fold_path), measure_memory=False)
    t_pipeline = time.time() - t1
    n = min(len(res.windows), te["n"])
    pc = np.array([np.nan if (w.classical or {}).get("candidate_bpm") is None else w.classical["candidate_bpm"]
                   for w in res.windows[:n]])
    pl = np.array([np.nan if (w.learned or {}).get("candidate_bpm") is None else w.learned["candidate_bpm"]
                   for w in res.windows[:n]])
    sl = np.round(script_learned[:n], 1)
    agree = {
        "windows_compared": int(n),
        "classical_identical": int(np.sum((pc == script_classical[:n]) | (np.isnan(pc) & np.isnan(script_classical[:n])))),
        "learned_within_0_05_bpm": int(np.sum((np.abs(pl - sl) <= 0.05) | (np.isnan(pl) & np.isnan(sl)))),
        "note": "the pipeline rounds the selected candidate to 0.1 bpm, as LearnedHeartTracker does; the script's "
                "selection is rounded the same way before comparing",
    }
    loso = json.loads((ROOT / "docs" / "ai" / "heart" / "results.json").read_text())["methods"]
    reference = {"classical": loso["classical"]["per_subject"][HELD_OUT]["mae_bpm"],
                 "learned": loso["learned"]["per_subject"][HELD_OUT]["mae_bpm"]}
    whole = summarise(res, label)
    whole["pipeline_s"] = round(t_pipeline, 1)

    excerpt_runs = {}
    if args.excerpt_out:
        ex_rec = hr.load_sample(args.excerpt_out.stem) if args.excerpt_out.parent == hr.SAMPLES_DIR else \
            hr.validate_recording(json.loads(args.excerpt_out.read_text()), name=args.excerpt_out.stem)
        for tag, path in (("fold_model_without_S1", str(fold_path)), ("shipped_model_trained_on_all_15", None)):
            r = hr.run(ex_rec, model_path=path)
            excerpt_runs[tag] = summarise(r, ex_rec.reference_bpm)
            excerpt_runs[tag]["learned_model"] = r.provenance["learned_model"].get("version")

    doc = {
        "what": "the research pipeline (care/heart_research.py) on a PPG-DaLiA subject held out from the learned model",
        "held_out_subject": HELD_OUT,
        "why_a_fold_model": "the shipped model was trained on all 15 subjects, so no subject is held out from it; the "
                            "fold model is the leave-one-subject-out model for this subject, same settings and seed",
        "fold_model": {"sha256": hashlib.sha256(fold_text.encode()).hexdigest(), "bytes": len(fold_text.encode()),
                       "train_rows": int(len(y)), "max_abs_probability_difference_numpy_vs_sklearn": max_dp,
                       "kept_in_repository": False},
        "reproduces_evaluation_script": agree,
        "results_json_loso_mae_bpm": reference,
        "whole_recording": whole,
        "excerpt": {"file": excerpt, "runs": excerpt_runs},
        "thresholds": {"motion_reject_g": hr.MOTION_REJECT_G, "classical_min_confidence": hp.he.MIN_CONFIDENCE,
                       "learned_min_probability": hl.MIN_PROBABILITY,
                       "note": "fixed before this evaluation; not tuned on it"},
        "runtime": {"total_s": round(time.time() - t_start, 1),
                    "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, no GPU, shared with other jobs",
                    "versions": {"python": platform.python_version(), "numpy": np.__version__,
                                 "scikit-learn": skl_version}},
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    hp.log(json.dumps({"agree": agree, "reference": reference, "whole": whole}, indent=1))
    hp.log(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
