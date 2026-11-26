# IRA CARE: cross-dataset evaluation of the SHIPPED fall pipeline on WEDA-FALL (requirement F4.f).
#
# What is evaluated, unchanged: fall model v3 (watch/app/src/main/assets/fall_model.tflite and
# fall_model.json, trained only on BITS-2 and UCI HAR) in "recover" mode beside the rule detector
# (D-080). The watch's Kotlin detectors (FallDetector, SampleWindow, RecoveringFallDetector) are
# ported below line for line; the model is the shipped .tflite file itself, run by the TensorFlow
# Lite interpreter, with the shipped spec's mean, std, threshold (0.95) and candidate (2.0 g).
# Nothing is retrained and no threshold is changed: WEDA-FALL is a held-out test, never a tuning set.
#
# WEDA-FALL (Marques and Moreno, Sensors 23(3):1146, 2023; github.com/joaojtmarques/WEDA-FALL,
# ODC-By 1.0): a Fitbit Sense on the wrist; 14 young volunteers (8 fall types onto a mattress,
# 11 daily activities) and 11 older adults aged 77 to 95 (daily activities only).
#
# Every transform, fixed before any result was looked at:
#   1. Units: the files hold m/s^2 (a still wrist reads about 9.8). Divided by 9.80665, as the
#      watch divides by SensorManager.GRAVITY_EARTH. A file whose median magnitude is not between
#      UNIT_BAND_G g after that (a file already in g reads about 0.1, one in mg about 100) is
#      refused, not silently rescaled. (The first run used 0.8 to 1.2 g and refused 7 genuine
#      m/s^2 recordings of jogging and clapping, median 1.21 to 1.52 g; the band was widened to
#      0.5 to 2.0 g, which still refuses a unit error by a factor of 9.8. It decides which files
#      are read, nothing in the pipeline.)
#   2. Axes: kept as the Fitbit gives them (device frame, gravity read as +9.8 on the axis that
#      points up, the same convention as Android's TYPE_ACCELEROMETER). The rules use only the
#      magnitude; the model was trained on randomly rotated windows (fall_model_v2.py, augment).
#   3. Time: the files' timestamps are the times batches of samples reached the phone (about five
#      samples share each 0.1 s step), not sample times. Rows are put in timestamp order (24 files
#      hold a block of about 15 to 19 rows out of order); a backward step longer than
#      MAX_BACKWARD_S refuses the file. The native rate is the one of 40 or 50 Hz within 5 % of
#      (samples - 1) / (last - first time); anything else refuses the file (samples lost). Sample
#      i is then at i / native rate.
#   4. Rate: linear interpolation onto the watch's 50 Hz grid. Most fall files are 40 Hz
#      (the "50Hz" folder holds the same files as the "40Hz" one); daily-activity files are 50 Hz
#      and pass through unchanged.
#   5. Edges: two seconds of the first sample before each recording and three seconds of the
#      last after it, so a candidate near either end has a full 4 s window (the watch would hold
#      real samples there). Decisions whose window touches the padding are counted. A rule event
#      raised inside the trailing padding is ignored; the rule's state is read at the real end.
#
# Scoring, fixed before any result was looked at:
#   - A fall recording is caught when a suspected fall is raised whose impact (rule) or peak
#     (model) lies within the annotated fall (dataset/fall_timestamps.csv) widened by
#     FALL_TOLERANCE_S on each side (the annotations were made by hand and "can have mistakes").
#   - Two conventions for the rule, as in docs/ai/fall/MODEL_CARD_V3.md: (a) only a complete 20 s
#     still window counts; (b) also an impact still waiting for its 20 s when the recording ended
#     (the person had not moved). WEDA-FALL recordings last about 3 to 40 s, so (a) almost never
#     completes and (b) is the rule's fair score.
#   - Alarms: suspected falls that start the 30 s countdown (a second one inside a running
#     countdown is ignored, as FallCountdown does). False alarms are counted per hour of the
#     daily-activity recordings and per recording, young and older adults separately.
#
#   python3 -I scripts/eval/fall_cross_weda.py --data <scratch>/weda/repo/dataset \
#       --commit <hash> --out docs/ai/fall/cross_dataset_weda.json
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "watch" / "app" / "src" / "main" / "assets"
G = 9.80665
OUT_HZ = 50
NATIVE_RATES = (40, 50)
RATE_TOLERANCE = 0.05
UNIT_BAND_G = (0.5, 2.0)
MAX_BACKWARD_S = 3.0
PRE_PAD_S, POST_PAD_S = 2.0, 3.0
FALL_TOLERANCE_S = 1.0
COUNTDOWN_MS = 30_000
OLDER_FROM_USER = 21          # README: users 1-14 young, 21-31 older adults
HEADER = ["accel_time_list", "accel_x_list", "accel_y_list", "accel_z_list"]
ADL_NAMES = {"D01": "walking", "D02": "jogging", "D03": "walking up and down stairs",
             "D04": "sit on a chair, wait, get up", "D05": "sit, try to get up, collapse into the chair",
             "D06": "crouch, tie shoes, get up", "D07": "stumble while walking",
             "D08": "gentle jump (reach a high object)", "D09": "hit a table with the hand",
             "D10": "clapping hands", "D11": "open and close a door"}
FALL_NAMES = {"F01": "forward, walking, slip", "F02": "lateral, walking, slip", "F03": "backward, walking, slip",
              "F04": "forward, walking, trip", "F05": "backward, trying to sit down",
              "F06": "forward, sitting, faint", "F07": "backward, sitting, faint", "F08": "lateral, sitting, faint"}


NOTICE = ("Contains information from WEDA-FALL (https://github.com/joaojtmarques/WEDA-FALL), which is made available "
          "under the ODC Attribution License (https://opendatacommons.org/licenses/by/1-0/). Cite: J. Marques and "
          "P. Moreno, Online Fall Detection Using Wrist Devices, Sensors 23(3):1146, 2023. Results only: no WEDA-FALL "
          "data is copied into this repository.")


class RecordingError(ValueError):
    """A recording that breaks the evaluation's input rules. ``code`` names the rule."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── input: units, timestamps, rate ───────────────────────────────────────────

def read_accel_csv(path) -> tuple[np.ndarray, np.ndarray]:
    """(delivery times in s, samples in m/s^2) from one WEDA-FALL ``*_accel.csv``."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows or [c.strip() for c in rows[0]] != HEADER:
        raise RecordingError("bad_header", f"{path}: expected the header {HEADER}")
    a = np.array([[float(v) for v in r] for r in rows[1:] if r], dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 4 or len(a) < 10:
        raise RecordingError("too_short", f"{path}: fewer than 10 samples")
    return a[:, 0], a[:, 1:]


def check_units_ms2(xyz: np.ndarray) -> float:
    """The median magnitude, in g, of samples given IN m/s^2, which must lie in UNIT_BAND_G;
    refuses anything else (a file already in g reads about 0.1 here, one in mg about 100)."""
    if not np.all(np.isfinite(xyz)):
        raise RecordingError("not_finite", "samples must be finite")
    med = float(np.median(np.linalg.norm(xyz, axis=1))) / G
    if not UNIT_BAND_G[0] <= med <= UNIT_BAND_G[1]:
        raise RecordingError("unit_mismatch", f"median magnitude {med:.3f} g if read as m/s^2: wrong unit")
    return med


def order_by_time(t: np.ndarray, xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """Rows in delivery-time order. Returns (t, xyz, rows that moved). A backward step longer than
    MAX_BACKWARD_S, or a repeated timestamp run that cannot be ordered, refuses the file."""
    d = np.diff(t)
    if len(d) and float(-d.min()) > MAX_BACKWARD_S:
        raise RecordingError("misaligned_timestamps", f"timestamps go back by {-d.min():.3f} s")
    idx = np.argsort(t, kind="stable")
    moved = int(np.count_nonzero(idx != np.arange(len(t))))
    return t[idx], xyz[idx], moved


def native_rate(t: np.ndarray) -> int:
    """40 or 50 Hz, whichever is within RATE_TOLERANCE of (n - 1) / span; otherwise refused."""
    span = float(t[-1] - t[0])
    if span <= 0:
        raise RecordingError("misaligned_timestamps", "timestamps do not advance")
    eff = (len(t) - 1) / span
    for hz in NATIVE_RATES:
        if abs(eff / hz - 1.0) <= RATE_TOLERANCE:
            return hz
    raise RecordingError("rate_unknown", f"{eff:.2f} samples per second is neither 40 nor 50 Hz: samples lost")


def resample_to_watch(xyz_ms2: np.ndarray, native_hz: int, out_hz: int = OUT_HZ) -> np.ndarray:
    """Samples at i / native_hz, linearly interpolated onto k / out_hz and divided by g."""
    n = len(xyz_ms2)
    src = np.arange(n) / native_hz
    m = int(math.floor((n - 1) / native_hz * out_hz + 1e-9)) + 1
    dst = np.arange(m) / out_hz
    out = np.stack([np.interp(dst, src, xyz_ms2[:, j]) for j in range(3)], axis=1)
    return out / G


def load_recording(path) -> dict:
    """One file through steps 1 to 4 above. Returns the 50 Hz signal in g and what was done."""
    t, xyz = read_accel_csv(path)
    median_g = check_units_ms2(xyz)
    t, xyz, moved = order_by_time(t, xyz)
    hz = native_rate(t)
    acc = resample_to_watch(xyz, hz)
    return {"acc_g": acc, "native_hz": hz, "rows_reordered": moved, "median_g": round(median_g, 4),
            "t_delivery": t, "seconds": len(acc) / OUT_HZ, "clipped_axis_samples": int(np.count_nonzero(np.abs(xyz) >= 39.2))}


def delivery_to_signal_time(t_delivery: np.ndarray, seconds: float, native_hz: int) -> float:
    """An annotation in the file's (delivery) seconds, as seconds on the resampled signal."""
    i = int(np.clip(np.searchsorted(t_delivery, seconds), 0, len(t_delivery) - 1))
    return i / native_hz


# ── the watch's detectors, ported line for line ──────────────────────────────

class FallSettings:
    def __init__(self, impact_g=2.5, still_seconds=20, still_threshold_g=0.3, moving_samples_tolerated=3):
        self.impact_g, self.still_seconds = impact_g, still_seconds
        self.still_threshold_g, self.moving_samples_tolerated = still_threshold_g, moving_samples_tolerated


class FallDetector:
    """care.ira.watch.sensors.FallDetector. feed() returns None or (kind, at_ms, peak_g, still_s)."""
    SETTLE_MS = 1_500

    def __init__(self, settings: FallSettings | None = None):
        self.s = settings or FallSettings()
        self.reset()

    def reset(self):
        self.impact_at, self.peak, self.moving = -1, 0.0, 0

    def feed(self, at_ms: int, g: float):
        if self.impact_at < 0:
            if g >= self.s.impact_g:
                self.impact_at, self.peak, self.moving = at_ms, g, 0
                return ("impact", at_ms, g, 0)
            return None
        since = at_ms - self.impact_at
        if since <= self.SETTLE_MS:
            if g > self.peak:
                self.peak = g
            return None
        if abs(g - 1.0) > self.s.still_threshold_g:
            self.moving += 1
            if self.moving > self.s.moving_samples_tolerated:
                self.reset()
                return ("cleared", at_ms, 0.0, 0)
        if since >= self.s.still_seconds * 1_000:
            out = ("fall", self.impact_at, self.peak, self.s.still_seconds)
            self.reset()
            return out
        return None


class SampleWindow:
    """care.ira.watch.sensors.SampleWindow: one row per 1000/rate ms slot, the last 200 kept."""

    def __init__(self, samples=200, rate_hz=50):
        self.slot_ms, self.n = 1000 // rate_hz, samples
        self.rows: list = []
        self.last_slot = None

    def add(self, at_ms, x, y, z, g):
        slot = at_ms // self.slot_ms
        row = np.array([x, y, z, g], dtype=np.float32)
        if slot == self.last_slot and self.rows:
            self.rows[-1] = row
        else:
            self.rows.append(row); self.last_slot = slot
        if len(self.rows) > self.n:
            del self.rows[0]

    @property
    def is_full(self):
        return len(self.rows) >= self.n

    def snapshot(self):
        return np.stack(self.rows)


class RecoveringFallDetector:
    """care.ira.watch.sensors.RecoveringFallDetector. ``infer`` takes the normalised (200, 4)
    float32 window and returns P(fall) or None (a failing model). feed() returns None or
    ("fall", at_ms, peak_g, still_s, path) with path "rule" or "model"."""
    SETTLE_MS = 1_500
    AFTER_PEAK_MS = 2_000

    def __init__(self, settings: FallSettings, spec: dict, infer, on_decision=None):
        self.rules = FallDetector(settings)
        self.window = SampleWindow(spec["windowSamples"], spec["rateHz"])
        self.mean = np.asarray(spec["mean"], np.float32)
        self.std = np.asarray(spec["std"], np.float32)
        self.threshold = np.float32(spec["threshold"])
        self.candidate_g = float(np.float32(spec["candidateG"]))
        self.infer, self.on_decision = infer, on_decision
        self.candidate_at, self.peak_at, self.peak_g = -1, -1, 0.0
        self.last_probability = None

    def feed(self, at_ms: int, x: float, y: float, z: float):
        g = math.sqrt(x * x + y * y + z * z)
        self.window.add(at_ms, x, y, z, g)
        rule = self.rules.feed(at_ms, g)
        if rule is not None and rule[0] == "fall":
            self.candidate_at = -1
            return (*rule, "rule")
        if self.candidate_at < 0:
            if g >= self.candidate_g:
                self.candidate_at, self.peak_at, self.peak_g = at_ms, at_ms, g
            return None
        if at_ms - self.candidate_at <= self.SETTLE_MS:
            if g > self.peak_g:
                self.peak_g, self.peak_at = g, at_ms
            return None
        if at_ms - self.peak_at >= self.AFTER_PEAK_MS and self.window.is_full:
            self.candidate_at = -1
            try:
                p = self.infer(((self.window.snapshot() - self.mean) / self.std).astype(np.float32))
            except Exception:  # noqa: BLE001  the watch: runCatching { ... }.getOrNull()
                p = None
            self.last_probability = p
            if self.on_decision:
                self.on_decision(self.peak_at, self.peak_g, p, at_ms)
            if p is not None and np.float32(p) >= self.threshold:
                self.rules.reset()
                return ("fall", self.peak_at, self.peak_g, 0, "model")
        return None


# ── one recording through one configuration ─────────────────────────────────

def stream(acc_g: np.ndarray, *, spec: dict, infer, config: str) -> dict:
    """Run ``config`` ("rule", "model" or "shipped") over a 50 Hz recording in g, with the edge
    padding of step 5. Times in the result are seconds from the first real sample."""
    pre, post = int(PRE_PAD_S * OUT_HZ), int(POST_PAD_S * OUT_HZ)
    sig = np.vstack([np.repeat(acc_g[:1], pre, 0), acc_g, np.repeat(acc_g[-1:], post, 0)])
    real_end = pre + len(acc_g)
    offset_ms = pre * 1000 // OUT_HZ
    decisions, raised, ignored = [], [], []
    if config == "rule":
        det = FallDetector()
    else:
        settings = FallSettings(impact_g=math.inf) if config == "model" else FallSettings()
        det = RecoveringFallDetector(settings, spec, infer, on_decision=lambda pk, pg, p, at: decisions.append(
            {"peak_s": (pk - offset_ms) / 1000, "peak_g": round(pg, 3), "p": None if p is None else round(float(p), 4),
             "padded": not (pre * 20 <= at - 199 * 20 and at < real_end * 20)}))
    rules = det if config == "rule" else det.rules
    open_impact = None
    countdown_until = -1
    for i, (x, y, z) in enumerate(sig):
        at = i * 1000 // OUT_HZ
        if i == real_end:
            open_impact = None if rules.impact_at < 0 else (rules.impact_at - offset_ms) / 1000
        if config == "rule":
            ev = det.feed(at, math.sqrt(x * x + y * y + z * z))
            ev = (*ev, "rule") if ev is not None and ev[0] == "fall" else None
        else:
            ev = det.feed(at, float(x), float(y), float(z))
        if ev is None:
            continue
        if ev[4] == "rule" and i >= real_end:
            continue                                      # a rule event made of padding
        if at < countdown_until:                          # FallCountdown ignores it while counting
            ignored.append({"path": ev[4], "at_s": (ev[1] - offset_ms) / 1000, "raised_s": (at - offset_ms) / 1000})
            continue
        countdown_until = at + COUNTDOWN_MS
        raised.append({"path": ev[4], "at_s": (ev[1] - offset_ms) / 1000, "peak_g": round(ev[2], 3),
                       "raised_s": (at - offset_ms) / 1000, "padded": i >= real_end})
    if config == "model":
        open_impact = None
    return {"raised": raised, "ignored_during_countdown": ignored, "open_rule_impact_s": open_impact, "decisions": decisions}


def alarms(res: dict, convention: str) -> list[float]:
    """Alarm times (impact or peak, s) under convention "a" (raised only) or "b" (also an open
    rule impact at the end, unless a raised alarm's countdown was still running then)."""
    out = [r["at_s"] for r in res["raised"]]
    if convention == "b" and res["open_rule_impact_s"] is not None:
        if not any(r["raised_s"] <= res["open_rule_impact_s"] < r["raised_s"] + COUNTDOWN_MS / 1000 for r in res["raised"]):
            out.append(res["open_rule_impact_s"])
    return out


def caught(times: list[float], start_s: float, end_s: float, tol: float = FALL_TOLERANCE_S) -> bool:
    return any(start_s - tol <= t <= end_s + tol for t in times)


def countdown_running_at(res: dict, at_s: float) -> bool:
    """Whether an alarm raised earlier had its 30 s countdown running at ``at_s``."""
    return any(r["raised_s"] <= at_s < r["raised_s"] + COUNTDOWN_MS / 1000 for r in res["raised"])


# ── the shipped model ────────────────────────────────────────────────────────

def load_shipped():
    """The shipped spec and a TFLite interpreter on the shipped file; refuses a file whose
    SHA-256 is not the one the spec (and FallModelReleaseTest) pins."""
    spec = json.loads((ASSETS / "fall_model.json").read_text())
    blob = (ASSETS / "fall_model.tflite").read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    if sha != spec["tfliteSha256"] or spec["mode"] != "recover" or spec["provenance"] != "ira-fall-v3-licensed":
        raise SystemExit("the model on disk is not the shipped v3 file")
    import tensorflow as tf
    interp = tf.lite.Interpreter(model_content=blob, num_threads=1)
    interp.allocate_tensors()
    inp, outp = interp.get_input_details()[0], interp.get_output_details()[0]

    def infer(w):
        interp.set_tensor(inp["index"], w[None].astype(np.float32))
        interp.invoke()
        return np.float32(interp.get_tensor(outp["index"]).ravel()[0])
    return spec, infer, {"tflite_bytes": len(blob), "tflite_sha256": sha, "tensorflow": tf.__version__}


def latency(infer, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    ws = rng.standard_normal((n, 200, 4)).astype(np.float32)
    for w in ws[:50]:
        infer(w)
    ms = []
    for w in ws:
        t0 = time.perf_counter(); infer(w); ms.append(1000 * (time.perf_counter() - t0))
    ms = np.array(ms)
    return {"windows": n, "threads": 1, "median_ms": round(float(np.median(ms)), 4),
            "p95_ms": round(float(np.percentile(ms, 95)), 4), "max_ms": round(float(ms.max()), 4)}


# ── the evaluation ───────────────────────────────────────────────────────────

FILE_RE = re.compile(r"^(?P<act>[DF]\d\d)/U(?P<user>\d\d)_R(?P<trial>\d\d)_accel\.csv$")


def files(data: Path):
    base = data / "50Hz"
    for p in sorted(base.glob("*/U??_R??_accel.csv")):
        rel = p.relative_to(base).as_posix()
        m = FILE_RE.match(rel)
        if m:
            yield p, m.group("act"), int(m.group("user")), int(m.group("trial"))


def rate(k, n):
    return {"n": n, "k": k, "share": round(k / n, 4) if n else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True, help="WEDA-FALL's dataset/ directory (outside the repository)")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    t_start = time.time()
    spec, infer, model_info = load_shipped()
    with open(args.data / "fall_timestamps.csv", newline="", encoding="utf-8-sig") as fh:
        ann = {r["filename"]: (float(r["start_time"]), float(r["end_time"])) for r in csv.DictReader(fh)}

    configs = ("rule", "model", "shipped")
    falls, adls, refused = [], [], {}
    prep = {"native_hz": {}, "rows_reordered_files": 0, "clipped_axis_samples": 0, "files": 0}
    for path, act, user, trial in files(args.data):
        try:
            rec = load_recording(path)
        except RecordingError as e:
            refused[e.code] = refused.get(e.code, 0) + 1
            continue
        prep["files"] += 1
        prep["native_hz"][str(rec["native_hz"])] = prep["native_hz"].get(str(rec["native_hz"]), 0) + 1
        prep["rows_reordered_files"] += int(rec["rows_reordered"] > 0)
        prep["clipped_axis_samples"] += rec["clipped_axis_samples"]
        res = {c: stream(rec["acc_g"], spec=spec, infer=infer, config=c) for c in configs}
        item = {"act": act, "user": user, "trial": trial, "group": "older" if user >= OLDER_FROM_USER else "young",
                "seconds": rec["seconds"], "native_hz": rec["native_hz"], "res": res,
                "max_g": float(np.max(np.linalg.norm(rec["acc_g"], axis=1)))}
        if act.startswith("F"):
            s, e = ann[f"{act}/U{user:02d}_R{trial:02d}"]
            item["fall_s"] = (delivery_to_signal_time(rec["t_delivery"], s, rec["native_hz"]),
                              delivery_to_signal_time(rec["t_delivery"], e, rec["native_hz"]))
            a_g = rec["acc_g"]; i0 = max(0, int((item["fall_s"][0] - FALL_TOLERANCE_S) * OUT_HZ))
            i1 = min(len(a_g), int((item["fall_s"][1] + FALL_TOLERANCE_S) * OUT_HZ) + 1)
            item["max_g_in_fall"] = float(np.max(np.linalg.norm(a_g[i0:i1], axis=1))) if i1 > i0 else 0.0
            falls.append(item)
        else:
            adls.append(item)

    # ── falls ──
    def fall_hits(f, cfg, conv):
        return caught(alarms(f["res"][cfg], conv), *f["fall_s"])
    sens = {}
    for cfg in configs:
        for conv in ("a", "b"):
            if cfg == "model" and conv == "b":
                continue
            k = sum(fall_hits(f, cfg, conv) for f in falls)
            sens[f"{cfg}_{conv}"] = rate(k, len(falls))
    by_type = {}
    for code in sorted({f["act"] for f in falls}):
        ff = [f for f in falls if f["act"] == code]
        by_type[code] = {"name": FALL_NAMES[code], "n": len(ff),
                         "rule_b": sum(fall_hits(f, "rule", "b") for f in ff),
                         "model": sum(fall_hits(f, "model", "a") for f in ff),
                         "shipped_a": sum(fall_hits(f, "shipped", "a") for f in ff),
                         "shipped_b": sum(fall_hits(f, "shipped", "b") for f in ff),
                         "reached_2_0_g_in_fall": sum(f["max_g_in_fall"] >= 2.0 for f in ff),
                         "reached_2_5_g_in_fall": sum(f["max_g_in_fall"] >= 2.5 for f in ff)}
    f08 = [f for f in falls if f["act"] == "F08"]
    f08_side = {"toward_the_watch_side_R01_R02": rate(sum(fall_hits(f, "shipped", "b") for f in f08 if f["trial"] <= 2),
                                                      sum(1 for f in f08 if f["trial"] <= 2)),
                "away_from_the_watch_side_R03_R04": rate(sum(fall_hits(f, "shipped", "b") for f in f08 if f["trial"] >= 3),
                                                         sum(1 for f in f08 if f["trial"] >= 3))}
    overlap = {"both": 0, "model_only": 0, "rule_only": 0, "neither": 0}
    for f in falls:
        r, m = fall_hits(f, "rule", "b"), fall_hits(f, "model", "a")
        overlap["both" if r and m else "model_only" if m else "rule_only" if r else "neither"] += 1

    def miss_reason(f):
        lo, hi = f["fall_s"][0] - FALL_TOLERANCE_S, f["fall_s"][1] + FALL_TOLERANCE_S
        if caught([e["at_s"] for e in f["res"]["shipped"]["ignored_during_countdown"]], *f["fall_s"]):
            return "raised at the fall but ignored: an earlier alarm's countdown was already running"
        if f["max_g_in_fall"] < 2.0:
            return "the wrist never reached 2.0 g during the fall"
        ds = [d for d in f["res"]["shipped"]["decisions"] if lo <= d["peak_s"] <= hi]
        if ds:
            return "the model scored the fall below the threshold"
        return "a candidate opened before the fall (its window and decision used up the fall's impact)"
    missed = [f for f in falls if not fall_hits(f, "shipped", "b")]
    reasons = {}
    for f in missed:
        r = miss_reason(f)
        reasons[r] = reasons.get(r, 0) + 1
    p_missed = [max([d["p"] for d in f["res"]["shipped"]["decisions"] if d["p"] is not None
                     and f["fall_s"][0] - FALL_TOLERANCE_S <= d["peak_s"] <= f["fall_s"][1] + FALL_TOLERANCE_S] or [None], key=lambda v: -1 if v is None else v)
                for f in missed]
    p_scored = [p for p in p_missed if p is not None]
    shipped_res = [f["res"]["shipped"] for f in falls]
    beyond = {
        "covered_at_the_fall_or_by_a_countdown_already_running_when_it_began": rate(sum(
            fall_hits(f, "shipped", "b") or countdown_running_at(r, f["fall_s"][0] - FALL_TOLERANCE_S)
            for f, r in zip(falls, shipped_res)), len(falls)),
        "raised_at_the_fall_counting_alarms_a_running_countdown_ignored": rate(sum(
            caught(alarms(r, "b") + [e["at_s"] for e in r["ignored_during_countdown"]], *f["fall_s"])
            for f, r in zip(falls, shipped_res)), len(falls)),
        "earlier_alarm_in_the_fall_recording": sum(any(a < f["fall_s"][0] - FALL_TOLERANCE_S for a in alarms(r, "a"))
                                                   for f, r in zip(falls, shipped_res))}
    out_of_interval = {cfg: sum(sum(1 for t in alarms(f["res"][cfg], "a") if not caught([t], *f["fall_s"])) for f in falls) for cfg in configs}

    # ── daily activities ──
    def adl_block(items):
        hours = sum(i["seconds"] for i in items) / 3600
        blk = {"recordings": len(items), "hours": round(hours, 4), "people": len({i["user"] for i in items})}
        scored = [d["p"] for i in items for d in i["res"]["model"]["decisions"] if d["p"] is not None]
        flagged = sum(np.float32(p) >= np.float32(spec["threshold"]) for p in scored)
        blk["model_candidates"] = {"scored": len(scored), "at_or_above_threshold": int(flagged),
                                   "specificity_on_candidates": round(1 - flagged / len(scored), 4) if scored else None}
        for cfg in configs:
            for conv in ("a", "b"):
                if cfg == "model" and conv == "b":
                    continue
                n_al = [len(alarms(i["res"][cfg], conv)) for i in items]
                blk[f"{cfg}_{conv}"] = {"alarms": int(sum(n_al)), "alarms_per_hour": round(sum(n_al) / hours, 2) if hours else None,
                                        "recordings_with_an_alarm": int(sum(1 for n in n_al if n)),
                                        "alarms_per_recording": round(sum(n_al) / len(items), 4) if items else None,
                                        "specificity_recordings_without_an_alarm": round(1 - sum(1 for n in n_al if n) / len(items), 4) if items else None}
        return blk
    adl_out = {"young": adl_block([i for i in adls if i["group"] == "young"]),
               "older": adl_block([i for i in adls if i["group"] == "older"]),
               "all": adl_block(adls)}
    per_type = {}
    for code in sorted({i["act"] for i in adls}):
        per_type[code] = {"name": ADL_NAMES[code]}
        for grp in ("young", "older"):
            ii = [i for i in adls if i["act"] == code and i["group"] == grp]
            if not ii:
                continue
            per_type[code][grp] = {"recordings": len(ii), "reached_2_0_g": sum(i["max_g"] >= 2.0 for i in ii),
                                   **{f"specificity_{c}": round(1 - sum(1 for i in ii if alarms(i["res"][c.split('_')[0]], c.split('_')[1])) / len(ii), 4)
                                      for c in ("rule_b", "model_a", "shipped_a", "shipped_b")}}

    # ── failure cases (no raw data: identifiers, peaks and probabilities only) ──
    fa = []
    for i in adls:
        for r in i["res"]["shipped"]["raised"]:
            p = next((d["p"] for d in i["res"]["shipped"]["decisions"] if abs(d["peak_s"] - r["at_s"]) < 1e-9), None)
            fa.append({"recording": f"{i['act']}/U{i['user']:02d}_R{i['trial']:02d}", "group": i["group"], "path": r["path"],
                       "at_s": round(r["at_s"], 2), "peak_g": r["peak_g"], "p": p, "decision_used_padding": r["padded"]})
    miss_list = [{"recording": f"{f['act']}/U{f['user']:02d}_R{f['trial']:02d}", "max_g_in_fall": round(f["max_g_in_fall"], 2),
                  "best_p_in_fall": p, "reason": miss_reason(f)} for f, p in zip(missed, p_missed)]
    thr = np.float32(spec["threshold"])
    padded = {"decisions": sum(len(x["res"]["shipped"]["decisions"]) for x in falls + adls),
              "decisions_whose_window_touched_padding": sum(d["padded"] for x in falls + adls for d in x["res"]["shipped"]["decisions"]),
              "falls_caught_only_by_a_decision_that_used_padding": sum(
                  1 for f in falls if fall_hits(f, "shipped", "a")
                  and not any(r["path"] == "rule" and caught([r["at_s"]], *f["fall_s"]) for r in f["res"]["shipped"]["raised"])
                  and all(d["padded"] for d in f["res"]["shipped"]["decisions"]
                          if d["p"] is not None and np.float32(d["p"]) >= thr and caught([d["peak_s"]], *f["fall_s"])))}

    result = {
        "kind": "cross_dataset_fall_evaluation",
        "requirement": "F4.f",
        "notice": NOTICE,
        "pipeline": {"model": "fall model v3 (shipped, unchanged)", "mode": spec["mode"], "threshold": spec["threshold"],
                     "candidate_g": spec["candidateG"], "rule_settings": {"impact_g": 2.5, "still_seconds": 20, "still_threshold_g": 0.3, "moving_samples_tolerated": 3},
                     **model_info, "params": 6705, "trained_on": spec["trained_on"],
                     "retrained": False, "threshold_changed": False},
        "dataset": {"name": "WEDA-FALL", "source": "https://github.com/joaojtmarques/WEDA-FALL", "commit": args.commit,
                    "licence": "ODC-By 1.0 (Open Data Commons Attribution License v1.0)",
                    "citation": "J. Marques and P. Moreno, Online Fall Detection Using Wrist Devices, Sensors 23(3):1146, 2023",
                    "device": "Fitbit Sense, wrist", "files_used": prep["files"], "refused": refused,
                    "fall_recordings": len(falls), "adl_recordings": len(adls),
                    "young_people": len({f["user"] for f in falls + adls if f["group"] == "young"}),
                    "older_people": len({f["user"] for f in adls if f["group"] == "older"})},
        "transforms": {"unit": "m/s^2 divided by 9.80665 (checked: median magnitude about 1 g)",
                       "axes": "Fitbit device frame, unchanged", "time": "rows ordered by delivery time; sample i at i / native rate",
                       "native_hz_files": prep["native_hz"], "files_with_rows_reordered": prep["rows_reordered_files"],
                       "resampling": "linear interpolation to 50 Hz", "padding_s": {"before": PRE_PAD_S, "after": POST_PAD_S},
                       "axis_samples_at_the_fitbit_4_g_limit": prep["clipped_axis_samples"]},
        "scoring": {"fall_tolerance_s": FALL_TOLERANCE_S, "countdown_ms": COUNTDOWN_MS,
                    "conventions": {"a": "only complete 20 s still windows count for the rule",
                                    "b": "also a rule impact still waiting when the recording ended"}},
        "falls": {"sensitivity": sens, "shipped_beyond_the_strict_count": beyond, "by_type": by_type, "f08_by_side_shipped_b": f08_side,
                  "rule_b_vs_model": overlap, "missed_by_shipped_b": len(missed), "missed_reasons": reasons,
                  "missed_scored_by_model_p_median": round(float(np.median(p_scored)), 4) if p_scored else None,
                  "alarms_outside_the_annotated_fall": out_of_interval},
        "adl": adl_out, "adl_by_type": per_type,
        "padding": padded,
        "failure_cases": {"adl_false_alarms_shipped": sorted(fa, key=lambda r: (r["group"], r["recording"])),
                          "missed_falls_shipped_b": miss_list},
        "latency": {**latency(infer), "machine": f"{platform.machine()}, {os.cpu_count()} CPUs shared, Python {platform.python_version()}",
                    "what": "one TFLite invoke on a 200 x 4 window, one thread"},
        "seconds": round(time.time() - t_start, 1),
    }
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({"sensitivity": sens, "adl_older": adl_out["older"], "adl_young": adl_out["young"]}, indent=1))


if __name__ == "__main__":
    main()
