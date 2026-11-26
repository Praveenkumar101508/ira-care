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
import select
import subprocess
import sys
import time
from pathlib import Path

PROBE_PREFIX = "ira-journey "


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
        # --console-pty attaches the app's stdout and stderr (where the development bundle's
        # console.log arrives) until the process ends or we stop reading.
        proc = subprocess.Popen(["xcrun", "simctl", "launch", "--console-pty", udid, a.bundle_id],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.step("launched", True, t0, bundle_id=a.bundle_id)

        t0 = time.time()
        console_path = self.out / "console.txt"
        collected = []
        deadline = time.time() + a.seconds
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    collected.append(line)
                    if "sign-in" in screens(line) and time.time() - t0 < a.seconds - 10:
                        deadline = min(deadline, time.time() + 10)   # ten more seconds after the screen
            if proc.poll() is not None:
                break
        console = "".join(collected)
        console_path.write_text(console)
        shot = self.out / "01_launched.png"
        run(["xcrun", "simctl", "io", udid, "screenshot", str(shot)])
        seen = screens(console)
        self.step("sign_in_screen_shown", "sign-in" in seen, t0, screens=seen[:10], screenshot=shot.name,
                  console_lines=len(collected))

        t0 = time.time()
        alive = proc.poll() is None
        pids = run(["xcrun", "simctl", "spawn", udid, "launchctl", "list"]).count(a.bundle_id)
        self.step("still_running", alive or pids > 0, t0, console_attached=alive, launchctl_entries=pids)
        if alive:
            proc.terminate()

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
    return Journey(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
