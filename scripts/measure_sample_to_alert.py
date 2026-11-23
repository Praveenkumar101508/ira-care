#!/usr/bin/env python3
"""Measure the sample-to-alert delay through the simulator path.

    python scripts/measure_sample_to_alert.py [--api http://127.0.0.1:8000] [--token cd1...] [--heart-rate 160]

The script does what the browser watch simulator does, over the same device
API with the same device token: it uploads three heart-rate samples outside
the person's range (a minute apart in sample time, all at once in arrival time,
as the watch now does for an out-of-range sample) and times:

  upload_to_question_s   from the moment the upload starts to the server's
                         answer saying a safety question is open ("ask first")
  question_visible_s     until GET /devices/me/sos shows the question (asking)
  sos_open_s             until the server has opened the SOS because nobody
                         answered: the person's `ask_first_wait_s` plus the
                         monitoring job's interval (a design choice, recorded)

Nothing here is fake timing: every number is wall-clock, measured against a
running API. The result is printed as JSON and, with --out, written to a file.
Without --token the device token comes from .demo/demo.local.json (person A).
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

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--heart-rate", type=float, default=160.0)
    ap.add_argument("--wait-sos", type=float, default=200.0, help="how long to wait for the SOS to open (s)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    token = args.token
    if not token:
        info = ROOT / ".demo" / "demo.local.json"
        if not info.exists():
            print("no --token and no .demo/demo.local.json (run scripts/demo/seed_demo.py)", file=sys.stderr)
            return 2
        token = json.loads(info.read_text())["people"][0]["device_token"]
    h = {"X-Device-Token": token, "X-Idempotency-Key": str(uuid.uuid4())}
    c = httpx.Client(base_url=args.api, timeout=20)
    me = c.get("/api/v1/care/devices/me", headers=h)
    me.raise_for_status()
    ranges = me.json()["readings"]
    if me.json()["sos"]["state"] not in ("idle", "closed"):
        print(json.dumps({"error": "an SOS is already open for this person; resolve it first", "sos": me.json()["sos"]}))
        return 3
    now = dt.datetime.now(dt.timezone.utc)
    samples = [{"kind": "heart_rate", "value": args.heart_rate + 5 * i, "unit": "bpm",
                "recorded_at": (now - dt.timedelta(seconds=120 - 60 * i)).isoformat()} for i in range(3)]
    t0 = time.perf_counter()
    r = c.post("/api/v1/care/devices/me/report", json={"readings": samples, "on_wrist": True, "battery_percent": 70,
                                                        "background_sensor_permission": True}, headers=h)
    t1 = time.perf_counter()
    r.raise_for_status()
    out = r.json()
    result = {"api": args.api, "measured_at": now.isoformat(), "heart_rate_range": ranges,
              "samples": [s["value"] for s in samples], "upload_to_question_s": round(t1 - t0, 3),
              "asked_first": "safety_check" in out}
    if "safety_check" not in out:
        result["error"] = "the server did not open a safety question (is a range set for this person?)"
        print(json.dumps(result, indent=2))
        return 4
    # The watch learns of the question through its state stream; here the simulator polls.
    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline:
        s = c.get("/api/v1/care/devices/me/sos", headers=h).json()
        if s["state"] == "asking":
            result["question_visible_s"] = round(time.perf_counter() - t0, 3)
            break
        time.sleep(0.2)
    # Nobody answers: the server opens the SOS after ask_first_wait_s, at the next monitoring pass.
    deadline = time.perf_counter() + args.wait_sos
    while time.perf_counter() < deadline:
        s = c.get("/api/v1/care/devices/me/sos", headers=h).json()
        if s["state"] in ("received", "on_the_way", "arrived"):
            result["sos_open_s"] = round(time.perf_counter() - t0, 3)
            result["sos"] = s
            break
        time.sleep(1.0)
    else:
        result["sos_open_s"] = None
        result["note"] = f"no SOS within {args.wait_sos} s: is the care_monitoring job running?"
    print(json.dumps(result, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
