#!/usr/bin/env python3
"""Drive one care flow over HTTPS through the deployed containers (Caddy in front).

    python scripts/staging/containers_smoke.py [--base https://localhost] [--out result.json]

Signs in as the seeded coordinator (scripts/demo/seed_demo.py writes .demo/demo.local.json),
creates a fictional person, registers and pairs a watch, sends simulated readings, presses
Help twice with the same idempotency key (one alert, the second answer replayed), sees the
alert on the carer's list, acknowledges and resolves it. Messages go to the log-only provider:
nothing leaves the machine. Prints and writes each step with its status and time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    demo = json.loads((ROOT / ".demo/demo.local.json").read_text())
    coord = next(l for l in demo["logins"] if l["key"] == "coordinator")
    steps: list[dict] = []
    c = httpx.Client(base_url=args.base, verify=False, timeout=20, trust_env=False)

    def step(name, fn):
        t = time.perf_counter()
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "ms": round((time.perf_counter() - t) * 1000), "detail": detail})
            return detail
        except Exception as exc:  # noqa: BLE001 - every failure is reported, then the run stops
            steps.append({"step": name, "ok": False, "ms": round((time.perf_counter() - t) * 1000), "detail": f"{type(exc).__name__}: {exc}"[:300]})
            raise

    def ok(r: httpx.Response, *codes: int) -> httpx.Response:
        if r.status_code not in (codes or (200, 201)):
            raise RuntimeError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:200]}")
        return r

    try:
        step("health", lambda: ok(c.get("/health")).json())
        login = step("sign in (coordinator)", lambda: ok(c.post("/auth/cloud/login", data={"email": coord["email"], "password": coord["password"]})).json())
        h = {"Authorization": f"Bearer {login['access_token']}", "X-Workspace-ID": demo["workspace_id"]}
        person = step("create a fictional person", lambda: ok(c.post("/api/v1/care/people", headers=h, json={"display_name": f"Staging Test {uuid.uuid4().hex[:6]} (fictional)", "preferred_name": "Test"}), 201).json())
        pid = person["id"]
        dev = step("register a watch", lambda: ok(c.post(f"/api/v1/care/people/{pid}/devices", headers=h, json={"platform": "wearos", "model": "staging virtual watch"}), 201).json())
        paired = step("pair the watch", lambda: ok(c.post("/api/v1/care/devices/pair", json={"pairing_code": dev["pairing_code"], "app_version": "staging"})).json())
        dh = {"X-Device-Token": paired["device_token"]}
        now = dt.datetime.now(dt.UTC)
        readings = [{"kind": "heart_rate", "value": 72 + i, "unit": "bpm", "recorded_at": (now - dt.timedelta(minutes=5 - i)).isoformat()} for i in range(5)]
        step("send simulated readings", lambda: ok(c.post("/api/v1/care/devices/me/report", headers=dh, json={"readings": readings, "battery_percent": 80, "on_wrist": True})).json() and "ok")
        key = str(uuid.uuid4())
        first = step("press Help", lambda: ok(c.post("/api/v1/care/devices/me/help", headers={**dh, "X-Idempotency-Key": key}, json={"via": "button"}), 201).json())
        def again():
            r = ok(c.post("/api/v1/care/devices/me/help", headers={**dh, "X-Idempotency-Key": key}, json={"via": "button"}), 200, 201)
            if r.headers.get("X-Idempotent-Replay") != "true":
                raise RuntimeError("the repeat was not a replay")
            return {"replayed": True, "same_answer": r.json() == first}
        step("press Help again with the same key (a retry)", again)
        def listed():
            alerts = ok(c.get("/api/v1/care/alerts", headers=h, params={"open_only": "true", "limit": 200})).json()["alerts"]
            mine = [a for a in alerts if a["person_id"] == pid]
            if len(mine) != 1:
                raise RuntimeError(f"{len(mine)} open alerts for the person, expected exactly 1")
            return {"open_alerts_for_person": 1, "kind": mine[0]["kind"], "severity": mine[0]["severity"], "id": mine[0]["id"]}
        alert = step("the alert is on the carer's list, once", listed)
        step("acknowledge", lambda: ok(c.post(f"/api/v1/care/alerts/{alert['id']}/acknowledge", headers=h, json={"kind": "on_my_way", "note": "Staging check."})).json()["status"])
        step("resolve", lambda: ok(c.post(f"/api/v1/care/alerts/{alert['id']}/resolve", headers=h, json={"resolution": "Staging check.", "outcome": "all_well"})).json()["status"])
        step("operations status", lambda: {k: v for k, v in ok(c.get("/api/v1/care/ops/status", headers=h)).json().items() if k in ("monitoring_paused", "delivery")})
        step("dashboard served", lambda: ok(c.get("/login")).status_code)
    except Exception:  # noqa: BLE001
        pass
    for s in steps:   # never write a token, pairing code or session into the evidence
        if s["ok"] and isinstance(s["detail"], dict):
            s["detail"] = {k: v for k, v in s["detail"].items() if not any(w in k for w in ("token", "pairing_code", "password"))}
        if s["step"].startswith("sign in") and s["ok"]:
            s["detail"] = "ok"
    result = {"base": args.base, "passed": all(s["ok"] for s in steps), "steps": steps}
    for s in steps:
        print(f"{'ok  ' if s['ok'] else 'FAIL'} {s['ms']:>5} ms  {s['step']}  {'' if s['ok'] else s['detail']}")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
