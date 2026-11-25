# IRA CARE: fall model v2, trained only on data whose original licence permits commercial use.
#
#   BITS-2 Dataset for Fall Detection (Nandi and K.R. Anupama, BITS Pilani Goa), the
#   authors' own Zenodo record 10013090, CC BY 4.0: a LEFT-WRIST MPU6500, 41 volunteers,
#   16 daily activities and 8 simulated falls.
#   UCI HAR (UCI 240, CC BY 4.0): waist smartphone, 30 other people, daily-activity negatives.
#
# The output is a drop-in for the watch's ModelFallDetector: a (200 x 4) window of
# x, y, z and |a| in g at 50 Hz, normalised with the mean and std written to the spec json,
# returning P(fall) from a sigmoid. The window is aligned the way the watch builds it: the
# rule detector's impact sample (the first >= 2.5 g) sits AFTER_IMPACT_MS = 2 s before the
# window's end, so at index 100.
#
# Evaluation is subject-independent: 5 folds over BITS-2 subjects (and UCI HAR subjects),
# each fold's threshold chosen on validation subjects drawn from its own training subjects,
# never on the test fold. The rule detector (care.ira.watch.sensors.FallDetector) is
# re-implemented below line for line and scored on the same windows and the same events.
#
# Usage (data stays outside the repository; run with -I because it reads downloaded files):
#   python3 -I scripts/training/fall_model_v2.py --data-dir <scratch>/fall2 --out <scratch>/fall2/out
# It downloads the two archives into --data-dir when they are missing and checks SHA-256.
#
# Nothing here is a medical claim: these are simulated falls by young adult volunteers on a
# research wrist band, not older adults, not the IRA watch.
import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import time
import urllib.request
import zipfile

import numpy as np

SEED = 20261125
RATE_HZ = 50
WIN = 200                      # 4 s at 50 Hz, FallModelSpec.windowSamples
AFTER_IMPACT = 100             # ModelFallDetector.AFTER_IMPACT_MS = 2000 ms -> 100 samples
IMPACT_AT = WIN - AFTER_IMPACT  # index of the impact sample inside the window
G = 9.80665

BITS2_URL = "https://zenodo.org/api/records/10013090/files/full_dataset.zip/content"
BITS2_SHA256 = "346cd2748ffaa006e775d1876a674f531430dc0e090bf0766dffb4437e137785"
HAR_URL = "https://archive.ics.uci.edu/static/public/240/human+activity+recognition+using+smartphones.zip"
HAR_SHA256 = "c00b803081a5c797cd5e4b83700a9810b38d53d9d84e01917e090e1fdbc81031"

# FallSettings defaults and FallDetector constants (watch/app/.../sensors/FallDetector.kt)
IMPACT_G, STILL_S, STILL_THR_G, TOLERATED, SETTLE_MS = 2.5, 20, 0.3, 3, 1500

# Subjects excluded because their own timestamps show they were not recorded at 50 Hz:
# user1's files hold 6 to 7 samples per (truncated) one-second timestamp bucket.
EXCLUDED_SUBJECTS = {"user1"}

T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def fetch(url, path, sha):
    if not os.path.exists(path):
        log(f"downloading {url}")
        urllib.request.urlretrieve(url, path)
    with open(path, "rb") as fh:
        h = hashlib.sha256(fh.read()).hexdigest()
    if h != sha:
        raise SystemExit(f"checksum mismatch for {path}: {h}")
    return path


# ── BITS-2 ──────────────────────────────────────────────────────────────────────
# Each csv: t,x,y,z,a,label rows; label acc (linear), acg (with gravity), gyro, mgm, hrt.
# Only `acg` (acceleration including gravity, m/s^2) is what a watch's TYPE_ACCELEROMETER
# gives. Timestamps were saved with 3 significant figures, so sample order is the only time
# reference; exact duplicate rows (3,299 of 627,774) are dropped.
def load_bits2(zip_path):
    zf = zipfile.ZipFile(zip_path)
    recs = []
    for n in sorted(zf.namelist()):
        m = re.search(r"Dataset/(adl|fall)/(user\d+)/user\d+_(adl|fall)(\d+)\.csv$", n)
        if not m:
            continue
        kind, subj, act = m.group(1), m.group(2), int(m.group(4))
        seen, rows = set(), []
        for r in csv.reader(io.StringIO(zf.read(n).decode("utf-8", "ignore"), newline="")):
            if len(r) >= 6 and r[5] == "acg":
                key = tuple(r[:4])
                if key in seen:
                    continue
                seen.add(key)
                try:
                    rows.append([float(r[1]), float(r[2]), float(r[3])])
                except ValueError:
                    pass
        if len(rows) < 10:
            continue
        recs.append({"kind": kind, "subject": subj, "activity": act, "acc": np.asarray(rows, np.float32) / G})
    return recs


# ── UCI HAR: stitch the 50 %-overlapping 128-sample windows back into continuous signals ──
def load_har(zip_path):
    hz = zipfile.ZipFile(zip_path)
    inner = [n for n in hz.namelist() if n.endswith(".zip")]
    if inner:
        hz = zipfile.ZipFile(io.BytesIO(hz.read(inner[0])))

    def arr(name):
        n = next(x for x in hz.namelist() if x.endswith(name))
        return np.loadtxt(io.StringIO(hz.read(n).decode()))

    segs = []
    for split in ("train", "test"):
        xs, ys, zs = (arr(f"Inertial Signals/total_acc_{a}_{split}.txt") for a in "xyz")
        subj = arr(f"/subject_{split}.txt").astype(int)
        act = arr(f"/y_{split}.txt").astype(int)
        cur = None
        for i in range(len(xs)):
            w = np.stack([xs[i], ys[i], zs[i]], 1).astype(np.float32)
            if (cur is not None and cur["subject"] == f"har{subj[i]}" and cur["activity"] == act[i]
                    and np.allclose(cur["acc"][-64:], w[:64], atol=1e-6)):
                cur["acc"] = np.vstack([cur["acc"], w[64:]])
            else:
                cur = {"subject": f"har{subj[i]}", "activity": int(act[i]), "acc": w}
                segs.append(cur)
    return segs


def features(a):
    return np.hstack([a, np.linalg.norm(a, axis=1, keepdims=True)]).astype(np.float32)


def window_at(acc, impact):
    """The 200 samples the watch would hold 2 s after an impact at `impact`. Samples before
    the start of the recording or after its end are edge-padded (counted in the results)."""
    s = impact - IMPACT_AT
    idx = np.clip(np.arange(s, s + WIN), 0, len(acc) - 1)
    padded = int(((np.arange(s, s + WIN) < 0) | (np.arange(s, s + WIN) >= len(acc))).sum())
    return acc[idx], padded


# ── The rule detector, line for line from FallDetector.feed (samples 20 ms apart) ──────────
def rule_events(acc):
    """Stream a recording through FallDetector. Returns one dict per impact:
    {impact, peak_idx, outcome in {'fall', 'cleared', 'open'}, still_samples_seen}.
    'open' = the recording ended before the 20 s still window was complete and the
    person had not moved more than tolerated: the rule had not cleared it."""
    mag = np.linalg.norm(acc, axis=1)
    out, impact_at, peak, moving, cur = [], -1, 0.0, 0, None
    for i, g in enumerate(mag):
        t = i * 1000 // RATE_HZ
        if impact_at < 0:
            if g >= IMPACT_G:
                impact_at, peak, moving = t, g, 0
                cur = {"impact": i, "peak_idx": i, "outcome": "open", "still_samples_seen": 0}
                out.append(cur)
            continue
        since = t - impact_at
        if since <= SETTLE_MS:
            if g > peak:
                peak = g; cur["peak_idx"] = i
            continue
        cur["still_samples_seen"] += 1
        if abs(g - 1.0) > STILL_THR_G:
            moving += 1
            if moving > TOLERATED:
                cur["outcome"] = "cleared"; impact_at = -1
                continue
        if since >= STILL_S * 1000:
            cur["outcome"] = "fall"; impact_at = -1
    return out


def rule_on_window(w):
    """The rule on a 4 s window alone (as kaggle_fall_model.py scored it): impact, 1.5 s
    settle, then stillness over what the window still shows."""
    mag = w[:, 3]
    hits = np.where(mag >= IMPACT_G)[0]
    if len(hits) == 0:
        return 0
    after = mag[int(hits[0]) + SETTLE_MS * RATE_HZ // 1000 + 1:]
    if len(after) < RATE_HZ // 2:
        return 0
    return int((np.abs(after - 1.0) > STILL_THR_G).sum() <= TOLERATED)


# ── Windows ────────────────────────────────────────────────────────────────────────────
def build_bits2(recs):
    """Fall files: the fall is the rule impact whose settle period contains the file's
    largest magnitude; when no sample reaches 2.5 g the rule gate cannot fire, the fall is
    recorded as gate-missed and its model window is centred on the maximum.
    Other impacts inside fall files are left out (unknown label: getting up, next trial).
    ADL files: every rule impact is a negative event; plus plain ADL windows every 2 s."""
    falls, adl_events, adl_plain = [], [], []
    stats = {"fall_files": 0, "fall_files_gate_missed": 0, "fall_other_impacts_skipped": 0,
             "adl_files": 0, "adl_seconds": 0.0, "fall_windows_padded": 0, "fall_padded_samples": 0,
             "adl_event_windows_padded": 0, "adl_event_padded_samples": 0}
    for r in recs:
        a = r["acc"]; mag = np.linalg.norm(a, axis=1)
        evs = rule_events(a)
        if r["kind"] == "fall":
            stats["fall_files"] += 1
            k = int(np.argmax(mag))
            match = [e for e in evs if e["impact"] <= k <= e["impact"] + SETTLE_MS * RATE_HZ // 1000]
            if match:
                e = match[0]
                w, pad = window_at(a, e["impact"])
                falls.append({**r, "w": features(w), "gate": 1, "rule": e["outcome"], "still_seen": e["still_samples_seen"], "pad": pad})
                stats["fall_other_impacts_skipped"] += len(evs) - 1
            else:
                stats["fall_files_gate_missed"] += 1
                w, pad = window_at(a, k)
                falls.append({**r, "w": features(w), "gate": 0, "rule": "no_impact", "still_seen": 0, "pad": pad})
                stats["fall_other_impacts_skipped"] += len(evs)
            stats["fall_windows_padded"] += int(pad > 0); stats["fall_padded_samples"] += pad
        else:
            stats["adl_files"] += 1; stats["adl_seconds"] += len(a) / RATE_HZ
            for e in evs:
                w, pad = window_at(a, e["impact"])
                adl_events.append({**r, "w": features(w), "gate": 1, "rule": e["outcome"], "still_seen": e["still_samples_seen"], "pad": pad})
                stats["adl_event_windows_padded"] += int(pad > 0); stats["adl_event_padded_samples"] += pad
            for s in range(0, max(1, len(a) - WIN + 1), 2 * RATE_HZ):
                w, _ = window_at(a, s + IMPACT_AT)
                adl_plain.append({**r, "w": features(w)})
    return falls, adl_events, adl_plain, stats


def har_windows(segs):
    out = []
    for s in segs:
        a = s["acc"]
        for st in range(0, len(a) - WIN + 1, 2 * RATE_HZ):
            out.append({"subject": s["subject"], "w": features(a[st:st + WIN])})
    return out


# ── Augmentation ───────────────────────────────────────────────────────────────────────
def random_rotation(rng):
    q = rng.normal(size=4); q /= np.linalg.norm(q)
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]], np.float32)


def rotate(w, R):
    return features(w[:, :3] @ R.T)


def shift(w, k):
    if k == 0:
        return w
    idx = np.clip(np.arange(WIN) - k, 0, WIN - 1)
    return w[idx]


def augment(ws, rng, copies, max_shift):
    out = []
    for w in ws:
        for _ in range(copies):
            out.append(rotate(shift(w, int(rng.integers(-max_shift, max_shift + 1))), random_rotation(rng)))
    return out


# ── Model ──────────────────────────────────────────────────────────────────────────────
def make_model(tf):
    return tf.keras.Sequential([
        tf.keras.layers.Input((WIN, 4)),
        tf.keras.layers.Conv1D(16, 7, activation="relu", padding="same"), tf.keras.layers.MaxPool1D(4),
        tf.keras.layers.Conv1D(32, 5, activation="relu", padding="same"), tf.keras.layers.MaxPool1D(4),
        tf.keras.layers.Conv1D(32, 3, activation="relu", padding="same"), tf.keras.layers.GlobalMaxPool1D(),
        tf.keras.layers.Dense(16, activation="relu"), tf.keras.layers.Dropout(0.2),
        tf.keras.layers.Dense(1, activation="sigmoid"),
    ])


def train(tf, pos, neg_hard, neg_plain, neg_har, rng, epochs):
    P = augment(pos, rng, 12, 10)
    Nh = augment(neg_hard, rng, 4, 10)
    Np = augment(neg_plain, rng, 1, 0)
    Nr = augment(neg_har, rng, 1, 0)
    X = np.stack(P + Nh + Np + Nr); y = np.array([1] * len(P) + [0] * (len(Nh) + len(Np) + len(Nr)), np.float32)
    mean = X.reshape(-1, 4).mean(0); std = X.reshape(-1, 4).std(0) + 1e-6
    model = make_model(tf)
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="binary_crossentropy")
    cw = {0: 1.0, 1: float((y == 0).sum() / max(1, y.sum()))}
    perm = rng.permutation(len(X))
    model.fit((X[perm] - mean) / std, y[perm], epochs=epochs, batch_size=128, class_weight=cw, verbose=0)
    return model, mean, std, {"train_windows": len(X), "train_positive": int(y.sum())}


def predict(model, ws, mean, std):
    if not ws:
        return np.zeros(0)
    return model.predict((np.stack(ws) - mean) / std, verbose=0, batch_size=512).ravel()


def choose_threshold(p_fall_gated, p_adl_events):
    """The highest threshold (2 decimals, within [0.05, 0.95]) at which the model keeps every
    validation fall the rule gate saw: in the watch the model can only veto, so any vetoed
    fall would make the pipeline less sensitive than the rule alone. ADL scores are unused."""
    if len(p_fall_gated) == 0:
        return 0.5
    t = float(np.min(p_fall_gated)) - 1e-6
    return float(min(0.95, max(0.05, math.floor(t * 100) / 100)))


def conf(pred, truth):
    pred = np.asarray(pred).astype(int); truth = np.asarray(truth).astype(int)
    tp = int(((pred == 1) & (truth == 1)).sum()); fn = int(((pred == 0) & (truth == 1)).sum())
    fp = int(((pred == 1) & (truth == 0)).sum()); tn = int(((pred == 0) & (truth == 0)).sum())
    sens = tp / max(1, tp + fn); spec = tn / max(1, tn + fp); prec = tp / max(1, tp + fp)
    f1 = 2 * prec * sens / max(1e-9, prec + sens)
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn, "sensitivity": round(sens, 4), "recall": round(sens, 4),
            "specificity": round(spec, 4), "precision": round(prec, 4), "f1": round(f1, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rng = np.random.default_rng(SEED)

    bits2 = fetch(BITS2_URL, os.path.join(args.data_dir, "bits2_full_dataset.zip"), BITS2_SHA256)
    har = fetch(HAR_URL, os.path.join(args.data_dir, "uci_har.zip"), HAR_SHA256)
    recs = [r for r in load_bits2(bits2) if r["subject"] not in EXCLUDED_SUBJECTS]
    log(f"BITS-2 recordings {len(recs)} from {len({r['subject'] for r in recs})} subjects")
    falls, adl_events, adl_plain, stats = build_bits2(recs)
    log(f"BITS-2 windows: {stats}; ADL impact events {len(adl_events)}; plain ADL windows {len(adl_plain)}")
    har_w = har_windows(load_har(har))
    log(f"UCI HAR stitched windows {len(har_w)}")

    import tensorflow as tf
    tf.keras.utils.set_random_seed(SEED); tf.config.experimental.enable_op_determinism()

    subjects = sorted({r["subject"] for r in recs}, key=lambda s: int(s[4:]))
    perm = list(rng.permutation(subjects))
    folds = [set(perm[i::args.folds]) for i in range(args.folds)]
    har_subjects = sorted({w["subject"] for w in har_w})
    har_perm = list(rng.permutation(har_subjects))
    har_folds = [set(har_perm[i::args.folds]) for i in range(args.folds)]

    oof = {"fall": np.full(len(falls), np.nan), "adl_ev": np.full(len(adl_events), np.nan),
           "adl_plain": np.full(len(adl_plain), np.nan), "har": np.full(len(har_w), np.nan),
           "fall_rot": np.full(len(falls), np.nan), "adl_ev_rot": np.full(len(adl_events), np.nan)}
    thr = {"fall": np.zeros(len(falls)), "adl_ev": np.zeros(len(adl_events)), "adl_plain": np.zeros(len(adl_plain)), "har": np.zeros(len(har_w))}
    fold_info = []
    rot_fixed_f = [random_rotation(rng) for _ in falls]
    rot_fixed_a = [random_rotation(rng) for _ in adl_events]
    for k in range(args.folds):
        test = folds[k]; rest = [s for s in perm if s not in test]
        val = set(rest[: max(2, len(rest) // 5)]); trn = set(rest) - val
        htest = har_folds[k]
        sel = lambda items, S: [i for i, it in enumerate(items) if it["subject"] in S]
        # inner: train on trn, choose the threshold on val
        m_in, mu, sd, _ = train(tf, [falls[i]["w"] for i in sel(falls, trn)], [adl_events[i]["w"] for i in sel(adl_events, trn)],
                                [adl_plain[i]["w"] for i in sel(adl_plain, trn)],
                                [w["w"] for w in har_w if w["subject"] not in htest], rng, args.epochs)
        vf = [i for i in sel(falls, val) if falls[i]["gate"] == 1]
        t = choose_threshold(predict(m_in, [falls[i]["w"] for i in vf], mu, sd),
                             predict(m_in, [adl_events[i]["w"] for i in sel(adl_events, val)], mu, sd))
        # outer: train on all non-test subjects, score the test fold at t
        model, mu, sd, tinfo = train(tf, [falls[i]["w"] for i in sel(falls, set(rest))], [adl_events[i]["w"] for i in sel(adl_events, set(rest))],
                                     [adl_plain[i]["w"] for i in sel(adl_plain, set(rest))],
                                     [w["w"] for w in har_w if w["subject"] not in htest], rng, args.epochs)
        for name, items in (("fall", falls), ("adl_ev", adl_events), ("adl_plain", adl_plain)):
            idx = sel(items, test)
            oof[name][idx] = predict(model, [items[i]["w"] for i in idx], mu, sd); thr[name][idx] = t
        hidx = [i for i, w in enumerate(har_w) if w["subject"] in htest]
        oof["har"][hidx] = predict(model, [har_w[i]["w"] for i in hidx], mu, sd); thr["har"][hidx] = t
        fi = sel(falls, test); ai = sel(adl_events, test)
        oof["fall_rot"][fi] = predict(model, [rotate(falls[i]["w"], rot_fixed_f[i]) for i in fi], mu, sd)
        oof["adl_ev_rot"][ai] = predict(model, [rotate(adl_events[i]["w"], rot_fixed_a[i]) for i in ai], mu, sd)
        fold_info.append({"fold": k, "test_subjects": sorted(test, key=lambda s: int(s[4:])), "val_subjects": sorted(val, key=lambda s: int(s[4:])),
                          "threshold": t, **tinfo})
        log(f"fold {k}: threshold {t}, test subjects {len(test)}")

    # ── Scoring ────────────────────────────────────────────────────────────────
    yf = np.ones(len(falls)); ya = np.zeros(len(adl_events))
    mp_f = (oof["fall"] >= thr["fall"]).astype(int); mp_a = (oof["adl_ev"] >= thr["adl_ev"]).astype(int)
    rw_f = np.array([rule_on_window(f["w"]) for f in falls]); rw_a = np.array([rule_on_window(e["w"]) for e in adl_events])
    gate_f = np.array([f["gate"] for f in falls])
    # Event level, the rule with the recording's own tail (up to 20 s): alarm unless it cleared.
    # 'open' (recording ended before 20 s) counts as an alarm: the person had not moved.
    rule_f = np.array([int(f["rule"] in ("fall", "open")) for f in falls])
    rule_a = np.array([int(e["rule"] in ("fall", "open")) for e in adl_events])
    pipe_f = rule_f & mp_f; pipe_a = rule_a & mp_a
    hours = stats["adl_seconds"] / 3600.0
    waking_h = 16.0

    def fa_day(n_alarms):
        return round(n_alarms / hours * waking_h, 2)

    plain_flags = int((oof["adl_plain"] >= thr["adl_plain"]).sum())
    har_flags = int((oof["har"] >= thr["har"]).sum())
    window_level = {
        "positives": "one window per BITS-2 fall file, impact (or the maximum when no sample reaches 2.5 g) at index 100",
        "negatives": "every rule impact in BITS-2 daily-activity files (hard negatives), runtime-aligned",
        "model_alone": conf(np.concatenate([mp_f, mp_a]), np.concatenate([yf, ya])),
        "rule_on_4s_window": conf(np.concatenate([rw_f, rw_a]), np.concatenate([yf, ya])),
        "impact_only_gate": conf(np.concatenate([gate_f, np.ones(len(ya), int)]), np.concatenate([yf, ya])),
        "model_alone_randomly_rotated_test_windows": conf(
            np.concatenate([(oof["fall_rot"] >= thr["fall"]).astype(int), (oof["adl_ev_rot"] >= thr["adl_ev"]).astype(int)]),
            np.concatenate([yf, ya])),
        "plain_adl_windows_flagged_by_model": f"{plain_flags}/{len(adl_plain)}",
        "uci_har_windows_flagged_by_model": f"{har_flags}/{len(har_w)}",
        "uci_har_windows_flagged_by_rule_on_window": f"{int(sum(rule_on_window(w['w']) for w in har_w))}/{len(har_w)}",
    }
    event_level = {
        "definition": ("the watch's pipeline on each recording: rule impact gate -> model at +2 s -> rule stillness up to 20 s "
                       "(or the end of the recording, which counts as still: the rule had not cleared it)"),
        "rule_detector": {**conf(np.concatenate([rule_f, rule_a]), np.concatenate([yf, ya])),
                          "missed_falls": int((rule_f == 0).sum()), "alarms_on_adl": int(rule_a.sum()),
                          "false_alarms_per_day": fa_day(int(rule_a.sum())),
                          "alarms_per_hour_of_adl": round(int(rule_a.sum()) / hours, 2)},
        "rule_plus_model_v2": {**conf(np.concatenate([pipe_f, pipe_a]), np.concatenate([yf, ya])),
                               "missed_falls": int((pipe_f == 0).sum()), "alarms_on_adl": int(pipe_a.sum()),
                               "false_alarms_per_day": fa_day(int(pipe_a.sum())),
                               "alarms_per_hour_of_adl": round(int(pipe_a.sum()) / hours, 2),
                               "falls_the_rule_caught_but_model_vetoed": int(((rule_f == 1) & (mp_f == 0)).sum())},
        "falls_with_no_sample_at_2_5_g (missed by both, gate)": int((gate_f == 0).sum()),
        "falls_rule_cleared_after_impact (person moved within 20 s or the next trial began)": int(sum(f["rule"] == "cleared" for f in falls)),
        "adl_impacts_where_the_recording_ended_before_20_s": int(sum(e["rule"] == "open" for e in adl_events)),
        "adl_impacts_with_a_full_20_s_still_window": int(sum(e["rule"] == "fall" for e in adl_events)),
        "falls_with_a_full_20_s_still_window": int(sum(f["rule"] == "fall" for f in falls)),
        "falls_where_the_recording_ended_before_20_s": int(sum(f["rule"] == "open" for f in falls)),
        "false_alarm_assumption": (f"alarms on {round(hours, 3)} h of held-out BITS-2 daily-activity recordings, scaled to "
                                   f"{waking_h:g} waking hours a day of the SAME activity mix; the mix is far denser in "
                                   "jumps, sits and lies than an older person's day, so this is a pessimistic rate for "
                                   "that mix and says nothing about a real day"),
    }
    # Post hoc, NOT a held-out result: the same out-of-fold scores at one shared threshold,
    # to show the trade-off a reviewer would be choosing from.
    sweep = {}
    for t in [0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]:
        mf = (oof["fall"] >= t).astype(int); ma = (oof["adl_ev"] >= t).astype(int)
        sweep[str(t)] = {"falls_vetoed_that_the_rule_caught": int(((rule_f == 1) & (mf == 0)).sum()),
                         "pipeline_sensitivity": round(float((rule_f & mf).mean()), 4),
                         "pipeline_alarms_on_adl": int((rule_a & ma).sum()),
                         "false_alarms_per_day": fa_day(int((rule_a & ma).sum()))}
    event_level["post_hoc_shared_threshold_sweep_not_held_out"] = sweep
    per_activity = {}
    for a in sorted({f["activity"] for f in falls}):
        ii = [i for i, f in enumerate(falls) if f["activity"] == a]
        per_activity[f"fall{a}"] = {"n": len(ii), "rule": int(rule_f[ii].sum()), "rule_plus_model": int(pipe_f[ii].sum()),
                                    "gate": int(gate_f[ii].sum())}
    adl_by = {}
    for a in sorted({e["activity"] for e in adl_events}):
        ii = [i for i, e in enumerate(adl_events) if e["activity"] == a]
        adl_by[f"adl{a}"] = {"impacts": len(ii), "rule_alarms": int(rule_a[ii].sum()), "rule_plus_model_alarms": int(pipe_a[ii].sum())}

    # ── Final model on every subject, threshold = median of the fold thresholds ────────
    t_final = float(np.median([f["threshold"] for f in fold_info]))
    model, mu, sd, tinfo = train(tf, [f["w"] for f in falls], [e["w"] for e in adl_events], [p["w"] for p in adl_plain],
                                 [w["w"] for w in har_w], rng, args.epochs)

    # Synthetic, never trained on: a watch dropped on a table, and a watch lying still
    def still(n, noise, R):
        return (np.tile([0, 0, 1.0], (n, 1)) + rng.normal(0, noise, (n, 3))).astype(np.float32) @ R.T

    drops, not_worn = [], []
    for i in range(50):
        R = random_rotation(rng)
        a = still(1400, 0.01, np.eye(3, dtype=np.float32))
        j = 300
        a[j - 15:j] = [0, 0, 0.05]                     # 0.3 s free fall
        a[j] = [0.3, 0.2, 3.0 + (i % 10) * 0.6]        # impact 3 g to 8.4 g
        a[j + 1] = [0, 0, 1.8]; a[j + 2] = [0, 0, 0.4]
        a = a @ R.T
        drops.append(a)
        not_worn.append(still(WIN, 0.003, R))
    drop_rule = [rule_events(a) for a in drops]
    drop_rule_alarm = sum(int(any(e["outcome"] in ("fall", "open") for e in ev)) for ev in drop_rule)
    drop_w = [features(window_at(a, ev[0]["impact"])[0]) for a, ev in zip(drops, drop_rule) if ev]
    p_drop = predict(model, drop_w, mu, sd)
    p_nw = predict(model, [features(a) for a in not_worn], mu, sd)
    synthetic = {
        "how": "50 synthetic table drops (0.3 s free fall, a 3 to 8.4 g impact, then motionless at 1 g, random orientation) "
               "and 50 'not worn' windows (motionless, random orientation). Synthetic: BITS-2 has no drops. Not trained on.",
        "table_drops_rule_alarm": f"{drop_rule_alarm}/50",
        "table_drops_model_at_or_above_threshold": f"{int((p_drop >= t_final).sum())}/{len(drop_w)}",
        "table_drops_rule_plus_model_alarm": f"{int(sum(1 for p in p_drop if p >= t_final))}/50",
        "not_worn_windows_model_at_or_above_threshold": f"{int((p_nw >= t_final).sum())}/50",
        "note": "off the wrist the watch's own onWrist flag stops both detectors; these numbers are for when that flag is unknown",
    }

    # ── Export ───────────────────────────────────────────────────────────────────
    export = {}
    keras_path = os.path.join(args.out, "fall_model_v2.keras")
    model.save(keras_path)
    try:
        conv = tf.lite.TFLiteConverter.from_keras_model(model)
        conv.optimizations = [tf.lite.Optimize.DEFAULT]
        tfl = conv.convert()
        with open(os.path.join(args.out, "fall_model_v2.tflite"), "wb") as fh:
            fh.write(tfl)
        interp = tf.lite.Interpreter(model_content=tfl, num_threads=1); interp.allocate_tensors()
        inp = interp.get_input_details()[0]; outp = interp.get_output_details()[0]
        test_ws = [f["w"] for f in falls[:100]] + [e["w"] for e in adl_events[:100]]
        pk = predict(model, test_ws, mu, sd); agree = 0; lat = []
        for i, w in enumerate(test_ws):
            x = ((w - mu) / sd)[None].astype(np.float32)
            t0 = time.perf_counter(); interp.set_tensor(inp["index"], x); interp.invoke(); lat.append(time.perf_counter() - t0)
            agree += int((interp.get_tensor(outp["index"]).ravel()[0] >= t_final) == (pk[i] >= t_final))
        lat_ms = sorted(1000 * x for x in lat)
        export = {"tflite_bytes": len(tfl), "tflite_input": [int(v) for v in inp["shape"]], "tflite_output": [int(v) for v in outp["shape"]],
                  "tflite_agrees_with_keras": f"{agree}/{len(test_ws)}",
                  "latency_ms_one_thread_this_build_machine_x86": {"median": round(lat_ms[len(lat_ms) // 2], 3), "p95": round(lat_ms[int(0.95 * len(lat_ms))], 3)},
                  "latency_note": "measured on the build machine's CPU, one thread; a watch CPU is slower and was not measured"}
    except Exception as exc:  # noqa: BLE001
        export = {"tflite_error": f"{type(exc).__name__}: {str(exc)[:300]}", "portable": "fall_model_v2.keras"}
    export["keras_bytes"] = os.path.getsize(keras_path); export["params"] = int(model.count_params())

    spec = {"threshold": round(t_final, 4), "mean": [float(x) for x in mu], "std": [float(x) for x in sd],
            "windowSamples": WIN, "rateHz": RATE_HZ,
            "trained_on": "BITS-2 (Zenodo 10013090, CC BY 4.0, left-wrist) and UCI HAR (CC BY 4.0), subject-independent folds; see docs/ai/fall/MODEL_CARD_V2.md",
            "cv_event_sensitivity_rule_plus_model": event_level["rule_plus_model_v2"]["sensitivity"],
            "cv_event_specificity_rule_plus_model": event_level["rule_plus_model_v2"]["specificity"]}
    with open(os.path.join(args.out, "fall_model_v2.json"), "w") as fh:
        json.dump(spec, fh, indent=1)

    results = {
        "data": {"bits2": {"record": "https://zenodo.org/records/10013090", "licence": "CC BY 4.0", "sha256": BITS2_SHA256,
                           "subjects_used": len(subjects), "excluded_subjects": sorted(EXCLUDED_SUBJECTS), **stats,
                           "channel": "acg (acceleration including gravity), m/s^2 / 9.80665 -> g; sample order as time at 50 Hz"},
                 "uci_har": {"record": "https://archive.ics.uci.edu/dataset/240", "licence": "CC BY 4.0", "sha256": HAR_SHA256,
                             "windows": len(har_w), "use": "negatives only, total_acc in g, 50 Hz, stitched from 50 % overlapping windows"}},
        "protocol": {"folds": args.folds, "split": "by BITS-2 subject (and by UCI HAR subject); threshold chosen per fold on validation subjects inside the training subjects",
                     "folds_detail": fold_info, "epochs": args.epochs, "seed": SEED,
                     "augmentation": "random 3-D rotation of every training window; impact shifted by up to +/-10 samples; 12 copies per fall, 4 per ADL impact"},
        "window_level": window_level,
        "event_level": event_level,
        "falls_by_activity": per_activity,
        "adl_impacts_by_activity": adl_by,
        "synthetic_cases": synthetic,
        "final_model": {"threshold": t_final, **export, **tinfo},
        "seconds": round(time.time() - T0, 1),
    }
    with open(os.path.join(args.out, "results_v2.json"), "w") as fh:
        json.dump(results, fh, indent=2)
    log("done: " + json.dumps({"window": window_level["model_alone"], "event_rule": event_level["rule_detector"],
                               "event_pipe": event_level["rule_plus_model_v2"]}))


if __name__ == "__main__":
    main()
