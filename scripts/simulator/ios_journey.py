#!/usr/bin/env python3
"""Launch the family app in the iOS simulator and read what it shows (register D2.h).

    python3 scripts/simulator/ios_journey.py --app IRACARE.app --out build/ios-journey \\
        [--device "iPhone 16"] [--bundle-id care.ira.family] [--seconds 60]

On a Mac with Xcode: picks (or boots) a simulator, installs the .app, launches it with its console
attached and, for `--seconds`, reads the development build's journey probe lines
(`mobile/src/journeyProbe.ts`: `ira-journey {"screen": "sign-in", ...}`), takes a screenshot and
keeps the app's log. Steps, each recorded in results.json: `simulator_booted`, `installed`,
`launched`, `sign_in_screen_shown` (the probe said `sign-in`), `still_running` (the process is
alive at the end), `no_crash_report` (no new crash report for the app under
~/Library/Logs/DiagnosticReports). Exit status 0 only if every step passed.

The probe exists in development bundles only (`__DEV__`), so this needs the Debug build with its
JavaScript bundled (the workflow builds it with FORCE_BUNDLING=1). A release .app logs nothing and
the sign-in step would fail, truthfully.

Everything that parses simctl's output or the console is a pure function with a unit test
(supracloud-jarvis/ira/tests/readiness/test_ios_journey.py); only `main` touches `xcrun`.
"""
from __future__ import annotations

import argparse
import json
import re
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROBE_PREFIX = "ira-journey "
#: Where the development build posts each probe record (mobile/src/journeyProbe.ts PROBE_URL): the
#: simulator shares the host's loopback. The console of a launched app is not always attached
#: (run 45910155640: one console line in 90 s while the app ran), so the listener is the channel
#: that counts; the console is kept as a second one.
PROBE_PORT = 8777


class ProbeListener:
    """Collects the JSON bodies the app posts to http://127.0.0.1:PROBE_PORT/journey."""

    def __init__(self, port: int = PROBE_PORT):
        self.records: list[dict] = []
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the http.server contract
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", errors="replace")
                listener.accept(body)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):   # quiet
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def accept(self, body: str) -> None:
        try:
            self.records.append(json.loads(body))
        except json.JSONDecodeError:
            pass

    def start(self):
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def screens(self) -> list[str]:
        return [r.get("screen", "") for r in self.records]


def pick_device(simctl_list_json: str, wanted: str | None = None) -> dict | None:
    """The simulator to use from `xcrun simctl list devices available -j`: the one named `wanted`,
    else a booted iPhone, else the newest-runtime iPhone. Returns its record with `runtime` added."""
    data = json.loads(simctl_list_json)
    candidates = []
    for runtime, devices in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        for d in devices:
            if not d.get("isAvailable", True) or "iPhone" not in d.get("name", ""):
                continue
            candidates.append({**d, "runtime": runtime})
    if wanted:
        named = [d for d in candidates if d["name"] == wanted]
        if named:
            return sorted(named, key=lambda d: d["runtime"])[-1]
    booted = [d for d in candidates if d.get("state") == "Booted"]
    if booted:
        return booted[0]
    return sorted(candidates, key=lambda d: (d["runtime"], d["name"]))[-1] if candidates else None


def probe_lines(console: str) -> list[dict]:
    """Every journey probe record in the launched app's console output, in order."""
    out = []
    for line in console.splitlines():
        at = line.find(PROBE_PREFIX)
        if at < 0:
            continue
        try:
            out.append(json.loads(line[at + len(PROBE_PREFIX):].strip()))
        except json.JSONDecodeError:
            continue
    return out


def screens(console: str) -> list[str]:
    return [p.get("screen", "") for p in probe_lines(console)]


#: Lines of the app's unified log worth showing in the job's own log when the screen was not seen:
#: React Native's JavaScript log and its errors (run 45980371196: no probe on either channel).
_DIAGNOSTIC = ("[javascript]", ":javascript]", "com.facebook.react", "ira-journey", "Unhandled", "Exception", "error", "Error", "bundle", "Bundle",
               "RCTFatal", "Invariant", "Unable to", "metro", "Metro")


def diagnostic_lines(app_log: str, limit: int = 60) -> list[str]:
    """The app log lines that say what the JavaScript did (or why it did not run), newest last."""
    out = [ln.strip() for ln in app_log.splitlines() if any(k in ln for k in _DIAGNOSTIC)]
    return out[-limit:]


def launch_pid(output: str, bundle_id: str) -> int | None:
    """The pid from `simctl launch`'s answer ("care.ira.family: 4321"), or None."""
    m = re.search(rf"{re.escape(bundle_id)}:\s*(\d+)", output or "")
    return int(m.group(1)) if m else None


def new_crash_reports(before: set[str], after: set[str], app_name: str) -> list[str]:
    """Crash report file names that appeared during the run and name the app."""
    return sorted(f for f in after - before if app_name.lower() in f.lower())


def run(cmd: list[str], timeout: int = 300, check: bool = False) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({r.returncode}): {r.stderr[:400]}")
    return r.stdout


class Journey:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []

    def step(self, name: str, ok: bool, t0: float, **info) -> bool:
        rec = {"step": name, "ok": bool(ok), "seconds": round(time.time() - t0, 1), **info}
        self.steps.append(rec)
        print(json.dumps(rec), flush=True)
        return ok

    def finish(self) -> int:
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps)}))
        return 0 if ok else 1

    def main(self) -> int:
        a = self.a
        reports = Path.home() / "Library" / "Logs" / "DiagnosticReports"
        before = {p.name for p in reports.glob("*")} if reports.exists() else set()

        t0 = time.time()
        device = pick_device(run(["xcrun", "simctl", "list", "devices", "available", "-j"], check=True), a.device)
        if device is None:
            self.step("simulator_booted", False, t0, error="no available iPhone simulator")
            return self.finish()
        udid = device["udid"]
        if device.get("state") != "Booted":
            run(["xcrun", "simctl", "boot", udid])
        run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=600)
        self.step("simulator_booted", True, t0, device=device["name"], runtime=device["runtime"], udid=udid)

        t0 = time.time()
        install = subprocess.run(["xcrun", "simctl", "install", udid, a.app], capture_output=True, text=True, timeout=600)
        if not self.step("installed", install.returncode == 0, t0, stderr=install.stderr[-300:]):
            return self.finish()

        t0 = time.time()
        listener = ProbeListener(a.probe_port).start()
        # A plain launch answers "<bundle id>: <pid>" or an error, so the step reports what happened
        # (run 45990580023: the attached-console launch was marked passed while the app never ran).
        launch = subprocess.run(["xcrun", "simctl", "launch", udid, a.bundle_id], capture_output=True, text=True, timeout=300)
        pid = launch_pid(launch.stdout, a.bundle_id)
        self.step("launched", launch.returncode == 0 and pid is not None, t0, bundle_id=a.bundle_id, pid=pid,
                  output=(launch.stdout + launch.stderr).strip()[-300:], probe_listener=f"127.0.0.1:{a.probe_port}")

        def app_log(minutes: int = 5) -> str:
            predicate = f"processID == {pid}" if pid else 'process == "IRACARE"'
            return run(["xcrun", "simctl", "spawn", udid, "log", "show", "--last", f"{minutes}m", "--style", "compact",
                        "--predicate", predicate], timeout=600)

        t0 = time.time()
        deadline = time.time() + a.seconds
        seen: list[str] = []
        log_now = ""
        while time.time() < deadline:
            time.sleep(10)
            log_now = app_log()
            seen = listener.screens() or screens(log_now)
            if "sign-in" in seen:
                break
        (self.out / "probe_posts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in listener.records))
        shot = self.out / "01_launched.png"
        run(["xcrun", "simctl", "io", udid, "screenshot", str(shot)])
        if "sign-in" not in seen:
            tail = [ln.strip() for ln in log_now.splitlines() if ln.strip()][-50:]
            print(json.dumps({"diagnosis": "the sign-in screen was not reported; the app's log follows",
                              "pid": pid, "app_log_tail": tail, "app_log_picked": diagnostic_lines(log_now)}), flush=True)
        self.step("sign_in_screen_shown", "sign-in" in seen, t0, screens=seen[:10], screenshot=shot.name,
                  probe_posts=len(listener.records), log_lines=len(log_now.splitlines()))

        t0 = time.time()
        listing = run(["xcrun", "simctl", "spawn", udid, "launchctl", "list"])
        running = any(a.bundle_id in ln and not ln.split()[0] == "-" for ln in listing.splitlines())
        self.step("still_running", running, t0, launchctl=[ln.strip() for ln in listing.splitlines() if a.bundle_id in ln][:3])
        listener.stop()

        t0 = time.time()
        log = run(["xcrun", "simctl", "spawn", udid, "log", "show", "--last", "5m", "--style", "compact",
                   "--predicate", 'process == "IRACARE"'], timeout=600)
        (self.out / "app_log.txt").write_text(log[-400_000:])
        after = {p.name for p in reports.glob("*")} if reports.exists() else set()
        crashes = new_crash_reports(before, after, "IRACARE")
        self.step("no_crash_report", not crashes, t0, crash_reports=crashes[:5])
        return self.finish()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--app", required=True, help="the built IRACARE.app (Debug, JavaScript bundled)")
    ap.add_argument("--out", default="build/ios-journey")
    ap.add_argument("--device", default=None, help="simulator name, e.g. 'iPhone 16' (default: a booted or the newest iPhone)")
    ap.add_argument("--bundle-id", default="care.ira.family")
    ap.add_argument("--seconds", type=int, default=60, help="how long to read the console for the sign-in screen")
    ap.add_argument("--probe-port", type=int, default=PROBE_PORT, help="the loopback port the app posts its probe to")
    return Journey(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
