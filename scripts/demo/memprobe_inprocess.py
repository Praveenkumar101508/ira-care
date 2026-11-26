#!/usr/bin/env python3
"""Drive the demo API in-process with the wall-clock soak's request mix, as fast
as it will go, and record memory against the number of requests served.

    IRA_TEST_DATABASE_DSN=... IRA_TEST_ADMIN_DSN=... POSTGRES_DB=... \\
        python3 scripts/demo/memprobe_inprocess.py --credentials <seed.local.json> \\
        --rounds 6000 --every 500 --out probe.jsonl

A debugging aid for the soak's RSS criterion, not a test and not evidence of a
soak: it compresses many hours of the soak's per-request work into minutes, so
growth that comes from serving requests (a cache keyed by something that keeps
changing, a list that is never trimmed) shows up as growth per request, while
growth that comes from wall-clock time (the worker's schedule) does not. The
mix per person and round is the soak's: a report with two readings, the
check-in poll, the answer when one is due; every `--ask-every` rounds a family
check-in request; every `--help-every` rounds Help, its replay with the same
idempotency key, acknowledge and resolve. The app is the demo API's
(`scripts/demo/api.py`, the same routers and middleware) through
`httpx.ASGITransport`; the care worker jobs run in the same process as they do
there. Fictional people only; message providers stay log-only.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import json
import random
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--credentials", required=True, help="seed-format JSON written by seed_demo.py")
    ap.add_argument("--rounds", type=int, default=6000, help="rounds per person")
    ap.add_argument("--every", type=int, default=500, help="record memory every N rounds")
    ap.add_argument("--ask-every", type=int, default=60)
    ap.add_argument("--help-every", type=int, default=60)
    ap.add_argument("--pause-ms", type=float, default=0.0, help="sleep between rounds (keeps CPU use modest)")
    ap.add_argument("--out", required=True)
    return ap.parse_args(argv)


async def main(a) -> None:
    import httpx
    import psutil
    from demo import (
        api as demo_api,  # prepares the environment exactly as the demo API does
    )
    from demo.memtrace import malloc_stats, pymalloc_stats, smaps_rollup

    creds = json.loads(Path(a.credentials).read_text())
    app = demo_api.app
    proc = psutil.Process()
    out = Path(a.out)
    counts = {"requests": 0, "status": {}}
    rng = random.Random(7)

    async def call(client, method, path, **kw):
        r = await client.request(method, path, **kw)
        counts["requests"] += 1
        counts["status"][str(r.status_code)] = counts["status"].get(str(r.status_code), 0) + 1
        return r

    def record(label: str, rounds_done: int) -> None:
        gc.collect()
        rec = {"label": label, "rounds": rounds_done, "requests": counts["requests"], "wall": time.time(),
               "rss_bytes": proc.memory_info().rss, "malloc": malloc_stats(), "pymalloc": pymalloc_stats(),
               "smaps": smaps_rollup(), "gc_objects": len(gc.get_objects()), "status": dict(counts["status"])}
        with out.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"{label:>8} rounds={rounds_done:6d} req={counts['requests']:7d} rss={rec['rss_bytes'] / 2**20:7.1f} MB "
              f"malloc_in_use={rec['malloc']['in_use'] / 2**20 if rec['malloc'] else -1:6.1f} MB objs={rec['gc_objects']}",
              flush=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
            fam = next(l for l in creds["logins"] if l["key"] == "family")
            r = await call(client, "POST", "/auth/cloud/login", data={"email": fam["email"], "password": fam["password"]})
            r.raise_for_status()
            fh = {"Authorization": f"Bearer {r.json()['access_token']}", "X-Workspace-ID": creds["workspace_id"]}
            login_at = time.time()
            api = "/api/v1/care"

            async def person(p, i: int) -> None:
                nonlocal fh, login_at
                dev = {"X-Device-Token": p["device_token"]}
                if time.time() - login_at > 20 * 60:   # family tokens last 30 minutes
                    r = await call(client, "POST", "/auth/cloud/login", data={"email": fam["email"], "password": fam["password"]})
                    fh = {"Authorization": f"Bearer {r.json()['access_token']}", "X-Workspace-ID": creds["workspace_id"]}
                    login_at = time.time()
                stamp = dt.datetime.now(dt.UTC).isoformat()
                await call(client, "POST", f"{api}/devices/me/report", headers=dev, json={
                    "readings": [{"kind": "heart_rate", "value": round(rng.uniform(62, 88), 1), "unit": "bpm", "recorded_at": stamp},
                                 {"kind": "steps", "value": float(rng.randint(5, 60)), "unit": "", "recorded_at": stamp}],
                    "battery_percent": 80, "on_wrist": True, "background_sensor_permission": True})
                await call(client, "GET", "/health")
                if i % a.ask_every == 0:
                    await call(client, "POST", f"{api}/people/{p['id']}/checkins/ask-now", headers=fh, json={"kind": "wellbeing"})
                r = await call(client, "GET", f"{api}/devices/me/checkins/due", headers=dev)
                if r.status_code == 200 and (r.json() or {}).get("checkin"):
                    await call(client, "POST", f"{api}/devices/me/checkins/{r.json()['checkin']['id']}/answer",
                               headers={**dev, "X-Idempotency-Key": str(uuid.uuid4())},
                               json={"text": "I am fine, thank you.", "via": "voice"})
                if i % a.help_every == a.help_every // 2:
                    key = str(uuid.uuid4())
                    r = await call(client, "POST", f"{api}/devices/me/help", headers={**dev, "X-Idempotency-Key": key},
                                   json={"via": "button", "note": "probe"})
                    await call(client, "POST", f"{api}/devices/me/help", headers={**dev, "X-Idempotency-Key": key},
                               json={"via": "button", "note": "probe"})
                    if r.status_code == 201:
                        aid = r.json()["id"]
                        await call(client, "POST", f"{api}/alerts/{aid}/acknowledge", headers=fh,
                                   json={"kind": "on_my_way", "note": "probe"})
                        await call(client, "POST", f"{api}/alerts/{aid}/resolve", headers=fh,
                                   json={"resolution": "Memory probe: fictional.", "outcome": "false_alarm"})

            record("start", 0)
            for i in range(1, a.rounds + 1):
                await asyncio.gather(*(person(p, i) for p in creds["people"]))
                if a.pause_ms:
                    await asyncio.sleep(a.pause_ms / 1000.0)
                if i % a.every == 0:
                    record("interval", i)
    print(json.dumps(counts["status"]))


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
