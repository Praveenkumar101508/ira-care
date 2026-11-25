#!/usr/bin/env python3
"""Run the 22-step care journey (tests/tenancy/test_care_journey_db.py) against the product's
REAL model chain, several times, and write the evidence (requirement J.e).

The model is whatever the deployment is configured with: Ollama at IRA_CARE_OLLAMA_URL, with
IRA_CARE_MODEL_PRIMARY then IRA_CARE_MODEL_SECONDARY (default gemma4:e4b then gemma4:e2b), under
the product's own time budgets. Fictional people, a recording SMS provider and a fake push service:
nothing leaves the machine. Needs a migrated PostgreSQL in IRA_TEST_ADMIN_DSN / IRA_TEST_DATABASE_DSN
that is NOT a shared test database.

    python3 scripts/demo/real_model_journey.py --runs 8 --out docs/evidence/demo/real_model_run.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IRA = ROOT / "supracloud-jarvis" / "ira"


def ollama_models(url: str) -> list[dict]:
    with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:  # noqa: S310 - local model server
        return [{"name": m["name"], "digest": m["digest"], "size": m["size"]} for m in json.load(r)["models"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    url = os.getenv("IRA_CARE_OLLAMA_URL", "http://127.0.0.1:11434")
    env = {**os.environ, "IRA_JOURNEY_MODEL": "real", "IRA_CARE_MODEL": "ollama", "IRA_CARE_OLLAMA_URL": url}
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(1, a.runs + 1):
            ev = Path(tmp) / f"run{i}.json"
            env["IRA_JOURNEY_EVIDENCE"] = str(ev)
            started = time.monotonic()
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                "tests/tenancy/test_care_journey_db.py"], cwd=IRA, env=env,
                               capture_output=True, text=True)
            took = round(time.monotonic() - started, 1)
            failed = [line[2:].strip() for line in p.stdout.splitlines() if line.startswith("E ")][:3]
            data = json.loads(ev.read_text()) if ev.exists() else {}
            runs.append({"run": i, "passed": p.returncode == 0, "seconds": took, "failure": failed,
                         **data})
            print(f"run {i}: {'passed' if p.returncode == 0 else 'FAILED'} in {took} s", flush=True)

    calls = [c for r in runs for c in r.get("calls", [])]
    by_kind = {}
    for c in calls:
        k = by_kind.setdefault(c["kind"], {"n": 0, "total_s": [], "first_token_s": []})
        k["n"] += 1
        k["total_s"].append(c["total_s"])
        if c["first_token_s"] is not None:
            k["first_token_s"].append(c["first_token_s"])
    summary = {k: {"calls": v["n"],
                   "total_s_median": round(statistics.median(v["total_s"]), 2),
                   "total_s_max": round(max(v["total_s"]), 2),
                   "first_token_s_median": round(statistics.median(v["first_token_s"]), 2) if v["first_token_s"] else None}
               for k, v in sorted(by_kind.items())}
    sources = [s for r in runs for s in r.get("step5_question_source", [])]
    out = {
        "what": "The 22-step care journey against the product's real model chain (requirement J.e)",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"platform": platform.platform(), "cpus": os.cpu_count(), "gpu": "none"},
        "model_server": url,
        "models": ollama_models(url),
        "chain": [os.getenv("IRA_CARE_MODEL_PRIMARY") or "gemma4:e4b", os.getenv("IRA_CARE_MODEL_SECONDARY") or "gemma4:e2b"],
        "runs": len(runs),
        "passed": sum(r["passed"] for r in runs),
        "step5_question_source": {"model": sources.count("model"), "template": sources.count("template")},
        "latency_by_call_kind": summary,
        "per_run": runs,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("runs", "passed", "step5_question_source", "latency_by_call_kind")}, indent=1))
    return 0 if out["passed"] == out["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
