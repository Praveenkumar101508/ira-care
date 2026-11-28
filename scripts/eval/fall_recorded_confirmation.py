#!/usr/bin/env python3
"""The fall confirmation stage on the permitted recorded datasets (requirement F4.i, D-103's gate).

    python3 scripts/eval/fall_recorded_confirmation.py --data-dir /path/outside/the/repo \\
        --out docs/ai/fall/recorded_confirmation.json

Data: BITS-2 (Zenodo 10013090, CC BY 4.0) and UCI HAR (UCI 240, CC BY 4.0), fetched by
`scripts/training/fall_model_v2.fetch` with the SHA-256 the training scripts pin, into `--data-dir`
(never the repository). WEDA-FALL, SisFall, MobiFall and every other set without confirmed
commercial permission are not used.

What runs, unchanged: the shipped pipeline (`fall_synthetic_eval.run_pipeline`: the rule detector
and `RecoveringFallDetector` with the shipped `fall_model.tflite` through LiteRT) over every
BITS-2 recording and every continuous UCI HAR segment, at 50 Hz in g; then
`fall_synthetic_eval.confirm_all` (the confirmation stage, `fall_confirmation.decide`) on each
suspected fall the pipeline raises, with the samples the watch would have.

Scored:
  * falls: BITS-2 fall recordings in which the pipeline raises a suspected fall (before), and in
    which at least one raise is confirmed (after). **The gate: no fall the pipeline catches may be
    lost to the confirmation stage** (the model card's rule for any change to the shipped path).
  * alarms: suspected falls raised on BITS-2 daily activities and on UCI HAR, before and after.

The honest limits are written into the output: the shipped model was trained on all BITS-2
subjects, so its own catch rate here is in-sample and is not a sensitivity claim; the confirmation
stage was never fitted to BITS-2 (its thresholds come from physical reasoning, D-103), so what it
removes or keeps here is out-of-sample. BITS-2 recordings often stop soon after the fall, so some
decisions see less than the full 30 s afterwards; they are counted.
"""
from __future__ import annotations

import argparse
import collections
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "training"))
import fall_confirmation as fc  # noqa: E402
import fall_model_v2 as v2  # noqa: E402
from fall_synthetic_eval import confirm_all, load_shipped_litert, run_pipeline  # noqa: E402


def score_recording(acc: np.ndarray, spec, infer) -> dict:
    raised = run_pipeline(acc, spec, infer)
    decided = confirm_all(acc, raised)
    seconds = len(acc) / fc.HZ
    return {"raised": len(raised), "confirmed": sum(d["confirmed"] for d in decided),
            "decisions": [{k: d[k] for k in ("path", "peak_g", "confirmed", "reason", "orientation_change_deg",
                                              "post_motion_sd_g", "impacts_in_window", "clipped")}
                          | {"seconds_after_impact": round(seconds - d["impact_ms"] / 1000, 1)} for d in decided]}


def summarise(falls: list[dict], adl: list[dict], har: list[dict]) -> dict:
    caught_before = [f for f in falls if f["raised"] > 0]
    caught_after = [f for f in falls if f["confirmed"] > 0]
    lost = [f["id"] for f in caught_before if f["confirmed"] == 0]
    short = sum(1 for f in caught_before for d in f["decisions"] if d["seconds_after_impact"] < fc.POST_TO_S)
    reasons = collections.Counter(d["reason"] for r in falls + adl + har for d in r["decisions"])
    return {
        "falls": {"recordings": len(falls), "caught_before": len(caught_before), "caught_after": len(caught_after),
                  "lost_to_confirmation": len(lost), "lost_ids": lost,
                  "decisions_with_less_than_full_window_after": short},
        "bits2_daily_activity": {"recordings": len(adl), "alarms_before": sum(r["raised"] for r in adl),
                                 "alarms_after": sum(r["confirmed"] for r in adl),
                                 "hours": round(sum(r["seconds"] for r in adl) / 3600, 3)},
        "uci_har": {"segments": len(har), "alarms_before": sum(r["raised"] for r in har),
                    "alarms_after": sum(r["confirmed"] for r in har),
                    "hours": round(sum(r["seconds"] for r in har) / 3600, 3)},
        "reasons": dict(sorted(reasons.items())),
        "gate": {"rule": "no fall the shipped pipeline catches may be lost to the confirmation stage",
                 "passed": not lost},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, type=Path, help="outside the repository")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    if ROOT in a.data_dir.resolve().parents or a.data_dir.resolve() == ROOT:
        raise SystemExit("--data-dir must be outside the repository: raw data is never committed")
    a.data_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    bits2 = v2.fetch(v2.BITS2_URL, str(a.data_dir / "bits2_full_dataset.zip"), v2.BITS2_SHA256)
    har_zip = v2.fetch(v2.HAR_URL, str(a.data_dir / "uci_har.zip"), v2.HAR_SHA256)
    spec, infer, model_info = load_shipped_litert()

    falls, adl, har = [], [], []
    for r in v2.load_bits2(bits2):
        if r["subject"] in v2.EXCLUDED_SUBJECTS:
            continue
        rec = {"id": f"{r['subject']}_{r['kind']}{r['activity']}", "seconds": len(r["acc"]) / fc.HZ,
               **score_recording(r["acc"], spec, infer)}
        (falls if r["kind"] == "fall" else adl).append(rec)
    for i, s in enumerate(v2.load_har(har_zip)):
        har.append({"id": f"{s['subject']}_a{s['activity']}_{i}", "seconds": len(s["acc"]) / fc.HZ,
                    **score_recording(s["acc"], spec, infer)})

    out = {"what": "the fall confirmation stage on BITS-2 and UCI HAR, shipped pipeline unchanged",
           "data": {"bits2": {"source": v2.BITS2_URL, "sha256": v2.BITS2_SHA256, "licence": "CC BY 4.0",
                              "excluded_subjects": sorted(v2.EXCLUDED_SUBJECTS)},
                    "uci_har": {"source": v2.HAR_URL, "sha256": v2.HAR_SHA256, "licence": "CC BY 4.0"}},
           "model": model_info,
           "limits": ["the shipped model was trained on all BITS-2 subjects: its catch rate here is in-sample, not a sensitivity claim",
                      "the confirmation stage was not fitted to BITS-2 or UCI HAR: what it keeps or removes here is out-of-sample",
                      "BITS-2 recordings often end soon after the fall: such decisions see less than 30 s afterwards and are counted",
                      "UCI HAR is a waist-worn phone, not a wrist, and holds no falls: it measures alarms only"],
           "summary": summarise(falls, adl, har),
           "machine": f"{platform.machine()}, {platform.system()} {platform.release()}",
           "seconds_to_run": round(time.time() - t0, 1),
           "falls": falls, "bits2_daily_activity": adl,
           "uci_har": [h for h in har if h["raised"]]}
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out["summary"], indent=1))
    return 0 if out["summary"]["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
