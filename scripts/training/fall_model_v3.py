# IRA CARE: fall model v3, trained only on data whose original licence permits commercial use
# (BITS-2, Zenodo 10013090, CC BY 4.0; UCI HAR, UCI 240, CC BY 4.0), as in fall_model_v2.py.
#
# What changes from v2. v2 could only VETO the rule detector's alarms, so it could never catch
# a fall the rule missed, and it lost 5 of 316 falls the rule caught. On BITS-2 the rule misses
# 121 of 316 falls: 59 never reach 2.5 g at the wrist, and 62 are cleared because the wrist moves
# within the 20 s stillness window. v3 is designed to RECOVER those, with the design fixed
# before any held-out result was seen:
#
#   1. candidate: the first sample at or above CANDIDATE_G = 2.0 g (the rule uses 2.5 g);
#   2. the peak: the largest magnitude in the 1.5 s settle period after the candidate;
#   3. the model scores the 4 s window whose index 100 is that peak (2 s before, 2 s after);
#   4. at or above the threshold, the watch's 30 s countdown asks "are you OK?" first.
#   No 20 s stillness requirement: the countdown, which asks the person, replaces it.
#
# The gate, on held-out subjects, against the rule detector on the SAME 316 falls and the SAME
# daily-activity recordings: sensitivity at least the rule's, and fewer false alarms per hour.
# Each fold's threshold is chosen on validation subjects drawn from its own training subjects:
# the highest threshold at which the validation falls are caught at least as often as the rule
# catches them there, plus a margin of MARGIN. Nothing is chosen on a test fold.
#
# Usage (data stays outside the repository; run with -I because it reads downloaded files):
#   python3 -I scripts/training/fall_model_v3.py --data-dir <scratch>/fall2 --out <scratch>/fall3
import argparse
import importlib.util
import json
import math
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("fall_model_v2", os.path.join(HERE, "fall_model_v2.py"))
v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v2)

SEED = 20261126
CANDIDATE_G = 2.0
SETTLE = v2.SETTLE_MS * v2.RATE_HZ // 1000   # 75 samples
MARGIN = 0.05
WIN, IMPACT_AT, RATE_HZ = v2.WIN, v2.IMPACT_AT, v2.RATE_HZ
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def candidates(acc):
    """The watch's v3 candidate stream (ModelFallDetector, CANDIDATE_G): the first sample at
    or above 2.0 g opens a candidate; its peak is the largest magnitude in the next 1.5 s; the
    model's window is aligned at the peak; the next candidate can open only after that window's
    decision point (peak + 2 s). Returns [{"start", "peak"}]."""
    mag = np.linalg.norm(acc, axis=1)
    out, i, n = [], 0, len(mag)
    while i < n:
        if mag[i] >= CANDIDATE_G:
            seg = mag[i:min(n, i + SETTLE + 1)]
            p = i + int(np.argmax(seg))
            out.append({"start": i, "peak": p})
            i = p + (WIN - IMPACT_AT) + 1
        else:
            i += 1
    return out


def build(recs):
    """Fall files: the fall's candidate is the one whose settle period holds the file's largest
    magnitude (the same rule v2 used to find the fall); a fall whose maximum stays below 2.0 g
    has no candidate and counts as missed. Other candidates in fall files are left out (their
    label is unknown: getting up, the next trial). Daily-activity files: every candidate is a
    negative. The rule's own outcome on each fall file is v2's, so both are scored on the same
    316 falls and the same daily-activity recordings."""
    falls, adl, stats = [], [], {"fall_files": 0, "fall_files_without_candidate": 0, "adl_candidates": 0, "adl_seconds": 0.0}
    for r in recs:
        a = r["acc"]; mag = np.linalg.norm(a, axis=1)
        cands = candidates(a)
        if r["kind"] == "fall":
            stats["fall_files"] += 1
            k = int(np.argmax(mag))
            evs = v2.rule_events(a)
            match = [e for e in evs if e["impact"] <= k <= e["impact"] + SETTLE]
            rule = match[0]["outcome"] if match else "no_impact"
            mine = [c for c in cands if c["start"] <= k <= c["start"] + SETTLE]
            if mine:
                w, _ = v2.window_at(a, mine[0]["peak"])
                falls.append({**r, "w": v2.features(w), "cand": 1, "rule": rule})
            else:
                stats["fall_files_without_candidate"] += 1
                w, _ = v2.window_at(a, k)
                falls.append({**r, "w": v2.features(w), "cand": 0, "rule": rule})
        else:
            stats["adl_seconds"] += len(a) / RATE_HZ
            for c in cands:
                w, _ = v2.window_at(a, c["peak"])
                adl.append({**r, "w": v2.features(w)})
            stats["adl_candidates"] += len(cands)
            for e in v2.rule_events(a):
                adl.append({**r, "w": None, "rule_event": e["outcome"]})
    return falls, adl, stats


def choose_threshold(p_val_falls, cand_val, rule_val_sens):
    """The highest threshold (2 decimals, in [0.05, 0.95]) at which the validation falls are
    caught at least as often as the rule catches them, plus MARGIN. Falls without a candidate
    count as missed, as they would on the watch."""
    target = min(1.0, rule_val_sens + MARGIN)
    best = 0.05
    for t in np.arange(0.95, 0.049, -0.01):
        t = round(float(t), 2)
        caught = np.mean((p_val_falls >= t) & (cand_val == 1)) if len(p_val_falls) else 0.0
        if caught >= target:
            best = t
            break
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rng = np.random.default_rng(SEED)

    bits2 = v2.fetch(v2.BITS2_URL, os.path.join(args.data_dir, "bits2_full_dataset.zip"), v2.BITS2_SHA256)
    har = v2.fetch(v2.HAR_URL, os.path.join(args.data_dir, "uci_har.zip"), v2.HAR_SHA256)
    recs = [r for r in v2.load_bits2(bits2) if r["subject"] not in v2.EXCLUDED_SUBJECTS]
    falls, adl_all, stats = build(recs)
    adl = [e for e in adl_all if e["w"] is not None]
    rule_adl = [e for e in adl_all if e["w"] is None]
    har_w = v2.har_windows(v2.load_har(har))
    log(f"falls {len(falls)} ({stats['fall_files_without_candidate']} without a 2.0 g candidate); "
        f"ADL candidates {len(adl)}; rule ADL impacts {len(rule_adl)}; ADL hours {stats['adl_seconds'] / 3600:.3f}; HAR windows {len(har_w)}")

    import tensorflow as tf
    tf.keras.utils.set_random_seed(SEED); tf.config.experimental.enable_op_determinism()

    subjects = sorted({r["subject"] for r in recs}, key=lambda s: int(s[4:]))
    perm = list(rng.permutation(subjects))
    folds = [set(perm[i::args.folds]) for i in range(args.folds)]
    har_perm = list(rng.permutation(sorted({w["subject"] for w in har_w})))
    har_folds = [set(har_perm[i::args.folds]) for i in range(args.folds)]
    sel = lambda items, S: [i for i, it in enumerate(items) if it["subject"] in S]  # noqa: E731
    rule_caught = lambda f: int(f["rule"] in ("fall", "open"))  # noqa: E731

    oof_f = np.full(len(falls), np.nan); oof_a = np.full(len(adl), np.nan); oof_h = np.full(len(har_w), np.nan)
    thr_f = np.zeros(len(falls)); thr_a = np.zeros(len(adl)); thr_h = np.zeros(len(har_w))
    fold_info = []
    for k in range(args.folds):
        test = folds[k]; rest = [s for s in perm if s not in test]
        val = set(rest[: max(2, len(rest) // 5)]); trn = set(rest) - val
        htest = har_folds[k]
        pos = lambda S: [falls[i]["w"] for i in sel(falls, S) if falls[i]["cand"] == 1]  # noqa: E731
        neg = lambda S: [adl[i]["w"] for i in sel(adl, S)]  # noqa: E731
        har_trn = [w["w"] for w in har_w if w["subject"] not in htest]
        m_in, mu, sd, _ = v2.train(tf, pos(trn), neg(trn), [], har_trn, rng, args.epochs)
        vi = sel(falls, val)
        p_val = v2.predict(m_in, [falls[i]["w"] for i in vi], mu, sd)
        cand_val = np.array([falls[i]["cand"] for i in vi])
        rule_val = float(np.mean([rule_caught(falls[i]) for i in vi])) if vi else 0.0
        t = choose_threshold(p_val, cand_val, rule_val)
        model, mu, sd, tinfo = v2.train(tf, pos(set(rest)), neg(set(rest)), [], har_trn, rng, args.epochs)
        fi = sel(falls, test); ai = sel(adl, test); hi = [i for i, w in enumerate(har_w) if w["subject"] in htest]
        oof_f[fi] = v2.predict(model, [falls[i]["w"] for i in fi], mu, sd); thr_f[fi] = t
        oof_a[ai] = v2.predict(model, [adl[i]["w"] for i in ai], mu, sd); thr_a[ai] = t
        oof_h[hi] = v2.predict(model, [har_w[i]["w"] for i in hi], mu, sd); thr_h[hi] = t
        fold_info.append({"fold": k, "threshold": t, "rule_validation_sensitivity": round(rule_val, 4),
                          "test_subjects": sorted(test, key=lambda s: int(s[4:])), **tinfo})
        log(f"fold {k}: threshold {t} (rule on validation {rule_val:.3f})")

    hours = stats["adl_seconds"] / 3600.0
    cand_f = np.array([f["cand"] for f in falls])
    v3_f = ((oof_f >= thr_f) & (cand_f == 1)).astype(int)
    v3_a = (oof_a >= thr_a).astype(int)
    rule_f = np.array([rule_caught(f) for f in falls])
    rule_a = np.array([int(e["rule_event"] in ("fall", "open")) for e in rule_adl])

    def summary(f_hits, adl_alarms, n_adl_events):
        tp = int(f_hits.sum()); fn = len(f_hits) - tp; fp = int(adl_alarms); tn = n_adl_events - fp
        sens = tp / len(f_hits); spec = tn / max(1, n_adl_events); prec = tp / max(1, tp + fp)
        return {"falls_caught": tp, "falls_missed": fn, "sensitivity": round(sens, 4),
                "alarms_on_adl": fp, "adl_events": n_adl_events, "specificity_on_adl_events": round(spec, 4),
                "precision": round(prec, 4), "alarms_per_hour_of_adl": round(fp / hours, 2)}

    rule = summary(rule_f, int(rule_a.sum()), len(rule_a))
    v3 = summary(v3_f, int(v3_a.sum()), len(v3_a))
    gate = {"sensitivity_at_least_the_rule": v3["sensitivity"] >= rule["sensitivity"],
            "fewer_alarms_per_hour_than_the_rule": v3["alarms_per_hour_of_adl"] < rule["alarms_per_hour_of_adl"]}
    gate["passed"] = gate["sensitivity_at_least_the_rule"] and gate["fewer_alarms_per_hour_than_the_rule"]
    # The shipped pipeline (secondary, specified after the primary result): v3 OR the rule's own
    # full 20 s stillness path, kept as a backstop so the watch never drops a fall the rule detector
    # would raise for a person who stays still. Scored two ways, because BITS-2 recordings often end
    # before 20 s: (a) only a COMPLETE 20 s still window counts for the rule, (b) a recording that
    # ended before 20 s without the person moving also counts (the convention the rule is scored with).
    # Alarms on daily activity are bounded above by the sum: the two paths may alarm on the same event.
    def union(rule_states):
        rf = np.array([int(f["rule"] in rule_states) for f in falls])
        ra = int(sum(int(e["rule_event"] in rule_states) for e in rule_adl))
        hits = (v3_f | rf).astype(int)
        return {"rule_outcomes_counted": sorted(rule_states), "falls_caught": int(hits.sum()),
                "sensitivity": round(float(hits.mean()), 4),
                "alarms_on_adl_at_most": int(v3_a.sum()) + ra,
                "alarms_per_hour_of_adl_at_most": round((int(v3_a.sum()) + ra) / hours, 2),
                "falls_the_rule_caught_that_this_misses": int(((rule_f == 1) & (hits == 0)).sum())}
    shipped = {"a_complete_20_s_still_window_only": union({"fall"}),
               "b_also_recordings_that_ended_still": union({"fall", "open"})}
    log(f"shipped pipeline {shipped}")
    both = {"rule_only": int(((rule_f == 1) & (v3_f == 0)).sum()), "v3_only": int(((rule_f == 0) & (v3_f == 1)).sum()),
            "both": int(((rule_f == 1) & (v3_f == 1)).sum()), "neither": int(((rule_f == 0) & (v3_f == 0)).sum())}
    har_flags = int((oof_h >= thr_h).sum())
    by_activity = {}
    for a in sorted({f["activity"] for f in falls}):
        ii = [i for i, f in enumerate(falls) if f["activity"] == a]
        by_activity[f"fall{a}"] = {"n": len(ii), "rule": int(rule_f[ii].sum()), "v3": int(v3_f[ii].sum())}
    # Post hoc, NOT held out: one shared threshold over the out-of-fold scores, for the trade-off.
    sweep = {}
    for t in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        sweep[str(t)] = {"sensitivity": round(float(((oof_f >= t) & (cand_f == 1)).mean()), 4),
                         "alarms_per_hour_of_adl": round(int((oof_a >= t).sum()) / hours, 2)}
    log(f"rule {rule}"); log(f"v3   {v3}"); log(f"gate {gate}")

    # Final model on every subject; threshold = median of the fold thresholds.
    t_final = float(np.median([f["threshold"] for f in fold_info]))
    model, mu, sd, tinfo = v2.train(tf, [f["w"] for f in falls if f["cand"] == 1], [e["w"] for e in adl], [],
                                    [w["w"] for w in har_w], rng, args.epochs)
    export = {}
    keras_path = os.path.join(args.out, "fall_model_v3.keras"); model.save(keras_path)
    conv = tf.lite.TFLiteConverter.from_keras_model(model); conv.optimizations = [tf.lite.Optimize.DEFAULT]
    tfl = conv.convert()
    with open(os.path.join(args.out, "fall_model_v3.tflite"), "wb") as fh:
        fh.write(tfl)
    interp = tf.lite.Interpreter(model_content=tfl, num_threads=1); interp.allocate_tensors()
    inp = interp.get_input_details()[0]; outp = interp.get_output_details()[0]
    test_ws = [f["w"] for f in falls[:100]] + [e["w"] for e in adl[:100]]
    pk = v2.predict(model, test_ws, mu, sd); agree = 0; reference = []
    for i, w in enumerate(test_ws):
        interp.set_tensor(inp["index"], ((w - mu) / sd)[None].astype(np.float32)); interp.invoke()
        p = float(interp.get_tensor(outp["index"]).ravel()[0])
        agree += int((p >= t_final) == (pk[i] >= t_final))
        if i in (0, 1, 2, 100, 101, 102):
            reference.append({"window": [[round(float(v), 6) for v in row] for row in w], "p": round(p, 6)})
    export = {"tflite_bytes": len(tfl), "tflite_agrees_with_keras": f"{agree}/{len(test_ws)}",
              "keras_bytes": os.path.getsize(keras_path), "params": int(model.count_params())}
    spec = {"threshold": round(t_final, 4), "mean": [float(x) for x in mu], "std": [float(x) for x in sd],
            "windowSamples": WIN, "rateHz": RATE_HZ, "candidateG": CANDIDATE_G, "mode": "recover",
            "provenance": "ira-fall-v3-licensed",
            "trained_on": "BITS-2 (Zenodo 10013090, CC BY 4.0) and UCI HAR (UCI 240, CC BY 4.0) only; see docs/ai/fall/MODEL_CARD_V3.md",
            "cv_sensitivity": v3["sensitivity"], "cv_alarms_per_hour_of_adl": v3["alarms_per_hour_of_adl"]}
    with open(os.path.join(args.out, "fall_model.json"), "w") as fh:
        json.dump(spec, fh, indent=1)
    with open(os.path.join(args.out, "reference_windows.json"), "w") as fh:
        json.dump({"threshold": spec["threshold"], "mean": spec["mean"], "std": spec["std"], "cases": reference}, fh)

    results = {
        "design": {"candidate_g": CANDIDATE_G, "settle_samples": SETTLE, "alignment": "window index 100 = the peak in the settle period",
                   "decision": "model at or above the fold threshold; no 20 s stillness requirement; the watch's countdown asks first",
                   "threshold_rule": f"per fold, the highest threshold at which validation falls are caught at least as often as the rule plus {MARGIN}",
                   "fixed_before_held_out_results": True},
        "data": {"bits2": "Zenodo 10013090, CC BY 4.0", "uci_har": "UCI 240, CC BY 4.0, negatives only", **stats,
                 "adl_hours": round(hours, 3), "excluded_subjects": sorted(v2.EXCLUDED_SUBJECTS)},
        "protocol": {"folds": args.folds, "epochs": args.epochs, "seed": SEED, "folds_detail": fold_info,
                     "split": "by BITS-2 subject and by UCI HAR subject; thresholds chosen on validation subjects inside each fold's training subjects"},
        "held_out": {"rule_detector": rule, "v3": v3, "gate": gate, "falls_caught_by": both,
                     "shipped_pipeline_v3_or_rule_stillness": shipped,
                     "uci_har_windows_flagged_cross_dataset_negatives": f"{har_flags}/{len(har_w)}",
                     "falls_by_activity": by_activity},
        "post_hoc_shared_threshold_sweep_not_held_out": sweep,
        "final_model": {"threshold": t_final, **export, **tinfo},
        "seconds": round(time.time() - T0, 1),
    }
    with open(os.path.join(args.out, "results_v3.json"), "w") as fh:
        json.dump(results, fh, indent=2)
    log("done")


if __name__ == "__main__":
    main()
