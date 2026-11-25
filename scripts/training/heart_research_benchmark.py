# IRA CARE: IRA Care Heart research pipeline, processing time and memory per window.
#
#   python3 -I scripts/training/heart_research_benchmark.py --out docs/ai/heart/research_benchmark.json
#
# Runs care/heart_research.py on the in-repository PPG-DaLiA excerpt and on the simulated
# samples, with the learned model and with the classical estimator only, and records:
#   - per-window processing time (each window timed by the pipeline itself), mean, median,
#     95th percentile and maximum, over every window of every repeat;
#   - loading the learned model;
#   - memory: the Python allocator's peak during one run (tracemalloc, in runs separate from the
#     timed ones, because tracing slows allocation-heavy code several-fold) and the process's
#     maximum resident set size before and after (resource.getrusage).
# No dataset download: the excerpt is in the repository. The machine is shared, so the
# numbers are what this machine did under its load at the time, not a guarantee.
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
from care import heart_learned as hl  # noqa: E402
from care import heart_research as hr  # noqa: E402


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "ai" / "heart" / "research_benchmark.json")
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()
    rss_start = rss_mib()
    t0 = time.perf_counter()
    hl.read_model(hl.DEFAULT_MODEL_PATH)
    load_ms = 1000 * (time.perf_counter() - t0)

    samples = ["ppg_dalia_s1_excerpt", *hr.SIMULATED]
    recs = {n: hr.load_sample(n) for n in samples}
    hr.run(recs["simulated_rest"])            # warm-up: imports, model cache, numpy plans
    out = {}
    for mode, use_learned in (("learned_with_classical_fallback", True), ("classical_only", False)):
        per_window, totals, peaks, n_windows = [], [], [], 0
        for _ in range(args.repeats):          # timing, without tracemalloc (it slows allocation-heavy code)
            for n in samples:
                t = time.perf_counter()
                res = hr.run(recs[n], use_learned=use_learned)
                totals.append(time.perf_counter() - t)
                per_window += [w.processing_ms for w in res.windows]
                n_windows += len(res.windows)
        for n in samples:                      # memory, in separate runs
            tracemalloc.start()
            hr.run(recs[n], use_learned=use_learned)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
        pw = np.array(per_window)
        out[mode] = {
            "windows_timed": int(len(pw)),
            "per_window_ms": {"mean": round(float(pw.mean()), 3), "median": round(float(np.median(pw)), 3),
                              "p95": round(float(np.percentile(pw, 95)), 3), "max": round(float(pw.max()), 3)},
            "python_peak_alloc_mib_per_run_max": round(max(peaks) / (1024 * 1024), 3),
            "seconds_total": round(sum(totals), 2),
        }
    doc = {
        "what": "care/heart_research.py, per-window processing time and memory",
        "samples": {n: {"windows": len(hr.window_starts(r)), "source": r.source} for n, r in recs.items()},
        "repeats": args.repeats,
        "model_load_ms": round(load_ms, 1),
        "results": out,
        "process_max_rss_mib": {"at_start": round(rss_start, 1), "at_end": round(rss_mib(), 1)},
        "note": "per-window time includes slicing, the quality checks, both estimators (the learned tracker runs the "
                "classical one inside it) and building the result; the windows arrive every 2 s",
        "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, no GPU, shared with other jobs",
        "versions": {"python": platform.python_version(), "numpy": np.__version__},
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(doc, indent=1))


if __name__ == "__main__":
    sys.exit(main())
