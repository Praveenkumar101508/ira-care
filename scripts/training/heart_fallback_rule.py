# IRA CARE: IRA Care Heart research pipeline, what to report when the learned selector is unsure.
#
#   python3 -I scripts/training/heart_fallback_rule.py --data-dir /path/outside/the/repo \
#       --out docs/ai/heart/fallback_rule_analysis.json
#
# The research pipeline (care/heart_research.py) reported the classical estimate whenever the
# learned selector's probability was below 0.5 and the classical confidence was at least 0.3.
# The held-out check on S1 and the independent GalaxyPPG evaluation both found those answers poor.
# This script decides between two replacements on PPG-DaLiA's own leave-one-subject-out data:
#
#   unavailable  the window is "unavailable" (reason learned_low_confidence);
#   agreement    the classical estimate is still reported, but only when it is within
#                AGREEMENT_BPM (5 bpm, the selector's own definition of a correct candidate,
#                fixed before running) of the learned candidate; otherwise "unavailable".
#
# The decision rule was fixed before running and is written into the output: the agreement rule
# is adopted only if, pooled over the 14 subjects other than S1 (S1 is the research pipeline's
# held-out subject, so it is reported but not used to decide), the windows it would add score no
# worse than the confident learned answers on the same subjects. GalaxyPPG is not used here; it
# stays an independent test.
#
# Per window, as the pipeline decides (the pipeline reproduces this path exactly on S1; see
# heart_research_holdout.py): a classical input refusal or band-limited accelerometer RMS above
# MOTION_REJECT_G makes the window unavailable; a learned probability of at least 0.5 is reported
# as learned; otherwise the rule under test applies. Fold models are trained exactly as in
# heart_ppg_dalia.py (same features, settings and seed) and are not kept.
#
# PPG-DaLiA: UCI dataset 495, CC BY 4.0 (Reiss, Indlekofer, Schmidt, Van Laerhoven, Sensors 2019,
# 19(14), 3079). Only summary numbers are written; nothing here is a measurement on any person.
from __future__ import annotations

import argparse
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
AGREEMENT_BPM = 5.0
DECISION_RULE = ("adopt the agreement rule only if, pooled over the 14 subjects other than S1, the fallback windows "
                 f"where the classical estimate is within {AGREEMENT_BPM:g} bpm of the learned candidate have an MAE "
                 "no higher than the confident learned answers on the same subjects; otherwise report unavailable")


def motion_pass(args):
    """Band-limited accelerometer RMS per window, as the pipeline computes it."""
    cache, subject = args
    d = hp.load_subject(Path(cache), subject)
    n, _, _ = hp.windows(d)
    return subject, np.array([hr.motion_rms_g(np.asarray(hp.acc_win(d, i), float), float(hp.ACC_HZ))
                              for i in range(n)])


def _mae(err):
    return {"mae_bpm": round(float(np.mean(err)), 2) if len(err) else None, "windows": int(len(err))}


def categorise(label, cls, conf, est, prob, motion):
    """Masks over the windows: what the pipeline does with each, before the rule under test."""
    refused = ~np.isfinite(cls)                       # the classical input checks refused the window
    usable = ~refused & (motion <= hr.MOTION_REJECT_G)
    learned = usable & np.isfinite(prob) & (prob >= hl.MIN_PROBABILITY)
    unsure = usable & ~learned                        # low probability, or no candidates
    classical_ok = np.isfinite(conf) & (conf >= hp.he.MIN_CONFIDENCE)
    fallback = unsure & classical_ok                  # what the old rule reported as classical
    agree = fallback & np.isfinite(est) & (np.abs(cls - est) <= AGREEMENT_BPM)
    return {"learned": learned, "fallback": fallback, "agree": agree, "unsure": unsure}


def summarise(rows):
    """Pooled numbers over a list of per-subject dicts."""
    cat = lambda k: np.concatenate([r[k] for r in rows])  # noqa: E731
    label, cls, est = cat("label"), cat("cls"), cat("est")
    m = {k: cat("m_" + k) for k in ("learned", "fallback", "agree", "unsure")}
    n = len(label)
    learned_err = np.abs(est[m["learned"]] - label[m["learned"]])
    fb_err = np.abs(cls[m["fallback"]] - label[m["fallback"]])
    ag_err = np.abs(cls[m["agree"]] - label[m["agree"]])
    dis = m["fallback"] & ~m["agree"]
    dis_err = np.abs(cls[dis] - label[dis])
    old_err = np.concatenate([learned_err, fb_err])
    ag_rule_err = np.concatenate([learned_err, ag_err])
    per_subject_fb = [float(np.mean(np.abs(r["cls"][r["m_fallback"]] - r["label"][r["m_fallback"]])))
                      for r in rows if r["m_fallback"].any()]
    return {
        "windows": int(n),
        "confident_learned": _mae(learned_err),
        "classical_fallback_old_rule": _mae(fb_err),
        "classical_fallback_per_subject_mae_bpm_mean_sd": [round(float(np.mean(per_subject_fb)), 2),
                                                           round(float(np.std(per_subject_fb)), 2)],
        "fallback_agreeing_within_5_bpm": _mae(ag_err),
        "fallback_disagreeing": _mae(dis_err),
        "learned_unsure_windows": int(m["unsure"].sum()),
        "rules": {
            "old_classical_fallback": {**_mae(old_err), "coverage": round(len(old_err) / n, 4)},
            "agreement_within_5_bpm": {**_mae(ag_rule_err), "coverage": round(len(ag_rule_err) / n, 4)},
            "unavailable": {**_mae(learned_err), "coverage": round(len(learned_err) / n, 4)},
        },
        "share_of_reported_windows_over_10_bpm_out": {
            "old_classical_fallback": round(float(np.mean(old_err > 10)), 4),
            "agreement_within_5_bpm": round(float(np.mean(ag_rule_err > 10)), 4),
            "unavailable": round(float(np.mean(learned_err > 10)), 4),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "ai" / "heart" / "fallback_rule_analysis.json")
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
        per = {r["subject"]: r for r in ex.map(hp.subject_pass, [(str(cache), s) for s in hp.SUBJECTS])}
        motion = dict(ex.map(motion_pass, [(str(cache), s) for s in hp.SUBJECTS]))
    hp.log("per-subject passes done")

    rows = {}
    for s in hp.SUBJECTS:
        tr = [per[o] for o in hp.SUBJECTS if o != s]
        clf = HistGradientBoostingClassifier(**hp.LEARNED_PARAMS).fit(np.vstack([r["X"] for r in tr]),
                                                                      np.concatenate([r["y"] for r in tr]))
        te = per[s]
        p = clf.predict_proba(te["X"])[:, 1] if len(te["X"]) else np.zeros(0)
        est, prob = hp.select_learned(p, te["win"], te["cand_bpm"], te["n"])
        est = np.round(est, 1)                         # the pipeline rounds the selection to 0.1 bpm
        label = np.asarray(te["label"], float)
        cls, conf = te["out"]["classical"], te["out"]["conf"]
        m = categorise(label, cls, conf, est, prob, motion[s][:te["n"]])
        rows[s] = {"label": label, "cls": cls, "est": est, **{"m_" + k: v for k, v in m.items()}}
        hp.log(f"{s}: fold done, fallback {int(m['fallback'].sum())}, agreeing {int(m['agree'].sum())}")

    decide = summarise([rows[s] for s in hp.SUBJECTS if s != HELD_OUT])
    ag, lr = decide["fallback_agreeing_within_5_bpm"]["mae_bpm"], decide["confident_learned"]["mae_bpm"]
    adopt = ag is not None and ag <= lr
    doc = {
        "what": "which answer the research pipeline should give when the learned selector is unsure, decided on "
                "PPG-DaLiA leave-one-subject-out data",
        "decision_rule_fixed_before_running": DECISION_RULE,
        "agreement_bpm": AGREEMENT_BPM,
        "decision_subjects": [s for s in hp.SUBJECTS if s != HELD_OUT],
        "decision": {"agreement_rule_adopted": bool(adopt),
                     "because": f"agreeing fallback windows {ag} bpm against confident learned {lr} bpm (pooled, 14 subjects)",
                     "rule": "agreement_within_5_bpm" if adopt else "unavailable"},
        "fourteen_subjects_without_S1": decide,
        "S1_held_out_not_used_to_decide": summarise([rows[HELD_OUT]]),
        "all_fifteen_subjects": summarise([rows[s] for s in hp.SUBJECTS]),
        "thresholds": {"learned_min_probability": hl.MIN_PROBABILITY, "classical_min_confidence": hp.he.MIN_CONFIDENCE,
                       "motion_reject_g": hr.MOTION_REJECT_G},
        "notes": ["windows are all PPG-DaLiA label windows; MAE against the dataset's ECG-derived label",
                  "fold models trained as in heart_ppg_dalia.py (same features, settings, seed) and not kept",
                  "GalaxyPPG was not used: it stays an independent test"],
        "runtime": {"total_s": round(time.time() - t_start, 1),
                    "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, no GPU, shared with other jobs",
                    "versions": {"python": platform.python_version(), "numpy": np.__version__,
                                 "scikit-learn": skl_version}},
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    hp.log(json.dumps(doc["decision"]))
    hp.log(f"wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
