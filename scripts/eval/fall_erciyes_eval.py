#!/usr/bin/env python3
"""The shipped fall pipeline on a dataset it never saw: Erciyes "Simulated Falls and Daily Living
Activities" (UCI 455, CC BY 4.0), right-wrist sensor.

    python3 scripts/eval/fall_erciyes_eval.py --data-dir /path/outside/the/repo/x \\
        --out docs/ai/fall/erciyes_eval.json

Data: Özdemir, A.T. & Barshan, B. (2014), UCI Machine Learning Repository, doi:10.24432/C52028,
"Creative Commons Attribution 4.0 International (CC BY 4.0) ... for any purpose, provided that the
appropriate credit is given" (the UCI page, read 28 November 2026). Fetched outside the repository
and unpacked there (`--data-dir` holds the subject folders 101 ... 209); raw data is never
committed. 17 volunteers (10 men, 7 women in this archive), 20 fall types and 16 daily activities,
about five repetitions each; Xsens MTw sensors at 25 Hz, the right wrist is sensor 340537.

What runs, unchanged: the shipped pipeline (`fall_synthetic_eval.run_pipeline`: the rule detector
and `RecoveringFallDetector` with the shipped `fall_model.tflite` through LiteRT), and the
confirmation stage (`confirm_all`, switched off in the product, D-110) on each suspected fall.

Scored per recording: a fall recording is caught when the pipeline raises at least once; a daily
activity recording raises a false alarm when it raises at all. Reported overall, per fall type and
per activity, and per subject.

Honest limits, written into the output:
  * the shipped model never saw this dataset (trained on BITS-2 and UCI HAR): every number here is
    out-of-sample;
  * the falls are simulated by young volunteers onto mats, not real falls of older people;
  * the sensor is a research IMU on the right wrist, not the IRA watch, with its own axes: the
    acceleration is in m/s², converted to g (÷ 9.80665);
  * it samples at 25 Hz; the watch pipeline runs at 50 Hz, so each recording is resampled by linear
    interpolation, which cannot restore content above 12.5 Hz: impact peaks may be lower than a 50 Hz
    watch would record, which can only make the impact rule fire less often (a pessimistic bias for
    sensitivity, an optimistic one for false alarms).
"""
from __future__ import annotations

import argparse
import collections
import json
import platform
import re
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import fall_confirmation as fc  # noqa: E402
from fall_synthetic_eval import confirm_all, load_shipped_litert, run_pipeline  # noqa: E402

WRIST = "340537"
SOURCE_HZ = 25.0
G = 9.80665
LICENCE = "CC BY 4.0 (UCI 455, doi:10.24432/C52028)"


def read_wrist(path: Path) -> np.ndarray | None:
    """Acc_X, Acc_Y, Acc_Z of one Xsens export, in m/s², rows with all three present."""
    rows = []
    header = None
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("//") or not line.strip():
            continue
        cells = line.rstrip("\t").split("\t")
        if header is None:
            header = cells
            ix = [header.index(c) for c in ("Acc_X", "Acc_Y", "Acc_Z")]
            continue
        try:
            rows.append([float(cells[i]) for i in ix])
        except (ValueError, IndexError):
            continue
    if header is None or len(rows) < int(SOURCE_HZ * 2):
        return None
    return np.asarray(rows, dtype=np.float64)


def to_watch(acc_ms2: np.ndarray) -> np.ndarray:
    """25 Hz m/s² to the pipeline's 50 Hz g, by linear interpolation."""
    n = len(acc_ms2)
    t_src = np.arange(n) / SOURCE_HZ
    t_dst = np.arange(0, t_src[-1], 1.0 / fc.HZ)
    out = np.stack([np.interp(t_dst, t_src, acc_ms2[:, k]) for k in range(3)], axis=1) / G
    return out.astype(np.float32)


def recordings(data_dir: Path):
    pattern = re.compile(r"^(\d{3})-")
    for subject in sorted(p for p in data_dir.iterdir() if p.is_dir() and p.name.isdigit()):
        export = subject / "Testler Export"
        if not export.is_dir():
            continue
        for activity in sorted(export.iterdir()):
            m = pattern.match(activity.name)
            if not m:
                continue
            code = int(m.group(1))
            for test in sorted(activity.iterdir()):
                f = test / f"{WRIST}.txt"
                if f.is_file():
                    yield subject.name, code, activity.name, test.name, f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, type=Path, help="the unpacked subject folders, outside the repository")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    if ROOT in a.data_dir.resolve().parents or a.data_dir.resolve() == ROOT:
        raise SystemExit("--data-dir must be outside the repository: raw data is never committed")
    t0 = time.time()
    spec, infer, model_info = load_shipped_litert()

    recs, unreadable = [], []
    for subject, code, activity, test, f in recordings(a.data_dir):
        acc = read_wrist(f)
        if acc is None:
            unreadable.append(f"{subject}/{activity}/{test}")
            continue
        xyz = to_watch(acc)
        raised = run_pipeline(xyz, spec, infer)
        decided = confirm_all(xyz, raised) if raised else []
        recs.append({"subject": subject, "code": code, "activity": activity.split("-", 1)[1] if "-" in activity else activity,
                     "test": test, "fall": code >= 900, "seconds": round(len(xyz) / fc.HZ, 1),
                     "peak_g": round(float(np.max(np.linalg.norm(xyz, axis=1))), 2),
                     "raised": len(raised), "confirmed": sum(d["confirmed"] for d in decided)})

    def rate(xs, key):
        return round(sum(1 for r in xs if r[key] > 0) / len(xs), 4) if xs else None

    falls = [r for r in recs if r["fall"]]
    adls = [r for r in recs if not r["fall"]]
    by_code = collections.defaultdict(list)
    for r in recs:
        by_code[(r["code"], r["activity"])].append(r)
    per_activity = [{"code": c, "activity": n, "fall": c >= 900, "recordings": len(v),
                     "raised": sum(1 for r in v if r["raised"] > 0), "confirmed": sum(1 for r in v if r["confirmed"] > 0)}
                    for (c, n), v in sorted(by_code.items())]
    by_subject = collections.defaultdict(list)
    for r in falls:
        by_subject[r["subject"]].append(r)
    per_subject = {s: {"falls": len(v), "caught": sum(1 for r in v if r["raised"] > 0)} for s, v in sorted(by_subject.items())}
    lost = [f"{r['subject']}/{r['code']}/{r['test']}" for r in falls if r["raised"] > 0 and r["confirmed"] == 0]

    summary = {
        "fall_recordings": len(falls), "falls_caught": sum(1 for r in falls if r["raised"] > 0),
        "sensitivity": rate(falls, "raised"),
        "adl_recordings": len(adls), "adl_recordings_with_an_alarm": sum(1 for r in adls if r["raised"] > 0),
        "adl_alarm_rate": rate(adls, "raised"), "adl_hours": round(sum(r["seconds"] for r in adls) / 3600, 3),
        "confirmation_stage_off_in_the_product": {
            "falls_caught_after": sum(1 for r in falls if r["confirmed"] > 0),
            "falls_it_would_lose": len(lost), "lost": lost,
            "adl_alarms_after": sum(1 for r in adls if r["confirmed"] > 0),
            "gate_no_caught_fall_lost": not lost},
        "unreadable_recordings": unreadable,
    }
    out = {"what": "the shipped fall pipeline, unchanged, on the Erciyes right-wrist recordings",
           "data": {"name": "Simulated Falls and Daily Living Activities (Erciyes, UCI 455)", "licence": LICENCE,
                    "sensor": f"right wrist, Xsens MTw {WRIST}, 25 Hz, m/s²", "fetched": "outside the repository"},
           "model": model_info,
           "limits": ["out-of-sample: the shipped model never saw this dataset",
                      "simulated falls by young volunteers onto mats, not real falls of older people",
                      "a research IMU on the right wrist with its own axes, not the IRA watch",
                      "25 Hz resampled to 50 Hz by linear interpolation: impact peaks above 12.5 Hz content are not restored"],
           "summary": summary, "per_activity": per_activity, "per_subject_falls": per_subject,
           "machine": f"{platform.machine()}, {platform.system()} {platform.release()}",
           "seconds_to_run": round(time.time() - t0, 1)}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
