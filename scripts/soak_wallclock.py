#!/usr/bin/env python3
"""Wall-clock soak: a real running IRA CARE stack under steady fictional load.

    # Start the demo API (which also runs the care worker jobs) against a local
    # database, seed it, and soak it for 24 hours of real time:
    IRA_TEST_DATABASE_DSN=... IRA_TEST_ADMIN_DSN=... \\
        python3 scripts/soak_wallclock.py --start-demo --hours 24 --out .demo/soak

    # Soak a stack that is already running (for example the compose stack in
    # deploy/), with a credentials file in the seed format:
    python3 scripts/soak_wallclock.py --base-url http://127.0.0.1:8000 \\
        --credentials .demo/demo.local.json --admin-dsn postgresql://... --hours 24 --out .demo/soak

    # Carry on after an interruption (the gap is recorded, never hidden):
    python3 scripts/soak_wallclock.py --resume .demo/soak/checkpoint.jsonl ...

    # Summarise a checkpoint file without running anything:
    python3 scripts/soak_wallclock.py --summarise .demo/soak/checkpoint.jsonl

This is NOT scripts/soak_24h.py. That script runs an accelerated, simulated day
in a few minutes with an explicit clock. This one runs in real time against a
running server: every second of --hours passes on the wall clock.

The load is fictional and goes through the public API only: the paired demo
watches report heart-rate and step readings, family members ask for check-ins
and the watches answer them, the watches press Help, and a family member
acknowledges and then resolves each alert. Message providers must be log-only:
the run refuses to start if a real SMS, call, email or monitoring-centre
provider is configured in its environment, so nothing ever leaves the machine.

Every sample is appended to a JSONL checkpoint (one JSON object per line,
flushed and fsynced), so a run that is killed still leaves its evidence. The
summary is computed from that file alone by `summarise()`, which is pure and
unit tested.

Pass criteria (stated before the run; all must hold, see CRITERIA):

* observed coverage: the harness was watching for at least 99 % of the wall
  clock between start and end (otherwise the verdict is INCOMPLETE, not PASS);
* availability: at least 99.9 % of health checks answered 200;
* no server errors (HTTP 5xx) and no client errors (other 4xx) on the load
  requests (a 401 for an expired family token, followed by signing in again,
  is counted as `auth_refresh`, not as an error);
* no lost Help alerts (every Help request the API accepted is an alert in the
  database) and no duplicate Help alerts (no alert the harness did not ask for,
  and a replay of the same idempotency key returns the same alert);
* no process restarts of the watched processes;
* RSS growth of each watched process at most 5 % per hour (least-squares slope
  after a warm-up, relative to the RSS when the warm-up ended);
* p95 latency of the load requests at most 1000 ms over the run;
* worker heartbeat never older than 180 s (when a heartbeat file is known);
* at the end, no outbox row (care_alert_dispatch) left unfinished for more
  than 300 s, and no alert of the soak's people left open (each was
  acknowledged and resolved).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import platform
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1

#: The pass/fail criteria. Stated here, written into the checkpoint's first
#: line, and applied by `summarise()`. Change them only by flag, and the flag
#: values are recorded with the run.
CRITERIA: dict[str, float] = {
    "min_observed_coverage": 0.99,
    "min_availability": 0.999,
    "max_server_errors": 0,
    "max_client_errors": 0,
    "max_lost_alerts": 0,
    "max_duplicate_alerts": 0,
    "max_restarts": 0,
    "max_rss_growth_pct_per_hour": 5.0,
    "max_p95_latency_ms": 1000.0,
    "max_heartbeat_age_s": 180.0,
    "max_stuck_outbox_rows": 0,
    "max_open_alerts_at_end": 0,
    "stuck_outbox_after_s": 300.0,
    "rss_warmup_s": 600.0,
}

#: Environment variables that would make the stack send something real. The
#: harness refuses to start while any of them is set.
REAL_PROVIDER_VARS = (
    "IRA_CARE_TWILIO_ACCOUNT_SID", "IRA_CARE_TWILIO_AUTH_TOKEN", "IRA_CARE_TWILIO_FROM",
    "IRA_CARE_ARC_URL", "IRA_CARE_ARC_SECRET",
    "IRA_CARE_SMTP_HOST", "IRA_CARE_SMTP_USERNAME", "IRA_CARE_SMTP_PASSWORD",
    "IRA_CARE_EXPO_ACCESS_TOKEN", "IRA_CARE_PUSH_PROVIDER",
)


# ── Pure parts (unit tested in tests/readiness/test_soak_wallclock.py) ───────

def percentile(values: Iterable[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile; None for no values."""
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    if not 0 < pct <= 100:
        raise ValueError("pct must be in (0, 100]")
    rank = max(1, math.ceil(pct / 100.0 * len(xs)))
    return xs[rank - 1]


def slope_per_hour(points: list[tuple[float, float]]) -> Optional[float]:
    """Least-squares slope of (seconds, value) points, in value units per hour."""
    if len(points) < 2:
        return None
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((t - mx) ** 2 for t, _ in points)
    if sxx == 0:
        return None
    sxy = sum((t - mx) * (v - my) for t, v in points)
    return sxy / sxx * 3600.0


def rss_growth_pct_per_hour(points: list[tuple[float, float]], warmup_s: float) -> Optional[float]:
    """RSS growth after the warm-up, as a percentage of the RSS when it ended, per hour.
    None when fewer than two points fall after the warm-up."""
    if not points:
        return None
    t0 = points[0][0]
    after = [(t, v) for t, v in points if t - t0 >= warmup_s]
    if len(after) < 2:
        return None
    base = after[0][1]
    slope = slope_per_hour(after)
    if slope is None or base <= 0:
        return None
    return slope / base * 100.0


def observed_gaps(times: list[float], expected_interval: float, tolerance: float = 3.0) -> list[dict]:
    """Stretches where consecutive samples are further apart than `tolerance`
    sampling intervals: the harness was not watching. Each gap counts only the
    time beyond one ordinary interval."""
    gaps = []
    ts = sorted(times)
    for a, b in zip(ts, ts[1:]):
        if b - a > tolerance * expected_interval:
            gaps.append({"from": a, "to": b, "unobserved_s": round(b - a - expected_interval, 3)})
    return gaps


def reconcile_alerts(issued: list[dict], observed_ids: Iterable[str]) -> dict:
    """Compare the Help alerts the harness caused with those in the database.

    `issued`: one entry per Help request the API accepted, with the idempotency
    key and the alert id the API answered (and `replay_id` when the same key was
    sent again). `observed_ids`: the help alerts the database holds for the soak's
    people since the start. Lost: accepted but not in the database. Duplicate: in
    the database but never answered to the harness, or one key answered with two
    different alerts, or two keys answered with the same alert."""
    observed = list(observed_ids)
    observed_set = set(observed)
    by_key: dict[str, set] = {}
    id_keys: dict[str, set] = {}
    replay_mismatch = 0
    for e in issued:
        aid = e.get("alert_id")
        if not aid:
            continue
        by_key.setdefault(e["key"], set()).add(aid)
        id_keys.setdefault(aid, set()).add(e["key"])
        if e.get("replay_id") is not None and e["replay_id"] != aid:
            replay_mismatch += 1
    expected = set(id_keys)
    lost = sorted(expected - observed_set)
    unexpected = sorted(observed_set - expected)
    db_repeats = len(observed) - len(observed_set)
    key_split = sum(1 for ids in by_key.values() if len(ids) > 1)
    shared = sum(1 for ks in id_keys.values() if len(ks) > 1)
    return {
        "issued": len(expected),
        "observed": len(observed_set),
        "lost": len(lost),
        "lost_ids": lost[:20],
        "duplicates": len(unexpected) + db_repeats + key_split + shared + replay_mismatch,
        "unexpected_ids": unexpected[:20],
        "replay_mismatches": replay_mismatch,
    }


def restarts(samples: list[dict]) -> dict[str, int]:
    """Per watched process: how many times the process seen at one sample was
    gone, or was another process (another pid or start time), at the next."""
    out: dict[str, int] = {}
    last: dict[str, Optional[tuple]] = {}
    for s in samples:
        for name, p in (s.get("processes") or {}).items():
            ident = (p.get("pid"), p.get("create_time")) if p.get("alive") else None
            out.setdefault(name, 0)
            # One count per loss of the process seen last: it vanished or was
            # replaced. Coming back after vanishing is the same event.
            if last.get(name) is not None and ident != last[name]:
                out[name] += 1
            last[name] = ident
    return out


def read_checkpoint(path: Path) -> list[dict]:
    """Every whole line of the checkpoint. A torn last line (the process was
    killed mid-write) is skipped, not an error."""
    recs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def summarise(records: list[dict], now_wall: Optional[float] = None) -> dict:
    """The run's summary and verdict, from the checkpoint records alone."""
    start = next((r for r in records if r.get("type") == "start"), None)
    if start is None:
        raise ValueError("checkpoint has no start record")
    crit = {**CRITERIA, **(start.get("criteria") or {})}
    samples = [r for r in records if r.get("type") == "sample"]
    ends = [r for r in records if r.get("type") == "end"]
    resumes = [r for r in records if r.get("type") == "resume"]
    events = [r for r in records if r.get("type") == "event"]
    final = next((r for r in reversed(records) if r.get("type") == "final_check"), None)

    stopped = next((r for r in reversed(records) if r.get("type") == "load_stopped"), None)
    t_start = start["wall"]
    planned_end = t_start + start["hours"] * 3600.0
    last_wall = max([r.get("wall", t_start) for r in records])
    # The observed window ends when the load stopped; the settling wait before
    # the final database check is not part of it.
    t_end = stopped["wall"] if stopped else ends[-1]["wall"] if ends else (now_wall if now_wall is not None else last_wall)
    duration = max(0.0, t_end - t_start)
    completed = bool(ends) and ends[-1].get("reason") == "completed"

    interval = float(start.get("sample_interval_s", 30))
    times = [t_start] + [s["wall"] for s in samples if s["wall"] <= t_end] + ([t_end] if (ends or stopped) else [])
    gaps = observed_gaps(times, interval)
    unobserved = sum(g["unobserved_s"] for g in gaps)
    coverage = 1.0 if duration == 0 else max(0.0, 1.0 - unobserved / duration)

    health_ok = sum(s.get("health", {}).get("ok", 0) for s in samples)
    health_total = sum(s.get("health", {}).get("total", 0) for s in samples)
    availability = (health_ok / health_total) if health_total else None

    lat = [x for s in samples for x in s.get("load", {}).get("latencies_ms", [])]
    server_errors = sum(s.get("load", {}).get("server_errors", 0) for s in samples)
    client_errors = sum(s.get("load", {}).get("client_errors", 0) for s in samples)
    conn_errors = sum(s.get("load", {}).get("connection_errors", 0) for s in samples)
    requests = sum(s.get("load", {}).get("requests", 0) for s in samples)
    counts: dict[str, int] = {}
    for s in samples:
        for k, v in (s.get("load", {}).get("counts") or {}).items():
            counts[k] = counts.get(k, 0) + v

    rss: dict[str, list[tuple[float, float]]] = {}
    cpu: dict[str, list[float]] = {}
    for s in samples:
        for name, p in (s.get("processes") or {}).items():
            if p.get("alive") and p.get("rss_bytes"):
                rss.setdefault(name, []).append((s["wall"], float(p["rss_bytes"])))
            if p.get("alive") and p.get("cpu_percent") is not None:
                cpu.setdefault(name, []).append(float(p["cpu_percent"]))
    growth = {n: rss_growth_pct_per_hour(pts, crit["rss_warmup_s"]) for n, pts in rss.items()}
    proc_summary = {
        n: {"rss_first_mb": round(pts[0][1] / 2**20, 1), "rss_last_mb": round(pts[-1][1] / 2**20, 1),
            "rss_max_mb": round(max(v for _, v in pts) / 2**20, 1),
            "rss_growth_pct_per_hour": None if growth[n] is None else round(growth[n], 3),
            "cpu_mean_pct": round(sum(cpu.get(n, [0])) / max(1, len(cpu.get(n, []))), 2),
            "cpu_p95_pct": percentile(cpu.get(n, []), 95)}
        for n, pts in rss.items()
    }
    rs = restarts(samples)

    hb = [s["worker_heartbeat_age_s"] for s in samples if s.get("worker_heartbeat_age_s") is not None]
    db = [(s["wall"], s["db"]["size_bytes"]) for s in samples if (s.get("db") or {}).get("size_bytes")]
    tables_first = next(((s.get("db") or {}).get("rows") for s in samples if (s.get("db") or {}).get("rows")), None)
    tables_last = next(((s.get("db") or {}).get("rows") for s in reversed(samples) if (s.get("db") or {}).get("rows")), None)
    outbox = [s["db"].get("outbox_pending", 0) for s in samples if s.get("db")]
    retries = [s["db"].get("dispatch_retries", 0) for s in samples if s.get("db")]

    issued = [e for e in events if e.get("event") == "help"]
    alerts = reconcile_alerts(issued, (final or {}).get("help_alert_ids", [])) if final else None
    stuck = (final or {}).get("stuck_outbox_rows")
    open_end = (final or {}).get("open_alerts_at_end")
    p95 = percentile(lat, 95)

    checks = {
        "observed_coverage": coverage >= crit["min_observed_coverage"],
        "availability": availability is not None and availability >= crit["min_availability"],
        "server_errors": server_errors <= crit["max_server_errors"],
        "client_errors": client_errors <= crit.get("max_client_errors", 0),
        "lost_alerts": alerts is not None and alerts["lost"] <= crit["max_lost_alerts"],
        "duplicate_alerts": alerts is not None and alerts["duplicates"] <= crit["max_duplicate_alerts"],
        "restarts": sum(rs.values()) <= crit["max_restarts"],
        "rss_growth": all(g is not None and g <= crit["max_rss_growth_pct_per_hour"] for g in growth.values()) and bool(growth),
        "p95_latency": p95 is not None and p95 <= crit["max_p95_latency_ms"],
        "worker_heartbeat": (not start.get("heartbeat_file")) or (bool(hb) and max(hb) <= crit["max_heartbeat_age_s"]),
        "outbox_drained": stuck is not None and stuck <= crit["max_stuck_outbox_rows"],
        "alerts_closed": open_end is not None and open_end <= crit.get("max_open_alerts_at_end", 0),
    }
    if not completed or not checks["observed_coverage"]:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS" if all(checks.values()) else "FAIL"
    return {
        "schema": SCHEMA,
        "run_id": start["run_id"],
        "label": start.get("label", ""),
        "mode": start.get("mode"),
        "planned_hours": start["hours"],
        "wall_clock": {
            "start_utc": _iso(t_start), "end_utc": _iso(t_end), "planned_end_utc": _iso(planned_end),
            "duration_s": round(duration, 1), "duration_h": round(duration / 3600.0, 4),
            "completed": completed, "end_reason": ends[-1].get("reason") if ends else "no end record (interrupted)",
            "resumes": len(resumes), "gaps": gaps, "unobserved_s": round(unobserved, 1),
            "observed_coverage": round(coverage, 6),
        },
        "samples": len(samples),
        "availability": {"health_ok": health_ok, "health_total": health_total,
                         "ratio": None if availability is None else round(availability, 6)},
        "load": {"requests": requests, "server_errors": server_errors, "client_errors": client_errors,
                 "connection_errors": conn_errors, "counts": counts,
                 "latency_ms": {"p50": percentile(lat, 50), "p95": p95, "p99": percentile(lat, 99),
                                "max": max(lat) if lat else None, "n": len(lat)}},
        "processes": proc_summary,
        "restarts": rs,
        "worker_heartbeat_age_s": {"max": max(hb) if hb else None, "n": len(hb)},
        "db": {"size_first_mb": round(db[0][1] / 2**20, 2) if db else None,
               "size_last_mb": round(db[-1][1] / 2**20, 2) if db else None,
               "growth_mb_per_hour": None if len(db) < 2 else round((slope_per_hour(db) or 0) / 2**20, 3),
               "rows_first": tables_first, "rows_last": tables_last,
               "outbox_pending_max": max(outbox) if outbox else None,
               "dispatch_retries_last": retries[-1] if retries else None},
        "alerts": alerts,
        "stuck_outbox_rows": stuck,
        "open_alerts_at_end": open_end,
        "checkins_by_status": (final or {}).get("checkins_by_status"),
        "other_alerts_by_kind": (final or {}).get("other_alerts_by_kind"),
        "criteria": crit,
        "checks": checks,
        "verdict": verdict,
    }


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def refuse_real_providers(env: dict) -> list[str]:
    """The names of real-provider variables set in `env` (non-empty)."""
    return [k for k in REAL_PROVIDER_VARS if (env.get(k) or "").strip()]


# ── Checkpoint writer ────────────────────────────────────────────────────────

class Checkpoint:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, rec: dict) -> None:
        rec.setdefault("wall", time.time())
        self.fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self) -> None:
        self.fh.close()


# ── Process sampling ─────────────────────────────────────────────────────────

class ProcessWatch:
    """A watched process found by pid or by a command-line substring. CPU % is
    psutil's since the previous sample (100 = one core)."""

    def __init__(self, name: str, *, pid: Optional[int] = None, match: Optional[str] = None):
        self.name, self.pid, self.match = name, pid, match
        self._proc = None

    def _find(self):
        import psutil
        if self.match:
            me = os.getpid()
            for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
                cmd = " ".join(p.info.get("cmdline") or [])
                if self.match in cmd and p.info["pid"] != me and "soak_wallclock" not in cmd:
                    return p
            return None
        return psutil.Process(self.pid) if self.pid else None

    def sample(self) -> dict:
        import psutil
        try:
            if self._proc is None or not self._proc.is_running():
                self._proc = self._find()
                if self._proc is not None:
                    self._proc.cpu_percent(None)
            if self._proc is None:
                return {"alive": False}
            with self._proc.oneshot():
                return {"alive": True, "pid": self._proc.pid, "create_time": self._proc.create_time(),
                        "rss_bytes": self._proc.memory_info().rss, "cpu_percent": self._proc.cpu_percent(None),
                        "threads": self._proc.num_threads(),
                        "open_fds": self._proc.num_fds() if hasattr(self._proc, "num_fds") else None}
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            self._proc = None
            return {"alive": False}


# ── The run ──────────────────────────────────────────────────────────────────

class Soak:
    def __init__(self, args, creds: dict, ckpt: Checkpoint, start_rec: dict):
        self.a = args
        self.creds = creds
        self.ckpt = ckpt
        self.start_rec = start_rec
        self.stop = asyncio.Event()
        self.window: dict[str, Any] = self._new_window()
        self.rng = random.Random(start_rec["run_id"])
        self.watches = [ProcessWatch(n, pid=p, match=m) for n, p, m in args._watches]
        self.child: Optional[subprocess.Popen] = None

    @staticmethod
    def _new_window() -> dict:
        return {"requests": 0, "server_errors": 0, "client_errors": 0, "connection_errors": 0,
                "latencies_ms": [], "counts": {}, "health_ok": 0, "health_total": 0, "health_ms": []}

    def _count(self, what: str) -> None:
        self.window["counts"][what] = self.window["counts"].get(what, 0) + 1

    async def call(self, client, method: str, path: str, *, what: str, ok=(200, 201), **kw):
        t0 = time.perf_counter()
        self.window["requests"] += 1
        try:
            r = await client.request(method, path, **kw)
        except Exception as exc:   # connection refused, timeout: a real failure, counted
            self.window["connection_errors"] += 1
            self._count(f"{what}:conn_error:{type(exc).__name__}")
            return None
        ms = (time.perf_counter() - t0) * 1000.0
        self.window["latencies_ms"].append(round(ms, 2))
        if r.status_code >= 500:
            self.window["server_errors"] += 1
        elif r.status_code not in ok:
            self.window["client_errors"] += 1
        self._count(f"{what}:{r.status_code}")
        return r

    async def login(self, client) -> dict:
        fam = next(l for l in self.creds["logins"] if l["key"] == "family")
        r = await client.post("/auth/cloud/login", data={"email": fam["email"], "password": fam["password"]})
        r.raise_for_status()
        tok = r.json()
        return {"Authorization": f"Bearer {tok['access_token']}", "X-Workspace-ID": self.creds["workspace_id"]}

    async def lift_quiet_hours(self, client) -> list[str]:
        """Fictional people only: set an empty quiet-hours window (start equals end) so
        the check-ins are asked and answered at any hour of the run. Uses the
        coordinator's login and the ordinary settings route."""
        coord = next(l for l in self.creds["logins"] if l.get("role") == "workspace_admin")
        r = await client.post("/auth/cloud/login", data={"email": coord["email"], "password": coord["password"]})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.json()['access_token']}", "X-Workspace-ID": self.creds["workspace_id"]}
        done = []
        for p in self.creds["people"]:
            g = await client.get(f"/api/v1/care/people/{p['id']}/settings", headers=h)
            g.raise_for_status()
            body = {k: v for k, v in g.json().items() if v is not None}
            body["quiet_start"] = body["quiet_end"] = "12:00:00"
            u = await client.put(f"/api/v1/care/people/{p['id']}/settings", headers=h, json=body)
            done.append(f"{p['id']}:{u.status_code}")
        return done

    async def health_loop(self, client) -> None:
        while not self.stop.is_set():
            t0 = time.perf_counter()
            ok = False
            try:
                r = await client.get(self.a.health_path, timeout=5.0)
                ok = r.status_code == 200
            except Exception:
                ok = False
            self.window["health_total"] += 1
            self.window["health_ok"] += int(ok)
            self.window["health_ms"].append(round((time.perf_counter() - t0) * 1000.0, 2))
            await self._sleep(self.a.health_interval)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    async def fam_call(self, client, session: dict, method: str, path: str, *, what: str, **kw):
        """A family member's call. Access tokens last 30 minutes: on a 401 the
        harness signs in again, as the app does, and retries once. That 401 is
        counted as `auth_refresh`, not as an error; a second 401 is an error."""
        if not session.get("headers"):
            session["headers"] = await self.login(client)
        r = await self.call(client, method, path, what=what, ok=(200, 201, 401), headers=session["headers"], **kw)
        if r is not None and r.status_code == 401:
            self._count("auth_refresh")
            session["headers"] = await self.login(client)
            r = await self.call(client, method, path, what=what, headers=session["headers"], **kw)
        return r

    async def person_loop(self, client, person: dict) -> None:
        """One fictional person: readings, check-ins, and a Help press now and then."""
        dev = {"X-Device-Token": person["device_token"]}
        api = "/api/v1/care"
        family: dict = {}
        next_ask = time.time() + self.rng.uniform(5, self.a.checkin_every)
        next_help = time.time() + self.rng.uniform(30, self.a.help_every)
        while not self.stop.is_set():
            now = time.time()
            try:
                stamp = dt.datetime.now(dt.UTC).isoformat()
                await self.call(client, "POST", f"{api}/devices/me/report", what="report", headers=dev, json={
                    "readings": [{"kind": "heart_rate", "value": round(self.rng.uniform(62, 88), 1), "unit": "bpm", "recorded_at": stamp},
                                 {"kind": "steps", "value": float(self.rng.randint(5, 60)), "unit": "", "recorded_at": stamp}],
                    "battery_percent": 80, "on_wrist": True, "background_sensor_permission": True})
                if now >= next_ask:
                    await self.fam_call(client, family, "POST", f"{api}/people/{person['id']}/checkins/ask-now",
                                        what="ask_now", json={"kind": "wellbeing"})
                    next_ask = now + self.a.checkin_every
                r = await self.call(client, "GET", f"{api}/devices/me/checkins/due", what="due", headers=dev)
                if r is not None and r.status_code == 200 and (r.json() or {}).get("checkin"):
                    cid = r.json()["checkin"]["id"]
                    await self.call(client, "POST", f"{api}/devices/me/checkins/{cid}/answer", what="answer",
                                    headers={**dev, "X-Idempotency-Key": str(uuid.uuid4())},
                                    json={"text": "I am fine, thank you.", "via": "voice"})
                if now >= next_help:
                    await self.help_cycle(client, person, dev, family)
                    next_help = time.time() + self.a.help_every
            except Exception as exc:   # never let one person's loop die silently
                self._count(f"loop_error:{type(exc).__name__}")
                family.clear()
            await self._sleep(self.a.load_interval)

    async def help_cycle(self, client, person: dict, dev: dict, family: dict) -> None:
        # `family` is the person loop's session: fam_call signs in again when the token expires.
        """Help, a replay of the same key, wait, acknowledge, resolve. The key is
        resent on a failed call, as the watch's outbox does."""
        api = "/api/v1/care"
        key = str(uuid.uuid4())
        alert_id = None
        for _ in range(3):
            r = await self.call(client, "POST", f"{api}/devices/me/help", what="help",
                                headers={**dev, "X-Idempotency-Key": key}, json={"via": "button", "note": "soak"})
            if r is not None and r.status_code == 201:
                alert_id = r.json().get("id")
                break
            await self._sleep(2)
        replay_id = None
        if alert_id:
            r = await self.call(client, "POST", f"{api}/devices/me/help", what="help_replay",
                                headers={**dev, "X-Idempotency-Key": key}, json={"via": "button", "note": "soak"})
            if r is not None and r.status_code == 201:
                replay_id = r.json().get("id")
        self.ckpt.write({"type": "event", "event": "help", "person_id": person["id"], "key": key,
                         "alert_id": alert_id, "replay_id": replay_id})
        if not alert_id:
            return
        await self._sleep(self.a.ack_after)   # long enough for the worker to escalate to tier 1
        await self.fam_call(client, family, "POST", f"{api}/alerts/{alert_id}/acknowledge", what="acknowledge",
                            json={"kind": "on_my_way", "note": "soak"})
        await self.fam_call(client, family, "POST", f"{api}/alerts/{alert_id}/resolve", what="resolve",
                            json={"resolution": "Soak test: fictional.", "outcome": "false_alarm"})

    async def db_sample(self, pool) -> dict:
        people = [uuid.UUID(p["id"]) for p in self.creds["people"]]
        async with pool.acquire() as c:
            size = await c.fetchval("SELECT pg_database_size(current_database())")
            rows = {}
            for t in ("care_alerts", "care_alert_attempts", "care_alert_attempt_events", "care_alert_dispatch",
                      "care_acknowledgements", "care_checkins", "care_vitals", "care_device_events", "care_transcripts"):
                if await c.fetchval("SELECT to_regclass($1)", "public." + t):
                    rows[t] = await c.fetchval(f"SELECT count(*) FROM {t}")
            pending = await c.fetchval("SELECT count(*) FROM care_alert_dispatch WHERE finished_at IS NULL")
            oldest = await c.fetchval("SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM care_alert_dispatch WHERE finished_at IS NULL")
            retries = await c.fetchval("SELECT coalesce(sum(tries - 1), 0) FROM care_alert_dispatch WHERE tries > 1")
            outcomes = {r["outcome"]: r["n"] for r in await c.fetch(
                "SELECT a.outcome, count(*) AS n FROM care_alert_attempts a JOIN care_alerts l ON l.id = a.alert_id "
                "WHERE l.person_id = ANY($1::uuid[]) GROUP BY 1", people)}
            open_alerts = await c.fetchval("SELECT count(*) FROM care_alerts WHERE person_id = ANY($1::uuid[]) "
                                           "AND status NOT IN ('resolved','cancelled')", people)
        return {"size_bytes": size, "rows": rows, "outbox_pending": pending,
                "outbox_oldest_pending_s": None if oldest is None else float(oldest),
                "dispatch_retries": int(retries), "attempt_outcomes": outcomes, "open_alerts": open_alerts}

    async def sampler(self, pool) -> None:
        seq = self.start_rec.get("_next_seq", 0)
        for w in self.watches:
            w.sample()
        while not self.stop.is_set():
            await self._sleep(self.a.sample_interval)
            win, self.window = self.window, self._new_window()
            rec = {"type": "sample", "seq": seq, "wall": time.time(), "mono": time.monotonic(),
                   "health": {"ok": win["health_ok"], "total": win["health_total"],
                              "p95_ms": percentile(win["health_ms"], 95)},
                   "load": {k: win[k] for k in ("requests", "server_errors", "client_errors", "connection_errors",
                                                "latencies_ms", "counts")},
                   "load_p95_ms": percentile(win["latencies_ms"], 95),
                   "processes": {w.name: w.sample() for w in self.watches}}
            if self.a.heartbeat_file:
                try:
                    rec["worker_heartbeat_age_s"] = round(time.time() - os.path.getmtime(self.a.heartbeat_file), 1)
                except OSError:
                    rec["worker_heartbeat_age_s"] = None
                    rec["worker_heartbeat_missing"] = True
            try:
                rec["db"] = await self.db_sample(pool)
            except Exception as exc:
                rec["db"] = {"error": type(exc).__name__}
            self.ckpt.write(rec)
            seq += 1
            if time.time() >= self.start_rec["wall"] + self.start_rec["hours"] * 3600.0:
                self.stop.set()

    async def final_check(self, pool) -> dict:
        people = [uuid.UUID(p["id"]) for p in self.creds["people"]]
        since = dt.datetime.fromtimestamp(self.start_rec["wall"], dt.UTC)
        async with pool.acquire() as c:
            ids = [str(r["id"]) for r in await c.fetch(
                "SELECT id FROM care_alerts WHERE person_id = ANY($1::uuid[]) AND kind = 'help_requested' "
                "AND opened_at >= $2 ORDER BY opened_at", people, since)]
            other = {r["kind"]: r["n"] for r in await c.fetch(
                "SELECT kind, count(*) AS n FROM care_alerts WHERE person_id = ANY($1::uuid[]) AND kind <> 'help_requested' "
                "AND opened_at >= $2 GROUP BY 1", people, since)}
            stuck = await c.fetchval(
                "SELECT count(*) FROM care_alert_dispatch WHERE finished_at IS NULL AND created_at < now() - make_interval(secs => $1)",
                float(CRITERIA["stuck_outbox_after_s"]))
            open_end = await c.fetchval("SELECT count(*) FROM care_alerts WHERE person_id = ANY($1::uuid[]) "
                                        "AND opened_at >= $2 AND status NOT IN ('resolved', 'cancelled')", people, since)
            checkins = {r["status"]: r["n"] for r in await c.fetch(
                "SELECT status, count(*) AS n FROM care_checkins WHERE person_id = ANY($1::uuid[]) AND scheduled_at >= $2 GROUP BY 1",
                people, since)}
        return {"type": "final_check", "help_alert_ids": ids, "other_alerts_by_kind": other,
                "stuck_outbox_rows": stuck, "open_alerts_at_end": open_end, "checkins_by_status": checkins}

    async def run(self) -> str:
        import asyncpg
        import httpx
        loop = asyncio.get_running_loop()
        reason = {"v": "completed"}

        def _interrupt(sig):
            reason["v"] = f"interrupted:{signal.Signals(sig).name}"
            self.stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _interrupt, sig)
        pool = await asyncpg.create_pool(dsn=self.a.admin_dsn, min_size=1, max_size=2)
        try:
            async with httpx.AsyncClient(base_url=self.a.base_url, timeout=10.0) as client:
                if self.a.no_quiet_hours:
                    self.ckpt.write({"type": "setup", "quiet_hours_lifted": await self.lift_quiet_hours(client)})
                tasks = [asyncio.create_task(self.health_loop(client)), asyncio.create_task(self.sampler(pool))]
                tasks += [asyncio.create_task(self.person_loop(client, p)) for p in self.creds["people"]]
                await self.stop.wait()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.ckpt.write({"type": "load_stopped", "reason": reason["v"]})
            # Let in-flight escalations settle before judging the outbox.
            if reason["v"] == "completed":
                await asyncio.sleep(min(90.0, self.a.ack_after + 30))
            self.ckpt.write(await self.final_check(pool))
        finally:
            await pool.close()
        self.ckpt.write({"type": "end", "reason": reason["v"]})
        return reason["v"]


# ── Starting the demo stack ──────────────────────────────────────────────────

def start_demo(args, out: Path, env: dict) -> tuple[subprocess.Popen, dict]:
    seed_out = out / "seed.local.json"
    py = sys.executable
    subprocess.run([py, str(ROOT / "scripts" / "demo" / "seed_demo.py"), "--out", str(seed_out)],
                   check=True, env=env, stdout=subprocess.DEVNULL)
    os.chmod(seed_out, 0o600)
    creds = json.loads(seed_out.read_text())
    log = open(out / "api.log", "ab")
    proc = subprocess.Popen([py, str(ROOT / "scripts" / "demo" / "api.py")], env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    import httpx
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"demo API exited with {proc.returncode}; see {out / 'api.log'}")
        try:
            if httpx.get(args.base_url + args.health_path, timeout=2).status_code == 200:
                return proc, creds
        except Exception:
            pass
        time.sleep(1)
    proc.terminate()
    raise SystemExit("demo API did not become healthy within 120 s")


def host_info() -> dict:
    info = {"hostname_hash": uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()).hex[:12],
            "platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()}
    try:
        import psutil
        info["memory_total_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:
        pass
    return info


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=str(ROOT / ".demo" / "soak"))
    ap.add_argument("--start-demo", action="store_true", help="seed and start scripts/demo/api.py (API + care worker jobs)")
    ap.add_argument("--port", type=int, default=8765, help="port for --start-demo")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--credentials", default=None, help="seed-format JSON (logins, workspace_id, people with device tokens)")
    ap.add_argument("--admin-dsn", default=os.getenv("IRA_TEST_ADMIN_DSN"), help="read-only use: sizes, counts, reconciliation")
    ap.add_argument("--health-path", default="/health")
    ap.add_argument("--heartbeat-file", default=None)
    ap.add_argument("--watch-pid", action="append", default=[], metavar="NAME=PID")
    ap.add_argument("--watch-match", action="append", default=[], metavar="NAME=SUBSTRING",
                    help="watch the process whose command line contains SUBSTRING")
    ap.add_argument("--sample-interval", type=float, default=30.0)
    ap.add_argument("--health-interval", type=float, default=5.0)
    ap.add_argument("--load-interval", type=float, default=5.0, help="seconds between one person's load rounds")
    ap.add_argument("--checkin-every", type=float, default=300.0)
    ap.add_argument("--help-every", type=float, default=600.0)
    ap.add_argument("--ack-after", type=float, default=90.0)
    ap.add_argument("--no-quiet-hours", action="store_true",
                    help="give the fictional people an empty quiet-hours window so check-ins run at any hour")
    ap.add_argument("--resume", default=None, help="continue the run in this checkpoint")
    ap.add_argument("--summarise", default=None, help="only summarise this checkpoint")
    for k, v in CRITERIA.items():
        ap.add_argument("--" + k.replace("_", "-"), type=float, default=v)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.summarise:
        s = summarise(read_checkpoint(Path(a.summarise)), now_wall=time.time())
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0 if s["verdict"] == "PASS" else 1
    bad = refuse_real_providers(dict(os.environ))
    if bad:
        print("Refusing to soak: real message providers are configured: " + ", ".join(bad), file=sys.stderr)
        return 2
    if not a.admin_dsn:
        print("Set --admin-dsn or IRA_TEST_ADMIN_DSN (used to read sizes, counts and the alerts for reconciliation).", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    crit = {k: getattr(a, k) for k in CRITERIA}
    a._watches = [(n, int(p), None) for n, p in (w.split("=", 1) for w in a.watch_pid)]
    a._watches += [(n, None, m) for n, m in (w.split("=", 1) for w in a.watch_match)]

    child = None
    env = {k: v for k, v in os.environ.items() if k not in REAL_PROVIDER_VARS}
    if a.resume:
        path = Path(a.resume)
        recs = read_checkpoint(path)
        start = next(r for r in recs if r.get("type") == "start")
        if any(r.get("type") == "end" and r.get("reason") == "completed" for r in recs):
            print("That run already completed; summarise it instead.", file=sys.stderr)
            return 2
        a.hours = start["hours"]
        a.sample_interval = start["sample_interval_s"]
        a.heartbeat_file = a.heartbeat_file or start.get("heartbeat_file")
        if start.get("mode") == "demo":
            a.base_url = start["base_url"]
            a.port = int(a.base_url.rsplit(":", 1)[1])
            creds = json.loads((path.parent / "seed.local.json").read_text())
        else:
            creds = json.loads(Path(a.credentials).read_text())
        ckpt = Checkpoint(path)
        last = max(r.get("wall", 0) for r in recs)
        ckpt.write({"type": "resume", "gap_s": round(time.time() - last, 1), "last_record_wall": last})
        start["_next_seq"] = 1 + max([r.get("seq", -1) for r in recs if r.get("type") == "sample"] or [-1])
        if start.get("mode") == "demo":
            env.update({"IRA_CARE_DEMO_PORT": str(a.port), "WORKER_HEARTBEAT_FILE": a.heartbeat_file})
            import httpx
            try:
                healthy = httpx.get(a.base_url + a.health_path, timeout=2).status_code == 200
            except Exception:
                healthy = False
            if not healthy:
                log = open(out / "api.log", "ab")
                child = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "demo" / "api.py")], env=env,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                time.sleep(15)
            a._watches.append(("api", None, "scripts/demo/api.py"))
    else:
        if a.start_demo:
            a.base_url = f"http://127.0.0.1:{a.port}"
            a.heartbeat_file = a.heartbeat_file or str(out / "worker_heartbeat")
            env.update({"IRA_CARE_DEMO_PORT": str(a.port), "WORKER_HEARTBEAT_FILE": a.heartbeat_file})
            child, creds = start_demo(a, out, env)
            a._watches.append(("api", child.pid, None))
        else:
            if not (a.base_url and a.credentials):
                print("Give --start-demo, or --base-url with --credentials.", file=sys.stderr)
                return 2
            creds = json.loads(Path(a.credentials).read_text())
        ckpt_path = out / "checkpoint.jsonl"
        if ckpt_path.exists() and ckpt_path.stat().st_size:
            print(f"{ckpt_path} exists: resume it with --resume, or choose another --out.", file=sys.stderr)
            return 2
        ckpt = Checkpoint(ckpt_path)
        start = {"type": "start", "schema": SCHEMA, "run_id": uuid.uuid4().hex, "label": a.label,
                 "mode": "demo" if a.start_demo else "external", "base_url": a.base_url, "hours": a.hours,
                 "sample_interval_s": a.sample_interval, "health_interval_s": a.health_interval,
                 "load": {"people": len(creds["people"]), "load_interval_s": a.load_interval,
                          "checkin_every_s": a.checkin_every, "help_every_s": a.help_every, "ack_after_s": a.ack_after},
                 "heartbeat_file": a.heartbeat_file, "no_quiet_hours": a.no_quiet_hours, "criteria": crit, "host": host_info(),
                 "git_commit": _git_commit(), "wall": time.time(), "mono": time.monotonic()}
        ckpt.write(dict(start))
    try:
        reason = asyncio.run(Soak(a, creds, ckpt, start).run())
    finally:
        ckpt.close()
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
    s = summarise(read_checkpoint(ckpt.path))
    (ckpt.path.parent / "summary.json").write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: s[k] for k in ("verdict", "checks", "wall_clock")}, indent=2, sort_keys=True))
    print(f"end reason: {reason}; checkpoint {ckpt.path}; summary {ckpt.path.parent / 'summary.json'}")
    return 0 if s["verdict"] == "PASS" else 1


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
