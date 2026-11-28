#!/usr/bin/env python3
"""A low-impact candidate for the shipped fall pipeline (D-115): develop on BITS-2 and UCI HAR,
then test the ONE chosen setting once on Erciyes.

    python3 scripts/eval/fall_lowimpact_candidate.py dev --data-dir /outside/the/repo \\
        --out docs/ai/fall/lowimpact_dev.json
    python3 scripts/eval/fall_lowimpact_candidate.py heldout --erciyes-dir /outside/x \\
        --candidate-g 1.6 --threshold 0.9 --out docs/ai/fall/lowimpact_erciyes.json

Why: on Erciyes (held out, D-113) the shipped pipeline caught 87.6 % of simulated falls and missed
mostly low-impact ones (rolling out of bed, sliding down a wall, onto the knees). The model only
sees an event whose peak reaches `candidateG` (2.0 g) and decides at `threshold` (0.95): a soft
fall never reaches it. This varies those two numbers only; the model file, the rule detector and
everything else stay as shipped.

Development (`dev`): every BITS-2 fall and daily-activity recording (Zenodo 10013090, CC BY 4.0) and
every UCI HAR segment (UCI 240, CC BY 4.0), fetched with the training scripts' SHA-256 pins. The
model was trained on all BITS-2 subjects, so its BITS-2 catch rate is in-sample; it is used here to
check no caught fall is lost and to count alarms. The setting is chosen on this data alone by the rule
written in `choose()`, before Erciyes is read.

Held-out (`heldout`): the chosen setting and the shipped one, once, on Erciyes (UCI 455, CC BY 4.0),
right wrist; per fall type and activity. Nothing is changed after it.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "training"))

SHIPPED = (2.0, 0.95)
GRID = [(g, t) for g in (2.0, 1.8, 1.6, 1.4) for t in (0.95, 0.9, 0.8)]


def run_all(recordings, spec, infer, cg, th):
    from fall_synthetic_eval import run_pipeline
    s = copy.deepcopy(spec)
    s["candidateG"], s["threshold"] = cg, th
    return [len(run_pipeline(acc, s, infer)) for acc in recordings]


def choose(rows):
    """The development rule, fixed before any held-out data is read: among settings that lose no
    fall the shipped setting catches on BITS-2, and add no more than 2 alarms per hour of daily
    activity over the shipped setting (BITS-2 activities and UCI HAR together), the one that catches
    the most BITS-2 falls; ties go to the higher candidateG, then the higher threshold."""
    ship = next(r for r in rows if (r["candidate_g"], r["threshold"]) == SHIPPED)
    ok = [r for r in rows if r["falls_lost_vs_shipped"] == 0
          and r["alarms_per_hour"] <= ship["alarms_per_hour"] + 2.0]
    return max(ok, key=lambda r: (r["falls_caught"], r["candidate_g"], r["threshold"]))


def dev(a) -> int:
    import fall_model_v2 as v2
    from fall_synthetic_eval import load_shipped_litert
    a.data_dir.mkdir(parents=True, exist_ok=True)
    bits2 = v2.fetch(v2.BITS2_URL, str(a.data_dir / "bits2_full_dataset.zip"), v2.BITS2_SHA256)
    har = v2.fetch(v2.HAR_URL, str(a.data_dir / "uci_har.zip"), v2.HAR_SHA256)
    spec, infer, model_info = load_shipped_litert()
    falls, adl = [], []
    for r in v2.load_bits2(bits2):
        if r["subject"] in v2.EXCLUDED_SUBJECTS:
            continue
        (falls if r["kind"] == "fall" else adl).append(r["acc"])
    har_segs = [s["acc"] for s in v2.load_har(har)]
    hours = (sum(len(x) for x in adl) + sum(len(x) for x in har_segs)) / 50 / 3600
    t0 = time.time()
    ship_falls = run_all(falls, spec, infer, *SHIPPED)
    rows = []
    for cg, th in GRID:
        fr = ship_falls if (cg, th) == SHIPPED else run_all(falls, spec, infer, cg, th)
        alarms = sum(run_all(adl, spec, infer, cg, th)) + sum(run_all(har_segs, spec, infer, cg, th))
        rows.append({"candidate_g": cg, "threshold": th, "falls": len(falls),
                     "falls_caught": sum(1 for n in fr if n > 0),
                     "falls_lost_vs_shipped": sum(1 for a_, b_ in zip(ship_falls, fr) if a_ > 0 and b_ == 0),
                     "alarms": alarms, "alarms_per_hour": round(alarms / hours, 3)})
        print(json.dumps(rows[-1]), flush=True)
    chosen = choose(rows)
    out = {"what": "low-impact candidate, development on BITS-2 and UCI HAR (D-115)", "model": model_info,
           "daily_activity_hours": round(hours, 3), "grid": rows, "chosen": chosen,
           "rule": choose.__doc__.strip(), "seconds": round(time.time() - t0, 1)}
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    print("chosen:", json.dumps(chosen))
    return 0


def heldout(a) -> int:
    import collections
    from fall_erciyes_eval import read_wrist, recordings, to_watch
    from fall_synthetic_eval import load_shipped_litert, run_pipeline
    spec, infer, model_info = load_shipped_litert()
    settings = {"shipped": SHIPPED, "candidate": (a.candidate_g, a.threshold)}
    per = {k: collections.defaultdict(lambda: [0, 0]) for k in settings}
    totals = {k: {"falls": 0, "caught": 0, "adl": 0, "adl_alarms": 0} for k in settings}
    lost = []
    for subject, code, activity, test, f in recordings(a.erciyes_dir):
        acc = read_wrist(f)
        if acc is None:
            continue
        xyz = to_watch(acc)
        got = {}
        for k, (cg, th) in settings.items():
            s = copy.deepcopy(spec)
            s["candidateG"], s["threshold"] = cg, th
            n = len(run_pipeline(xyz, s, infer))
            got[k] = n
            key = (code, activity.split("-", 1)[1] if "-" in activity else activity)
            per[k][key][0] += 1
            per[k][key][1] += n > 0
            if code >= 900:
                totals[k]["falls"] += 1
                totals[k]["caught"] += n > 0
            else:
                totals[k]["adl"] += 1
                totals[k]["adl_alarms"] += n > 0
        if code >= 900 and got["shipped"] > 0 and got["candidate"] == 0:
            lost.append(f"{subject}/{code}/{test}")
    out = {"what": "the shipped setting and the development-chosen candidate, once, on Erciyes (held out)",
           "settings": {k: {"candidate_g": v[0], "threshold": v[1]} for k, v in settings.items()},
           "totals": totals, "falls_the_candidate_loses": lost,
           "per_type": [{"code": c, "activity": n, "recordings": per["shipped"][(c, n)][0],
                         "shipped": per["shipped"][(c, n)][1], "candidate": per["candidate"][(c, n)][1]}
                        for (c, n) in sorted(per["shipped"])],
           "licence": "Erciyes: CC BY 4.0 (UCI 455, doi:10.24432/C52028), A.T. Özdemir and B. Barshan"}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({"totals": totals, "lost": len(lost)}, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("dev")
    d.add_argument("--data-dir", required=True, type=Path)
    d.add_argument("--out", required=True, type=Path)
    h = sub.add_parser("heldout")
    h.add_argument("--erciyes-dir", required=True, type=Path)
    h.add_argument("--candidate-g", required=True, type=float)
    h.add_argument("--threshold", required=True, type=float)
    h.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    for p in (getattr(a, "data_dir", None), getattr(a, "erciyes_dir", None)):
        if p is not None and (ROOT in p.resolve().parents or p.resolve() == ROOT):
            raise SystemExit("data must be outside the repository: raw data is never committed")
    return dev(a) if a.mode == "dev" else heldout(a)


if __name__ == "__main__":
    raise SystemExit(main())
