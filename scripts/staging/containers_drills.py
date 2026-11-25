#!/usr/bin/env python3
"""Outage drills against the deployed containers (compose project given by --project).

    python scripts/staging/containers_drills.py [--project irastage] [--only db,api,worker] [--out drills.json]

  db      stop the database container for 20 s: the server must answer with clean errors
          (never hang), and recover by itself when the database is back.
  api     five watches press Help (each with its own idempotency key, retried with the SAME
          key on any failure, as the watch's outbox does) while the API container restarts:
          every press must end answered, and each person must have exactly one open alert.
  worker  stop the worker container: /health/jobs and the carers' operations status must say
          monitoring has paused (within the 5-minute staleness window plus one pass), and
          recover once it is started again. Takes about 7 minutes.

Uses the seeded coordinator from .demo/demo.local.json; fictional people only; messages go
to the log-only provider.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://localhost"


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, verify=False, timeout=10, trust_env=False)


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def code(c: httpx.Client, path: str, **kw) -> str:
    try:
        return str(c.get(path, **kw).status_code)
    except httpx.HTTPError as exc:
        return type(exc).__name__


def wait_for(c: httpx.Client, path: str, want: str, limit_s: float, **kw) -> float | None:
    t = time.monotonic()
    while time.monotonic() - t < limit_s:
        if code(c, path, **kw) == want:
            return round(time.monotonic() - t, 1)
        time.sleep(1)
    return None


def drill_db(project: str, h: dict) -> dict:
    c = client()
    seen_health, seen_api = {}, {}
    docker("stop", f"{project}-postgres-1")
    t0 = time.monotonic()
    worst = 0.0
    while time.monotonic() - t0 < 20:
        s = time.perf_counter()
        a, b = code(c, "/health"), code(c, "/api/v1/care/people", headers=h)
        worst = max(worst, time.perf_counter() - s)
        seen_health[a] = seen_health.get(a, 0) + 1
        seen_api[b] = seen_api.get(b, 0) + 1
        time.sleep(1)
    docker("start", f"{project}-postgres-1")
    back = wait_for(c, "/api/v1/care/people", "200", 120, headers=h)
    return {"health_codes_while_down": seen_health, "care_api_codes_while_down": seen_api,
            "slowest_answer_while_down_s": round(worst, 2), "care_api_back_after_start_s": back,
            "passed": back is not None and "200" not in seen_api and all(k in ("503", "500", "502") for k in seen_api)}


def drill_api(project: str, h: dict) -> dict:
    c = client()
    watches = []
    for i in range(5):
        p = c.post("/api/v1/care/people", headers=h, json={"display_name": f"Drill Watch {i} {uuid.uuid4().hex[:4]} (fictional)"}).json()
        d = c.post(f"/api/v1/care/people/{p['id']}/devices", headers=h, json={"platform": "wearos"}).json()
        tok = c.post("/api/v1/care/devices/pair", json={"pairing_code": d["pairing_code"]}).json()["device_token"]
        watches.append({"person": p["id"], "token": tok, "key": str(uuid.uuid4()), "attempts": 0, "answer": None, "errors": {}})

    def press(w):
        wc = client()
        end = time.monotonic() + 120
        while time.monotonic() < end:
            w["attempts"] += 1
            try:
                r = wc.post("/api/v1/care/devices/me/help", headers={"X-Device-Token": w["token"], "X-Idempotency-Key": w["key"]}, json={"via": "button"})
                if r.status_code in (200, 201):
                    w["answer"] = r.status_code
                    return
                w["errors"][str(r.status_code)] = w["errors"].get(str(r.status_code), 0) + 1
            except httpx.HTTPError as exc:
                w["errors"][type(exc).__name__] = w["errors"].get(type(exc).__name__, 0) + 1
            time.sleep(0.5)

    restart = threading.Thread(target=lambda: docker("restart", f"{project}-api-1"))
    restart.start()
    time.sleep(0.3)   # the presses start while the server is going down
    threads = [threading.Thread(target=press, args=(w,)) for w in watches]
    [t.start() for t in threads]
    [t.join() for t in threads]
    restart.join()
    wait_for(c, "/health", "200", 60)
    alerts = c.get("/api/v1/care/alerts", headers=h, params={"open_only": "true", "limit": 500}).json()["alerts"]
    per = {w["person"]: sum(1 for a in alerts if a["person_id"] == w["person"]) for w in watches}
    for a in alerts:
        if a["person_id"] in per:
            c.post(f"/api/v1/care/alerts/{a['id']}/resolve", headers=h, json={"resolution": "Staging drill.", "outcome": "all_well"})
    return {"watches": [{k: w[k] for k in ("attempts", "answer", "errors")} for w in watches],
            "open_alerts_per_person": list(per.values()),
            "passed": all(w["answer"] for w in watches) and all(n == 1 for n in per.values())}


def drill_worker(project: str, h: dict) -> dict:
    c = client()
    before = c.get("/health/jobs").json()
    docker("stop", f"{project}-worker-1")
    paused_after = wait_for(c, "/health/jobs", "503", 420)
    ops = c.get("/api/v1/care/ops/status", headers=h).json() if paused_after is not None else {}
    docker("start", f"{project}-worker-1")
    back_after = wait_for(c, "/health/jobs", "200", 180)
    return {"monitoring_paused_before": before.get("monitoring_paused"), "stale_after_s": before.get("stale_after_s"),
            "jobs_reported_stale_after_stop_s": paused_after, "carer_ops_status_monitoring_paused": ops.get("monitoring_paused"),
            "jobs_healthy_after_start_s": back_after,
            "passed": paused_after is not None and ops.get("monitoring_paused") is True and back_after is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="irastage")
    ap.add_argument("--only", default="db,api,worker")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    demo = json.loads((ROOT / ".demo/demo.local.json").read_text())
    coord = next(l for l in demo["logins"] if l["key"] == "coordinator")
    login = client().post("/auth/cloud/login", data={"email": coord["email"], "password": coord["password"]}).json()
    h = {"Authorization": f"Bearer {login['access_token']}", "X-Workspace-ID": demo["workspace_id"]}
    out = {"started_at": dt.datetime.now(dt.UTC).isoformat()}
    for name in args.only.split(","):
        t = time.monotonic()
        out[name] = {**{"db": drill_db, "api": drill_api, "worker": drill_worker}[name](args.project, h), "seconds": round(time.monotonic() - t)}
        print(name, json.dumps(out[name]))
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    return 0 if all(out[n]["passed"] for n in args.only.split(",")) else 1


if __name__ == "__main__":
    sys.exit(main())
