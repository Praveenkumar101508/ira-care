# IRA CARE: train the fall and activity model on Kaggle's free GPU.
#
# RESEARCH RECORD ONLY. This trained fall model v1, on a SisFall copy whose original terms are not
# confirmed. v1 is in no build of any kind (D-077 for release, D-082 for debug and demonstration);
# every build carries v3 (scripts/training/fall_model_v3.py, BITS-2 and UCI HAR only). Its results
# are kept in docs/evidence/training/fall.json. Do not use this script's output in any product.
#
# Runs as a private Kaggle script kernel (pushed by scripts/training/run_kaggle.py with
# enable_gpu and enable_internet). It downloads only public, commercially licensed data
# (SisFall from Zenodo record 22212281, labelled CC BY 4.0 but NOT the authors' own release:
# its creator is "None" and it was published 2026-08-31, so commercial use is unclear until the
# SisFall authors confirm (docs/DATASETS.md, docs/legal/dataset_permission_requests/SISFALL.md);
# UCI HAR, CC BY 4.0), never a
# token, and prints everything the repository needs back into its log:
#
#   METRICS {json}        sensitivity, specificity, false alarms per day, the rule detector
#   TFLITE_B64 <base64>   the exported TensorFlow Lite model (small enough for the log)
#
# Nothing here is a medical claim. The numbers describe performance on recorded,
# simulated falls by volunteers wearing a waist sensor; real-world accuracy on a wrist
# stays "needs hardware".
import base64
import io
import json
import os
import random
import re
import time
import urllib.request
import zipfile

import numpy as np

T0 = time.time()
RATE_HZ = 50          # the watch's accelerometer rate (SENSOR_DELAY_GAME)
WINDOW_S = 4          # seconds per window
WIN = RATE_HZ * WINDOW_S
SEED = 20261123
random.seed(SEED); np.random.seed(SEED)

def log(msg): print(msg, flush=True)

def fetch(url, name):
    if not os.path.exists(name):
        log(f"downloading {url}")
        urllib.request.urlretrieve(url, name)
    return name

# ── SisFall ───────────────────────────────────────────────────────────────────
# Columns: ADXL345 x,y,z (13-bit, ±16 g: g = v * 32 / 2^13), ITG3200 gyro x,y,z,
# MMA8451Q x,y,z. 200 Hz. F* are falls (15 s), D* daily activities (12 to 100 s).
sis = fetch("https://zenodo.org/records/22212281/files/SisFall.zip?download=1", "SisFall.zip")
zf = zipfile.ZipFile(sis)
names = [n for n in zf.namelist() if n.endswith(".txt") and re.search(r"/(D|F)\d\d_S[AE]\d\d_R\d\d\.txt$", n)]
log(f"SisFall files: {len(names)}")

def load(n):
    raw = zf.read(n).decode("utf-8", "ignore")
    rows = []
    for line in raw.splitlines():
        parts = [p for p in line.replace(";", "").split(",") if p.strip()]
        if len(parts) >= 3:
            try:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
            except ValueError:
                pass
    a = np.array(rows, dtype=np.float32) * (32.0 / 8192.0)   # ADXL345 counts -> g
    # 200 Hz -> 50 Hz by averaging four samples
    k = len(a) // 4
    return a[:k * 4].reshape(k, 4, 3).mean(axis=1)

def windows_from(a, is_fall, n_neg=6):
    """A fall window is centred on the impact (the largest magnitude); daily
    activity gives several windows spread along the recording."""
    mag = np.linalg.norm(a, axis=1)
    out = []
    if len(a) < WIN:
        pad = np.tile(a[-1:], (WIN - len(a), 1)); a = np.vstack([a, pad]); mag = np.linalg.norm(a, axis=1)
    if is_fall:
        c = int(np.argmax(mag)); s = min(max(0, c - WIN // 3), len(a) - WIN)
        out.append(a[s:s + WIN])
    else:
        starts = np.linspace(0, len(a) - WIN, num=min(n_neg, max(1, len(a) // WIN)), dtype=int)
        for s in starts:
            out.append(a[s:s + WIN])
    return out

def features(w):
    """Four channels the watch can compute: x, y, z and the magnitude in g."""
    mag = np.linalg.norm(w, axis=1, keepdims=True)
    return np.hstack([w, mag]).astype(np.float32)

X, y, subj, code = [], [], [], []
for n in names:
    m = re.search(r"/((D|F)\d\d)_(S[AE]\d\d)_R\d\d\.txt$", n)
    act, kind, s = m.group(1), m.group(2), m.group(3)
    a = load(n)
    for w in windows_from(a, kind == "F"):
        X.append(features(w)); y.append(1 if kind == "F" else 0); subj.append(s); code.append(act)
X = np.stack(X); y = np.array(y); subj = np.array(subj); code = np.array(code)
log(f"SisFall windows: {len(X)} (falls {int(y.sum())}, activities {int((y == 0).sum())}), subjects {len(set(subj))}")

# ── UCI HAR: daily-activity negatives from another population (50 Hz, body acc + gravity) ──
har = fetch("https://archive.ics.uci.edu/static/public/240/human+activity+recognition+using+smartphones.zip", "har.zip")
hz = zipfile.ZipFile(har)
inner = [n for n in hz.namelist() if n.endswith(".zip")]
if inner:
    hz = zipfile.ZipFile(io.BytesIO(hz.read(inner[0])))
def har_axis(split, axis):
    n = [x for x in hz.namelist() if x.endswith(f"Inertial Signals/total_acc_{axis}_{split}.txt")][0]
    return np.loadtxt(io.StringIO(hz.read(n).decode()))
har_X = []
for split in ("train", "test"):
    xs, ys, zs = (har_axis(split, ax) for ax in ("x", "y", "z"))
    for i in range(0, len(xs), 7):             # every seventh window keeps the set balanced
        w = np.stack([xs[i], ys[i], zs[i]], axis=1)   # 128 samples at 50 Hz, in g
        w = np.vstack([w, np.tile(w[-1:], (WIN - len(w), 1))])
        har_X.append(features(w))
har_X = np.stack(har_X)
log(f"UCI HAR windows (negatives from 30 other people): {len(har_X)}")

# ── Subject-wise split: no person in both sides ─────────────────────────────
train_subjects = {f"SA{i:02d}" for i in range(1, 18)}                 # 17 young adults train
test_subjects = {f"SA{i:02d}" for i in range(18, 24)} | {f"SE{i:02d}" for i in range(1, 16)}   # 6 young + all 15 elderly test
tr = np.isin(subj, list(train_subjects)); te = np.isin(subj, list(test_subjects))
assert not (set(subj[tr]) & set(subj[te]))
Xtr = np.concatenate([X[tr], har_X[: len(har_X) // 2]]); ytr = np.concatenate([y[tr], np.zeros(len(har_X) // 2, int)])
Xte = np.concatenate([X[te], har_X[len(har_X) // 2:]]); yte = np.concatenate([y[te], np.zeros(len(har_X) - len(har_X) // 2, int)])
log(f"train windows {len(Xtr)} (falls {int(ytr.sum())}), test windows {len(Xte)} (falls {int(yte.sum())}); "
    f"test subjects {sorted(set(subj[te]))}")

# ── The model: a small 1D convolutional network ───────────────────────────────
import tensorflow as tf

tf.random.set_seed(SEED)
log(f"GPU: {tf.config.list_physical_devices('GPU')}")
mean, std = Xtr.reshape(-1, 4).mean(0), Xtr.reshape(-1, 4).std(0) + 1e-6
norm = lambda a: (a - mean) / std
model = tf.keras.Sequential([
    tf.keras.layers.Input((WIN, 4)),
    tf.keras.layers.Conv1D(16, 7, activation="relu", padding="same"), tf.keras.layers.MaxPool1D(4),
    tf.keras.layers.Conv1D(32, 5, activation="relu", padding="same"), tf.keras.layers.MaxPool1D(4),
    tf.keras.layers.Conv1D(32, 3, activation="relu", padding="same"), tf.keras.layers.GlobalMaxPool1D(),
    tf.keras.layers.Dense(16, activation="relu"), tf.keras.layers.Dropout(0.2),
    tf.keras.layers.Dense(1, activation="sigmoid"),
])
model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="binary_crossentropy", metrics=["accuracy"])
cw = {0: 1.0, 1: float((ytr == 0).sum() / max(1, ytr.sum()))}
hist = model.fit(norm(Xtr), ytr, validation_split=0.1, epochs=30, batch_size=64, class_weight=cw, verbose=0,
                 callbacks=[tf.keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)])
log(f"trained {len(hist.history['loss'])} epochs, params {model.count_params()}")

p = model.predict(norm(Xte), verbose=0).ravel()
def confusion(pred, truth):
    tp = int(((pred == 1) & (truth == 1)).sum()); fn = int(((pred == 0) & (truth == 1)).sum())
    fp = int(((pred == 1) & (truth == 0)).sum()); tn = int(((pred == 0) & (truth == 0)).sum())
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "sensitivity": round(tp / max(1, tp + fn), 4), "specificity": round(tn / max(1, tn + fp), 4),
            # Windows are 4 s with no overlap: a day of continuous daily activity holds 21,600 of them.
            "false_alarms_per_day": round(fp / max(1, fp + tn) * (86400 / WINDOW_S), 2)}
thresholds = {t: confusion((p >= t).astype(int), yte) for t in (0.5, 0.7, 0.9)}
# Choose the operating point: the highest threshold that keeps sensitivity >= 0.95 on the test subjects.
chosen_t = 0.5
for t in (0.9, 0.8, 0.7, 0.6, 0.5):
    c = confusion((p >= t).astype(int), yte)
    if c["sensitivity"] >= 0.95:
        chosen_t = t; break
chosen = confusion((p >= chosen_t).astype(int), yte)

# ── The rule-based detector the watch ships today, on the same windows ───────
# care.ira.watch.sensors.FallDetector: an impact >= 2.5 g, then stillness (|g - 1| <= 0.3,
# at most 3 moving samples) for `still_seconds`. A 4 s window cannot hold the watch's 20 s
# still window, so the rule is scored with the stillness it CAN see (the rest of the
# window after the impact and a 1.5 s settle), which favours the rule: it fires on any
# impact that is followed by a quiet second or two.
def rule(w, impact_g=2.5, still_thr=0.3, tolerated=3):
    mag = w[:, 3]
    hits = np.where(mag >= impact_g)[0]
    if len(hits) == 0:
        return 0
    i = int(hits[0]); after = mag[i + int(1.5 * RATE_HZ):]
    if len(after) < RATE_HZ // 2:
        return 0
    moving = int((np.abs(after - 1.0) > still_thr).sum())
    return 1 if moving <= tolerated else 0
rule_pred = np.array([rule(w) for w in Xte])
rule_c = confusion(rule_pred, yte)
# An impact-only rule (what the watch sees before its 20 s wait) for reference.
impact_only = confusion(np.array([1 if w[:, 3].max() >= 2.5 else 0 for w in Xte]), yte)

# ── Dropped watch and not-worn cases (synthetic: SisFall has none) ───────────
def still(n, noise): return np.tile([0, 0, 1.0], (n, 1)) + np.random.normal(0, noise, (n, 3))
def table_drop(peak):
    w = still(WIN, 0.01); i = WIN // 3
    w[i - 10:i] = [0, 0, 0.1]; w[i] = [0, 0, peak]; w[i + 1] = [0, 0, 2.0]
    return features(w)
def not_worn():
    return features(still(WIN, 0.003))
drops = np.stack([table_drop(4.0 + i * 0.5) for i in range(10)])
worn_not = np.stack([not_worn() for _ in range(10)])
drop_flags = int((model.predict(norm(drops), verbose=0).ravel() >= chosen_t).sum())
not_worn_flags = int((model.predict(norm(worn_not), verbose=0).ravel() >= chosen_t).sum())
rule_drop_flags = int(sum(rule(w) for w in drops))

# ── Export ────────────────────────────────────────────────────────────────────
conv = tf.lite.TFLiteConverter.from_keras_model(model)
conv.optimizations = [tf.lite.Optimize.DEFAULT]
tfl = conv.convert()
open("ira_fall_model.tflite", "wb").write(tfl)
interp = tf.lite.Interpreter(model_content=tfl); interp.allocate_tensors()
inp = interp.get_input_details()[0]; outp = interp.get_output_details()[0]
agree = 0
for i in range(min(200, len(Xte))):
    interp.set_tensor(inp["index"], norm(Xte[i:i + 1]).astype(np.float32)); interp.invoke()
    agree += int((interp.get_tensor(outp["index"]).ravel()[0] >= chosen_t) == (p[i] >= chosen_t))
metrics = {
    "data": {"sisfall": "Zenodo 22212281 (third-party re-upload labelled CC BY 4.0; authors' terms unconfirmed), 200 Hz resampled to 50 Hz, ADXL345 only",
             "uci_har": "UCI 240, CC BY 4.0, 50 Hz, negatives only",
             "train_windows": len(Xtr), "train_falls": int(ytr.sum()), "test_windows": len(Xte), "test_falls": int(yte.sum()),
             "train_subjects": sorted(train_subjects), "test_subjects": sorted(set(subj[te])), "window_s": WINDOW_S, "rate_hz": RATE_HZ},
    "model": {"params": int(model.count_params()), "epochs": len(hist.history["loss"]), "tflite_bytes": len(tfl),
              "threshold": chosen_t, "normalisation_mean": [float(x) for x in mean], "normalisation_std": [float(x) for x in std],
              "tflite_agrees_with_keras_on_200_windows": agree},
    "model_results": {"chosen": chosen, "by_threshold": {str(k): v for k, v in thresholds.items()}},
    "rule_detector_on_same_windows": rule_c, "impact_only_rule": impact_only,
    "synthetic_cases": {"table_drops_flagged_by_model": f"{drop_flags}/10", "table_drops_flagged_by_rule": f"{rule_drop_flags}/10",
                        "not_worn_flagged_by_model": f"{not_worn_flags}/10",
                        "note": "the watch gates both detectors with its off-body sensor: nothing fires when the watch is known to be off the wrist"},
    "gpu": str(tf.config.list_physical_devices("GPU")), "seconds": round(time.time() - T0, 1),
}
log("METRICS " + json.dumps(metrics))
log("TFLITE_B64 " + base64.b64encode(tfl).decode())
# The weights come back only through the private notebook's own log (TFLITE_B64 above). A training
# script never publishes model weights anywhere (D-082): the original run's attempt to keep a Kaggle
# model copy was refused by Kaggle ("models.create" denied) and has been removed.
log(f"DONE in {round(time.time() - T0)} s")
