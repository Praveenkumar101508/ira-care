#!/usr/bin/env python3
"""Load test: simulated watches against the deployed containers, through Caddy (HTTPS).

    python scripts/staging/containers_load.py [--levels 10,25,50] [--seconds 30] [--out load.json]

Each virtual watch is a real paired device of a fictional person. In a loop it sends a report
of five simulated heart-rate readings (what a watch sends every few minutes, here back to
back); once per level it presses Help with its own idempotency key. Reports requests per
second, latency percentiles and errors per level. One shared 4-CPU machine running every
container and the load generator: a regression baseline, not a production capacity figure.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://localhost"


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] * 1000, 1)


async def make_watches(n: int) -> list[str]:
    demo = json.loads((ROOT / ".demo/demo.local.json").read_text())
    coord = next(l for l in demo["logins"] if l["key"] == "coordinator")
    async with httpx.AsyncClient(base_url=BASE, verify=False, timeout=30, trust_env=False) as c:
        login = (await c.post("/auth/cloud/login", data={"email": coord["email"], "password": coord["password"]})).json()
        h = {"Authorization": f"Bearer {login['access_token']}", "X-Workspace-ID": demo["workspace_id"]}
        tokens = []
        for i in range(n):
            p = (await c.post("/api/v1/care/people", headers=h, json={"display_name": f"Load Watch {i:03d} (fictional)"})).json()
            d = (await c.post(f"/api/v1/care/people/{p['id']}/devices", headers=h, json={"platform": "wearos"})).json()
            r = await c.post("/api/v1/care/devices/pair", json={"pairing_code": d["pairing_code"]})
            while r.status_code == 429:   # pairing may be rationed per address: wait it out
                await asyncio.sleep(61)
                r = await c.post("/api/v1/care/devices/pair", json={"pairing_code": d["pairing_code"]})
            r.raise_for_status()
            tokens.append(r.json()["device_token"])
        return tokens


async def level(tokens: list[str], seconds: float) -> dict:
    lat: dict[str, list[float]] = {"report": [], "help": []}
    errors: dict[str, int] = {}
    limits = httpx.Limits(max_connections=len(tokens), max_keepalive_connections=len(tokens))
    async with httpx.AsyncClient(base_url=BASE, verify=False, timeout=30, trust_env=False, limits=limits) as c:
        async def watch(tok: str):
            end = time.monotonic() + seconds
            helped = False
            while time.monotonic() < end:
                now = dt.datetime.now(dt.UTC)
                if not helped and time.monotonic() > end - seconds / 2:
                    kind, call = "help", c.post("/api/v1/care/devices/me/help", headers={"X-Device-Token": tok, "X-Idempotency-Key": str(uuid.uuid4())}, json={"via": "button"})
                    helped = True
                else:
                    body = {"readings": [{"kind": "heart_rate", "value": 70 + i, "unit": "bpm", "recorded_at": (now - dt.timedelta(seconds=10 * i)).isoformat()} for i in range(5)], "battery_percent": 80, "on_wrist": True}
                    kind, call = "report", c.post("/api/v1/care/devices/me/report", headers={"X-Device-Token": tok}, json=body)
                t = time.perf_counter()
                try:
                    r = await call
                    ok = r.status_code in (200, 201)
                    key = None if ok else str(r.status_code)
                except httpx.HTTPError as exc:
                    ok, key = False, type(exc).__name__
                lat[kind].append(time.perf_counter() - t)
                if not ok:
                    errors[f"{kind}:{key}"] = errors.get(f"{kind}:{key}", 0) + 1
        t0 = time.monotonic()
        await asyncio.gather(*(watch(t) for t in tokens))
        wall = time.monotonic() - t0
    total = len(lat["report"]) + len(lat["help"])
    return {"virtual_watches": len(tokens), "seconds": round(wall, 1), "requests": total,
            "requests_per_s": round(total / wall, 1), "errors": errors,
            "error_rate": round(sum(errors.values()) / total, 4) if total else None,
            "report_ms": {"p50": pct(lat["report"], 50), "p95": pct(lat["report"], 95), "p99": pct(lat["report"], 99),
                          "mean": round(statistics.mean(lat["report"]) * 1000, 1) if lat["report"] else None},
            "help_ms": {"n": len(lat["help"]), "p50": pct(lat["help"], 50), "p95": pct(lat["help"], 95), "max": pct(lat["help"], 100)}}


async def run(levels: list[int], seconds: float) -> dict:
    tokens = await make_watches(max(levels))
    return {"levels": [await level(tokens[:n], seconds) for n in levels]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="10,25,50")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out = asyncio.run(run([int(x) for x in args.levels.split(",")], args.seconds))
    for lv in out["levels"]:
        print(json.dumps(lv))
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
