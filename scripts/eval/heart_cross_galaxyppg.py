# IRA CARE: independent heart-rate evaluation of the research pipeline on GalaxyPPG.
#
# What is evaluated, unchanged: care/heart_research.py (D-086) with the shipped classical
# estimator (care/heart_estimate.py) and the shipped learned selector
# (care/models/heart_selector.json, trained on all 15 PPG-DaLiA subjects). Nothing is retrained,
# no setting is changed, and nothing here is chosen by looking at GalaxyPPG's results.
#
# GalaxyPPG (Park, Zheng, Lee, KAIST; Zenodo 14635823, doi:10.5281/zenodo.14635823, CC BY 4.0;
# Sci Data 2025, doi:10.1038/s41597-025-05152-z): 24 young adults; Empatica E4 BVP at 64 Hz and
# accelerometer at 32 Hz (1/64 g counts), Galaxy Watch 5 green PPG at 25 Hz and accelerometer
# in m/s^2, Polar H10 chest ECG at 130 Hz as the reference; baseline, stress tasks, typing,
# standing, walking, jogging, running and rests.
#
# Reference heart rate, fixed before any result was looked at:
#   - R peaks by the two-moving-average detector of Elgendi, Jonkman and De Boer (BIOSIGNALS
#     2010; "elgendi2010" in NeuroKit2): 8-20 Hz band-pass, squared, 97 ms and 611 ms moving
#     averages, offset 0.08 times the mean energy (over a centred 30 s here), 250 ms refractory.
#     (A Pan-Tompkins implementation was tried first. Checked against the strap's own beat count
#     before any heart-rate estimate was looked at, it lost track after artefacts: 2 beats in an
#     hour for P17, a 349 s silence for P10. It was replaced.)
#   - Per 8 s window: 60 / mean RR of the beats inside it. No reference when the ECG has a gap
#     in the window, fewer than 4 RR intervals, an RR outside 0.27-2.0 s, or an RR more than 40 %
#     from the window's median RR (a missed or extra beat). Agreement with the strap's own
#     heart rate (PolarH10/HR.csv) is reported as a check on the detector.
#
# Time alignment:
#   - Polar timestamps are UTC+9 (the dataset's README); 9 h is subtracted.
#   - The chest strap and the wrist devices have separate clocks. The lag between them is
#     measured from movement alone: 1 s moving standard deviation of the acceleration
#     magnitude, 10 Hz, wrist against chest, the best correlation within +/-10 s. The ECG
#     reference is shifted by that lag. A participant whose lag is over MAX_LAG_S, or whose
#     best correlation is under MIN_CORR, is refused as misaligned: no numbers from them.
#   - Activities from Event.csv; a window that is not wholly inside one session is "transition".
#
# Units: E4 accelerometer as the pipeline's "e4_raw" (1/64 g); Galaxy Watch accelerometer as
# "m/s2". Each is checked first: the median magnitude must be 0.5-2.0 g in the declared unit,
# so a stream declared in the wrong unit is refused rather than silently misread.
#
#   python3 -I scripts/eval/heart_cross_galaxyppg.py --zip <scratch>/galaxy/GalaxyPPG.zip \
#       --out docs/ai/heart/cross_dataset_galaxyppg.json
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))

G = 9.80665
UTC9_S = 9 * 3600
ECG_HZ = 130.0
MAX_LAG_S = 5.0
MIN_CORR = 0.5
SEARCH_LAG_S = 10.0
ENV_HZ = 10
UNIT_BAND_G = (0.5, 2.0)
ACC_SCALE = {"g": 1.0, "m/s2": 1.0 / G, "e4_raw": 1.0 / 64.0, "mg": 1e-3}
RR_RANGE_S = (0.27, 2.0)
RR_JUMP = 0.40
MIN_RR = 4
ECG_GAP_S = 0.1
ZIP_MD5 = "d9a48bfcd07928fb22b96dc40cf2e7d4"
PPG_DALIA = {"classical_mean_sd_of_subjects": "13.28 +/- 5.01", "learned_mean_sd_of_subjects": "7.75 +/- 3.23",
             "classical_pooled": 13.23, "learned_pooled": 7.68,
             "source": "docs/ai/heart/MODEL_CARD.md (leave-one-subject-out; every window, before any rejection)"}


class AlignmentError(ValueError):
    """A stream whose units or timing cannot be trusted. ``code`` names the rule."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── pure parts: units, time, R peaks, reference ──────────────────────────────

def check_acc_unit(samples: np.ndarray, unit: str) -> float:
    """Median magnitude in g when ``samples`` are read in ``unit``; refused outside UNIT_BAND_G."""
    if unit not in ACC_SCALE:
        raise AlignmentError("bad_unit", f"unknown unit {unit!r}")
    a = np.asarray(samples, float)
    a = a[np.all(np.isfinite(a), axis=1)]
    med = float(np.median(np.linalg.norm(a, axis=1))) * ACC_SCALE[unit]
    if not UNIT_BAND_G[0] <= med <= UNIT_BAND_G[1]:
        raise AlignmentError("unit_mismatch", f"median magnitude {med:.3f} g when read as {unit}: wrong unit")
    return med


def polar_seconds(phone_ms: np.ndarray) -> np.ndarray:
    """Polar phoneTimestamp (ms, UTC+9 per the README) as UTC seconds."""
    return np.asarray(phone_ms, float) / 1e3 - UTC9_S


def motion_envelope(t: np.ndarray, acc_g: np.ndarray, t0: float, t1: float, hz: int = ENV_HZ) -> np.ndarray:
    """1 s moving standard deviation of |a| (g), sampled at ``hz`` on [t0, t1)."""
    mag = np.linalg.norm(acc_g, axis=1)
    n = int((t1 - t0) * hz)
    k = np.floor((t - t0) * hz).astype(np.int64)
    ok = (k >= 0) & (k < n)
    c = np.bincount(k[ok], minlength=n).astype(float)
    s = np.bincount(k[ok], mag[ok], minlength=n)
    s2 = np.bincount(k[ok], mag[ok] ** 2, minlength=n)
    box = np.ones(hz)
    C, S, S2 = (np.convolve(v, box, "same") for v in (c, s, s2))
    with np.errstate(invalid="ignore", divide="ignore"):
        var = S2 / C - (S / C) ** 2
    return np.sqrt(np.clip(np.nan_to_num(var), 0, None))


def estimate_lag(env_wrist: np.ndarray, env_chest: np.ndarray, hz: int = ENV_HZ, search_s: float = SEARCH_LAG_S):
    """(lag_s, correlation): the chest envelope matches the wrist one shifted by lag_s, i.e. an
    event at chest time t is at wrist time t + lag_s."""
    n = min(len(env_wrist), len(env_chest))
    a, b = env_wrist[:n], env_chest[:n]
    best = (0.0, -2.0)
    for k in range(-int(search_s * hz), int(search_s * hz) + 1):
        x, y = (a[k:], b[:n - k]) if k >= 0 else (a[:n + k], b[-k:])
        if len(x) < 10 * hz or np.std(x) == 0 or np.std(y) == 0:
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if r > best[1]:
            best = (k / hz, r)
    return best


def check_alignment(lag_s: float, corr: float) -> None:
    if not math.isfinite(corr) or corr < MIN_CORR:
        raise AlignmentError("misaligned", f"movement correlation {corr:.2f} below {MIN_CORR}: no common timebase")
    if abs(lag_s) > MAX_LAG_S:
        raise AlignmentError("misaligned", f"clocks {lag_s:+.1f} s apart (at most {MAX_LAG_S:g} s)")


def _bandpass(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x - np.mean(x))
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, n=len(x))


def _moving_mean(x: np.ndarray, w: int) -> np.ndarray:
    w = max(1, int(w))
    c = np.cumsum(np.r_[0.0, x])
    lo = np.clip(np.arange(len(x)) - w // 2, 0, len(x))
    hi = np.clip(lo + w, 0, len(x))
    return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)


def r_peaks(ecg: np.ndarray, fs: float = ECG_HZ) -> np.ndarray:
    """R-peak indices on one contiguous ECG segment: the two-moving-average detector of Elgendi
    et al. (2010): 8-20 Hz band-pass, squared; blocks where the 97 ms moving average exceeds the
    611 ms one plus 0.08 times the mean (here the mean over a centred 30 s, so one artefact does
    not blind a whole recording); a block at least 97 ms long holds one beat, at its largest
    squared value; beats closer than 250 ms keep the larger."""
    x = np.asarray(ecg, float)
    if len(x) < 2 * fs:
        return np.zeros(0, int)
    y = _bandpass(x, fs, 8.0, 20.0) ** 2
    w1, w2 = int(round(0.097 * fs)), int(round(0.611 * fs))
    ma_qrs, ma_beat = _moving_mean(y, w1), _moving_mean(y, w2)
    thr = ma_beat + 0.08 * _moving_mean(y, int(30 * fs))
    on = ma_qrs > thr
    edges = np.flatnonzero(np.diff(np.r_[0, on.astype(np.int8), 0]))
    peaks: list[int] = []
    refr = int(0.250 * fs)
    for a, b in zip(edges[::2], edges[1::2]):
        if b - a < w1:
            continue
        i = a + int(np.argmax(y[a:b]))
        if peaks and i - peaks[-1] < refr:
            if y[i] > y[peaks[-1]]:
                peaks[-1] = i
            continue
        peaks.append(i)
    return np.array(peaks, int)


def ecg_beats(t: np.ndarray, ecg: np.ndarray, fs: float = ECG_HZ, gap_s: float = ECG_GAP_S):
    """Beat times (s) over every contiguous segment, and the gaps [(start, end)] between them."""
    t = np.asarray(t, float)
    if np.any(np.diff(t) <= 0):
        raise AlignmentError("not_monotonic", "ECG timestamps must increase")
    cut = np.flatnonzero(np.diff(t) > gap_s)
    bounds = np.r_[0, cut + 1, len(t)]
    beats, gaps = [], [(float(t[c]), float(t[c + 1])) for c in cut]
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a > 2 * fs:
            beats.append(t[a:b][r_peaks(ecg[a:b], fs)])
    return (np.concatenate(beats) if beats else np.zeros(0)), gaps


def window_reference(beats: np.ndarray, gaps, t0: float, window_s: float = 8.0):
    """(bpm or None, reason or None) for the window [t0, t0 + window_s)."""
    t1 = t0 + window_s
    if any(g0 < t1 and g1 > t0 for g0, g1 in gaps):
        return None, "ecg_gap"
    b = beats[(beats >= t0) & (beats < t1)]
    rr = np.diff(b)
    if len(rr) < MIN_RR:
        return None, "too_few_beats"
    if rr.min() < RR_RANGE_S[0] or rr.max() > RR_RANGE_S[1]:
        return None, "implausible_rr"
    med = float(np.median(rr))
    if np.any(np.abs(rr - med) > RR_JUMP * med):
        return None, "irregular_rr"
    return 60.0 / float(np.mean(rr)), None


def activity_at(events: list, t0: float, t1: float) -> str:
    """The session wholly containing [t0, t1), else "transition" (or "unlabelled" with no events)."""
    if not events:
        return "unlabelled"
    for name, a, b in events:
        if a <= t0 and t1 <= b:
            return name
    return "transition"


def group_activity(name: str) -> str:
    """Event.csv sessions grouped: numbered repeats (rest-1..5, meditation-1..2) together."""
    base = name.rsplit("-", 1)[0] if name.rsplit("-", 1)[-1].isdigit() else name
    return base


# ── reading the archive ──────────────────────────────────────────────────────

def _csv(z: zipfile.ZipFile, name: str, cols: list[str]) -> np.ndarray:
    with z.open(name) as fh:
        hdr = fh.readline().decode().strip().split(",")
        idx = [hdr.index(c) for c in cols]
        return np.loadtxt(io.TextIOWrapper(fh), delimiter=",", usecols=idx, dtype=np.float64, ndmin=2)


def _events(z: zipfile.ZipFile, p: str) -> list:
    name = f"Dataset/{p}/Event.csv"
    if name not in z.namelist():
        return []
    rows = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))
    open_, out = {}, []
    for r in rows:
        if r["status"] == "ENTER":
            open_[r["session"]] = int(r["timestamp"]) / 1e3
        elif r["status"] == "EXIT" and r["session"] in open_:
            out.append((r["session"], open_.pop(r["session"]), int(r["timestamp"]) / 1e3))
    return out


def _mae_rmse(est, ref):
    e = np.asarray(est, float) - np.asarray(ref, float)
    e = e[np.isfinite(e)]
    if not len(e):
        return {"windows": 0, "mae_bpm": None, "rmse_bpm": None}
    return {"windows": int(len(e)), "mae_bpm": round(float(np.mean(np.abs(e))), 2), "rmse_bpm": round(float(np.sqrt(np.mean(e ** 2))), 2)}


def evaluate_participant(args):
    zip_path, p = args
    from care import heart_research as hr
    z = zipfile.ZipFile(zip_path)
    out = {"participant": p, "devices": {}}
    ecg = _csv(z, f"Dataset/{p}/PolarH10/ECG.csv", ["phoneTimestamp", "ecg"])
    t_ecg = polar_seconds(ecg[:, 0])
    pol = _csv(z, f"Dataset/{p}/PolarH10/ACC.csv", ["phoneTimestamp", "X", "Y", "Z"])
    t_pol, a_pol = polar_seconds(pol[:, 0]), pol[:, 1:] * ACC_SCALE["mg"]
    check_acc_unit(pol[:, 1:], "mg")
    beats, gaps = ecg_beats(t_ecg, ecg[:, 1])
    strap = _csv(z, f"Dataset/{p}/PolarH10/HR.csv", ["phoneTimestamp", "hr"])
    t_strap = polar_seconds(strap[:, 0])
    events = _events(z, p)

    streams = {"E4": None, "GalaxyWatch": None}
    bvp = _csv(z, f"Dataset/{p}/E4/BVP.csv", ["timestamp", "value"])
    e4a = _csv(z, f"Dataset/{p}/E4/ACC.csv", ["timestamp", "x", "y", "z"])
    streams["E4"] = {"ppg_t": bvp[:, 0] / 1e6, "ppg": bvp[:, 1], "ppg_hz": 64.0, "ppg_unit": "E4 BVP (arbitrary units)",
                     "acc_t": e4a[:, 0] / 1e6, "acc": e4a[:, 1:], "acc_hz": 32.0, "acc_unit": "e4_raw"}
    if f"Dataset/{p}/GalaxyWatch/PPG.csv" in z.namelist():
        gp = _csv(z, f"Dataset/{p}/GalaxyWatch/PPG.csv", ["timestamp", "ppg", "status"])
        ga = _csv(z, f"Dataset/{p}/GalaxyWatch/ACC.csv", ["timestamp", "x", "y", "z"])
        ppg = np.where(gp[:, 2] == 0, gp[:, 1], np.nan)          # status != 0: not a valid sample
        streams["GalaxyWatch"] = {"ppg_t": gp[:, 0] / 1e3, "ppg": ppg, "ppg_hz": 25.0, "ppg_unit": "Galaxy Watch green PPG ADC",
                                  "acc_t": ga[:, 0] / 1e3, "acc": ga[:, 1:], "acc_hz": 25.0, "acc_unit": "m/s2",
                                  "invalid_ppg_samples": int(np.count_nonzero(gp[:, 2] != 0))}
    for dev, s in streams.items():
        if s is None:
            out["devices"][dev] = {"status": "not recorded"}
            continue
        d = {"invalid_ppg_samples": s.get("invalid_ppg_samples", 0)}
        try:
            d["acc_median_g"] = round(check_acc_unit(s["acc"], s["acc_unit"]), 4)
            acc_g = s["acc"] * ACC_SCALE[s["acc_unit"]]
            t0, t1 = max(s["acc_t"][0], t_pol[0]), min(s["acc_t"][-1], t_pol[-1])
            lag, corr = estimate_lag(motion_envelope(s["acc_t"], acc_g, t0, t1), motion_envelope(t_pol, a_pol, t0, t1))
            d["clock_lag_s"], d["movement_correlation"] = lag, round(corr, 3)
            check_alignment(lag, corr)
        except AlignmentError as e:
            d.update({"status": "refused", "refusal": e.code, "why": str(e)})
            out["devices"][dev] = d
            continue
        base = float(min(s["ppg_t"][0], s["acc_t"][0]))
        rec = {"source": "research_recording",
               "ppg": {"fs_hz": s["ppg_hz"], "unit": s["ppg_unit"], "timestamps_s": (s["ppg_t"] - base).tolist(),
                       "samples": [None if not np.isfinite(v) else float(v) for v in s["ppg"]]},
               "acc": {"fs_hz": s["acc_hz"], "unit": s["acc_unit"], "timestamps_s": (s["acc_t"] - base).tolist(),
                       "samples": s["acc"].tolist()},
               "label": f"GalaxyPPG {p} {dev}", "dataset": "GalaxyPPG", "licence": "CC BY 4.0"}
        try:
            r = hr.validate_recording(rec, name=f"{p}-{dev}", max_seconds=None, max_samples=None)
        except hr.ContractError as e:
            d.update({"status": "refused by the pipeline's data contract", "refusal": e.code, "why": str(e)})
            out["devices"][dev] = d
            continue
        t_run = time.perf_counter()
        res = hr.run(r)
        d["run_seconds"] = round(time.perf_counter() - t_run, 1)
        d["status"] = "evaluated"
        d["model"] = res.provenance["learned_model"].get("version")
        rows = []
        for w in res.windows:
            ws = base + w.t_start_s                       # wrist clock, UTC seconds
            ref, why = window_reference(beats, gaps, ws - lag)
            m = (t_strap >= ws - lag) & (t_strap < ws - lag + 8.0)
            rows.append({"t": ws, "act": activity_at(events, ws, ws + 8.0), "ref": ref, "ref_reason": why,
                         "strap_hr": float(np.mean(strap[m, 1])) if m.any() else None,
                         "status": w.status, "reason": w.reason, "bpm": w.bpm, "method": w.method,
                         "classical": (w.classical or {}).get("candidate_bpm"), "learned": (w.learned or {}).get("candidate_bpm")})
        d["windows"] = rows
        d["resources"] = res.resources
        out["devices"][dev] = d
    out["beats"] = int(len(beats))
    out["ecg_gaps"] = len(gaps)
    return out


def summarise(parts: list, dev: str) -> dict:
    rows = [dict(w, participant=p["participant"]) for p in parts
            for w in p["devices"].get(dev, {}).get("windows", [])]
    if not rows:
        return {"windows": 0}
    f = lambda k: np.array([np.nan if w[k] is None else w[k] for w in rows], float)  # noqa: E731
    ref, cls, lrn, rep = f("ref"), f("classical"), f("learned"), f("bpm")
    has_ref = np.isfinite(ref)
    rep_ok = np.array([w["status"] == "estimate" for w in rows])
    meth = np.array([w["method"] or "" for w in rows])
    out = {"windows": len(rows), "windows_with_reference": int(has_ref.sum()),
           "reference_unavailable_by_reason": {}, "reported": int(rep_ok.sum()),
           "reported_share": round(float(rep_ok.mean()), 4), "unavailable_by_reason": {},
           "learned_estimates": int(((meth == "learned") & rep_ok).sum()),
           "classical_fallbacks": int(((meth == "classical") & rep_ok).sum())}
    for w in rows:
        if w["ref_reason"]:
            out["reference_unavailable_by_reason"][w["ref_reason"]] = out["reference_unavailable_by_reason"].get(w["ref_reason"], 0) + 1
        if w["status"] != "estimate":
            out["unavailable_by_reason"][w["reason"]] = out["unavailable_by_reason"].get(w["reason"], 0) + 1
    out["pooled"] = {"classical_every_window": _mae_rmse(cls[has_ref], ref[has_ref]),
                     "learned_every_window": _mae_rmse(lrn[has_ref], ref[has_ref]),
                     "reported_by_the_pipeline": _mae_rmse(rep[has_ref & rep_ok], ref[has_ref & rep_ok]),
                     "reported_learned": _mae_rmse(rep[has_ref & rep_ok & (meth == "learned")], ref[has_ref & rep_ok & (meth == "learned")]),
                     "reported_classical_fallback": _mae_rmse(rep[has_ref & rep_ok & (meth == "classical")], ref[has_ref & rep_ok & (meth == "classical")]),
                     "reported_share_of_windows_with_reference": round(float(rep_ok[has_ref].mean()), 4)}
    per = {}
    for pid in sorted({w["participant"] for w in rows}):
        m = np.array([w["participant"] == pid for w in rows]) & has_ref
        per[pid] = {"windows": int(m.sum()), "classical_mae": _mae_rmse(cls[m], ref[m])["mae_bpm"],
                    "learned_mae": _mae_rmse(lrn[m], ref[m])["mae_bpm"],
                    "reported_mae": _mae_rmse(rep[m & rep_ok], ref[m & rep_ok])["mae_bpm"],
                    "reported_share": round(float(rep_ok[m].mean()), 4) if m.any() else None}
    out["per_participant"] = per

    def msd(k):
        v = np.array([x[k] for x in per.values() if x[k] is not None], float)
        return {"mean": round(float(v.mean()), 2), "sd": round(float(v.std()), 2), "participants": int(len(v))}
    out["mean_sd_of_participants"] = {"classical_every_window": msd("classical_mae"), "learned_every_window": msd("learned_mae"),
                                      "reported_by_the_pipeline": msd("reported_mae")}
    acts = {}
    for a in sorted({group_activity(w["act"]) for w in rows}):
        m = np.array([group_activity(w["act"]) == a for w in rows])
        mr = m & has_ref
        acts[a] = {"windows": int(m.sum()), "with_reference": int(mr.sum()),
                   "classical_every_window": _mae_rmse(cls[mr], ref[mr]), "learned_every_window": _mae_rmse(lrn[mr], ref[mr]),
                   "reported": _mae_rmse(rep[mr & rep_ok], ref[mr & rep_ok]),
                   "reported_share": round(float(rep_ok[mr].mean()), 4) if mr.any() else None}
    out["by_activity"] = acts
    strap = f("strap_hr")
    ms = has_ref & np.isfinite(strap)
    out["reference_check_against_the_strap_hr"] = {**_mae_rmse(ref[ms], strap[ms]),
                                                   "within_5_bpm_share": round(float(np.mean(np.abs(ref[ms] - strap[ms]) <= 5)), 4) if ms.any() else None}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--participants", nargs="*")
    a = ap.parse_args()
    t_start = time.time()
    md5 = hashlib.md5()
    with open(a.zip, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            md5.update(chunk)
    if md5.hexdigest() != ZIP_MD5:
        raise SystemExit("GalaxyPPG.zip does not match the Zenodo record's MD5")
    z = zipfile.ZipFile(a.zip)
    pids = a.participants or sorted({n.split("/")[1] for n in z.namelist() if n.startswith("Dataset/P") and n.count("/") >= 2})
    with ProcessPoolExecutor(a.workers) as ex:
        parts = list(ex.map(evaluate_participant, [(str(a.zip), p) for p in pids]))
    from care import heart_learned as hl
    from care import heart_research as hr
    _, prov = hr.model_provenance()
    devices = {}
    for dev in ("E4", "GalaxyWatch"):
        devices[dev] = {"participants": {p["participant"]: {k: v for k, v in p["devices"][dev].items() if k not in ("windows",)}
                                         for p in parts},
                        **summarise(parts, dev)}
    result = {
        "kind": "independent_heart_rate_evaluation",
        "pipeline": {"module": "care/heart_research.py", "version": hr.PIPELINE_VERSION, "learned_model": prov,
                     "classical": hr.classical_provenance(), "retrained": False, "settings_changed": False,
                     "min_probability": hl.MIN_PROBABILITY, "motion_reject_g": hr.MOTION_REJECT_G},
        "dataset": {"name": "GalaxyPPG", "record": "https://zenodo.org/records/14635823", "doi": "10.5281/zenodo.14635823",
                    "licence": "CC BY 4.0 (Zenodo record metadata: cc-by-4.0)", "archive_md5": ZIP_MD5,
                    "paper": "Sci Data 2025, doi:10.1038/s41597-025-05152-z", "participants": len(parts)},
        "reference": {"detector": "Elgendi et al. 2010 two-moving-average QRS detector on Polar H10 ECG, 130 Hz", "per_window": "60 / mean RR of the beats in the 8 s window",
                      "rr_range_s": RR_RANGE_S, "rr_jump": RR_JUMP, "min_rr": MIN_RR, "ecg_gap_s": ECG_GAP_S},
        "alignment": {"polar_utc_offset_h": 9, "lag_from": "1 s moving SD of |acc|, wrist against chest, 10 Hz",
                      "max_lag_s": MAX_LAG_S, "min_correlation": MIN_CORR},
        "ppg_dalia_for_comparison": PPG_DALIA,
        "devices": devices,
        "machine": f"{platform.machine()}, {os.cpu_count()} CPUs shared, Python {platform.python_version()}, numpy {np.__version__}",
        "seconds": round(time.time() - t_start, 1),
    }
    a.out.write_text(json.dumps(result, indent=1) + "\n")
    for dev in devices:
        d = devices[dev]
        print(dev, {k: d.get(k) for k in ("windows", "windows_with_reference", "reported_share", "pooled", "mean_sd_of_participants")})


if __name__ == "__main__":
    main()
