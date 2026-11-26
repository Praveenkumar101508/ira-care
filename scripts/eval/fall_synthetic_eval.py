#!/usr/bin/env python3
"""The shipped fall pipeline, with and without the confirmation stage, on the synthetic scenarios.

    python3 scripts/eval/fall_synthetic_eval.py --out docs/ai/fall/synthetic_confirmation.json [--seed 7 --per-kind 20]

What runs, unchanged: the watch's detectors as `fall_cross_weda.py` ports them (the rule detector
and `RecoveringFallDetector` with the shipped v3 model, threshold 0.95, candidate 2.0 g), the
shipped `fall_model.tflite` through LiteRT. Then `fall_confirmation.decide` on every suspected
fall the pipeline raises, with the samples around the impact, exactly as the watch would.

Scored per scenario kind: how many fall scenarios are caught (a suspected fall raised, then
confirmed), how many non-fall scenarios raise an alarm (baseline) and how many still do after the
confirmation stage, and the added delay before the person is asked. The synthetic set is a
regression corpus made from physical reasoning, never a measurement of real accuracy; the model
card says so, and the permitted recorded datasets are the test that matters (BITS-2 and UCI HAR
are not on this machine, so that run is left to a machine that has them:
`fall_confirmation_eval` note in docs/ai/fall/FALSE_ALARM_INVESTIGATION.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fall_confirmation as fc  # noqa: E402
import fall_synthetic_scenarios as fs  # noqa: E402
from fall_cross_weda import ASSETS, FallSettings, RecoveringFallDetector  # noqa: E402

HZ = 50


def load_shipped_litert():
    spec = json.loads((ASSETS / "fall_model.json").read_text())
    blob = (ASSETS / "fall_model.tflite").read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    if sha != spec["tfliteSha256"] or spec["mode"] != "recover" or spec["provenance"] != "ira-fall-v3-licensed":
        raise SystemExit("the model on disk is not the shipped v3 file")
    from ai_edge_litert.interpreter import Interpreter

    interp = Interpreter(model_content=blob, num_threads=1)
    interp.allocate_tensors()
    inp, outp = interp.get_input_details()[0], interp.get_output_details()[0]

    def infer(w):
        interp.set_tensor(inp["index"], w[None].astype(np.float32))
        interp.invoke()
        return np.float32(interp.get_tensor(outp["index"]).ravel()[0])

    return spec, infer, {"tflite_sha256": sha, "runtime": "ai-edge-litert"}


def run_pipeline(xyz: np.ndarray, spec, infer) -> list[dict]:
    """Every suspected fall the shipped pipeline raises on this recording (no countdown policy:
    each raise is listed; the countdown's own D-092 handling is upstream of what is measured here)."""
    det = RecoveringFallDetector(FallSettings(), spec, infer)
    raised = []
    for i, (x, y, z) in enumerate(xyz):
        at = i * 1000 // HZ
        ev = det.feed(at, float(x), float(y), float(z))
        if ev is not None and ev[0] == "fall":
            raised.append({"path": ev[4], "impact_ms": int(ev[1]), "peak_g": round(float(ev[2]), 3), "raised_ms": at})
    return raised


def confirm_all(xyz: np.ndarray, raised: list[dict]) -> list[dict]:
    t_s = np.arange(len(xyz)) / HZ
    out = []
    for r in raised:
        impact_s = r["impact_ms"] / 1000
        # the watch decides when the sample at impact + POST_TO_S arrives, or at the end of what it has
        decide_s = min(impact_s + fc.POST_TO_S, t_s[-1])
        d = fc.decide(t_s[t_s <= decide_s], xyz[t_s <= decide_s], impact_s)
        out.append({**r, "confirmed": d.confirm, "reason": d.reason, "orientation_change_deg": round(d.orientation_change_deg, 1),
                    "post_motion_sd_g": round(d.post_motion_sd_g, 3), "impacts_in_window": d.impacts_in_window,
                    "clipped": d.clipped, "asked_at_ms": int(max(r["raised_ms"], decide_s * 1000))})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--per-kind", type=int, default=20)
    a = ap.parse_args(argv)
    spec, infer, model_info = load_shipped_litert()
    scenarios = fs.generate(a.seed, a.per_kind)
    t0 = time.time()
    per_kind: dict[str, dict] = {}
    details = []
    for s in scenarios:
        raised = run_pipeline(s["xyz"], spec, infer)
        judged = confirm_all(s["xyz"], raised)
        kept = [j for j in judged if j["confirmed"]]
        k = per_kind.setdefault(s["kind"], {"label": s["label"], "scenarios": 0, "seconds": 0.0,
                                            "baseline_alarmed": 0, "confirmed_alarmed": 0, "baseline_alarms": 0,
                                            "confirmed_alarms": 0, "paths": {"rule": 0, "model": 0}, "added_delay_ms": []})
        k["scenarios"] += 1
        k["seconds"] += len(s["xyz"]) / HZ
        k["baseline_alarmed"] += bool(raised)
        k["confirmed_alarmed"] += bool(kept)
        k["baseline_alarms"] += len(raised)
        k["confirmed_alarms"] += len(kept)
        for j in judged:
            k["paths"][j["path"]] += 1
        for j in kept:
            k["added_delay_ms"].append(j["asked_at_ms"] - j["raised_ms"])
        details.append({"id": s["id"], "label": s["label"], "kind": s["kind"], "params": s["params"], "raised": judged})
    for k in per_kind.values():
        delays = k.pop("added_delay_ms")
        k["added_delay_ms_median"] = int(np.median(delays)) if delays else None
        k["added_delay_ms_max"] = int(max(delays)) if delays else None
        hours = k["seconds"] / 3600
        k["baseline_alarms_per_hour"] = round(k["baseline_alarms"] / hours, 1) if hours else None
        k["confirmed_alarms_per_hour"] = round(k["confirmed_alarms"] / hours, 1) if hours else None
    falls = {n: v for n, v in per_kind.items() if v["label"] == "fall"}
    others = {n: v for n, v in per_kind.items() if v["label"] == "not_fall"}
    summary = {
        "falls": {"scenarios": sum(v["scenarios"] for v in falls.values()),
                  "caught_baseline": sum(v["baseline_alarmed"] for v in falls.values()),
                  "caught_with_confirmation": sum(v["confirmed_alarmed"] for v in falls.values())},
        "not_falls": {"scenarios": sum(v["scenarios"] for v in others.values()),
                      "hours": round(sum(v["seconds"] for v in others.values()) / 3600, 3),
                      "alarms_baseline": sum(v["baseline_alarms"] for v in others.values()),
                      "alarms_with_confirmation": sum(v["confirmed_alarms"] for v in others.values())},
    }
    h = summary["not_falls"]["hours"]
    summary["not_falls"]["alarms_per_hour_baseline"] = round(summary["not_falls"]["alarms_baseline"] / h, 1) if h else None
    summary["not_falls"]["alarms_per_hour_with_confirmation"] = round(summary["not_falls"]["alarms_with_confirmation"] / h, 1) if h else None
    out = {"what": "the shipped fall pipeline with and without the confirmation stage, on original synthetic scenarios (not real accuracy)",
           "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seed": a.seed, "per_kind": a.per_kind,
           "model": model_info, "confirmation_constants": {k: getattr(fc, k) for k in (
               "PRE_FROM_S", "PRE_TO_S", "POST_FROM_S", "POST_TO_S", "STILL_SD_G", "BUSY_SD_G", "SAME_TURN_DEG",
               "REPEAT_TURN_DEG", "REPEAT_MIN", "REPEAT_WINDOW_S", "REPEAT_GAP_S", "IMPACT_G", "CLIP_G")},
           "summary": summary, "per_kind": per_kind, "seconds_to_run": round(time.time() - t0, 1), "details": details}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({"summary": summary}, indent=1))
    print(f"{'kind':22} {'label':9} {'n':>3} {'base':>5} {'conf':>5} {'alarms/h base':>14} {'alarms/h conf':>14}")
    for n, v in per_kind.items():
        print(f"{n:22} {v['label']:9} {v['scenarios']:3} {v['baseline_alarmed']:5} {v['confirmed_alarmed']:5} "
              f"{str(v['baseline_alarms_per_hour']):>14} {str(v['confirmed_alarms_per_hour']):>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
