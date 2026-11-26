#!/usr/bin/env python3
"""Smoke journey for the watch app on a running emulator (or a device on adb).

    python3 scripts/emulator/watch_smoke.py --apk watch/app/build/outputs/apk/wearos/debug/app-wearos-debug.apk \\
        --out build/emulator-smoke [--serial emulator-5554] \\
        [--api http://127.0.0.1:8000 --credentials .demo/demo.local.json]

Without --api it installs the debug APK, launches the main activity and checks
the pairing screen is shown with its Pair button. With --api and a seed-format
credentials file it also does the care journey against that (demo) backend:
creates a pairing code for the first fictional person as the coordinator,
types it on the watch, grants the runtime permissions with `pm grant` (as a
person would on the permission screen), checks the home screen shows the Help
button, presses Help, and checks both the wrist ("Help request received") and
the server (a new open help_requested alert for that person).

The emulator reaches the host's 127.0.0.1 as 10.0.2.2, which is where the
debug build points (CARE_API_BASE_URL in watch/app/build.gradle.kts), so the
backend must listen on host port 8000.

Every step is timed. Screenshots (PNG), UI dumps, logcat and results.json go
to --out. Exit status 0 only if every step that ran passed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PERMISSIONS = ("android.permission.BODY_SENSORS", "android.permission.ACTIVITY_RECOGNITION",
               "android.permission.RECORD_AUDIO", "android.permission.POST_NOTIFICATIONS",
               "android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION",
               "android.permission.BODY_SENSORS_BACKGROUND")

_NODE = re.compile(r"<node [^>]*>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def parse_nodes(xml: str) -> list[dict]:
    """The attributes of every node in a `uiautomator dump`."""
    return [dict(_ATTR.findall(m.group(0))) for m in _NODE.finditer(xml)]


def find(nodes: list[dict], *, text: str | None = None, desc_prefix: str | None = None,
         cls: str | None = None) -> dict | None:
    for n in nodes:
        if text is not None and n.get("text") != text:
            continue
        if desc_prefix is not None and not n.get("content-desc", "").startswith(desc_prefix):
            continue
        if cls is not None and n.get("class") != cls:
            continue
        return n
    return None


def visible_text(nodes: list[dict]) -> list[str]:
    """The texts and descriptions on screen, in order, without blanks or repeats."""
    seen: list[str] = []
    for n in nodes:
        for key in ("text", "content-desc"):
            v = n.get(key, "").strip()
            if v and v not in seen:
                seen.append(v)
    return seen


def centre(node: dict) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
    return (x1 + x2) // 2, (y1 + y2) // 2


class Smoke:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.shot_n = 0

    def adb(self, *args, timeout=300, check=True, binary=False):
        r = subprocess.run([self.a.adb, "-s", self.a.serial, *args], capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace')[:400]}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def step(self, name: str, ok: bool, t0: float, **info) -> bool:
        rec = {"step": name, "ok": bool(ok), "seconds": round(time.time() - t0, 1), **info}
        self.steps.append(rec)
        print(json.dumps(rec), flush=True)
        return ok

    def shot(self, label: str) -> str:
        self.shot_n += 1
        name = f"{self.shot_n:02d}_{label}.png"
        (self.out / name).write_bytes(self.adb("exec-out", "screencap", "-p", binary=True, timeout=120))
        return name

    def dump(self) -> list[dict]:
        # /data/local/tmp, not /sdcard: a watch image has no shared storage the shell can read back.
        self.last_dump_output = self.adb("shell", "uiautomator", "dump", "/data/local/tmp/ui.xml",
                                         timeout=180, check=False).strip()
        xml = self.adb("exec-out", "cat", "/data/local/tmp/ui.xml", timeout=60, check=False)
        self.last_xml = xml
        return parse_nodes(xml)

    def diagnose(self, label: str) -> dict:
        """What the device shows when a UI step fails, printed into the job log as well as saved,
        because the uploaded artefacts are not always reachable by whoever reads the result."""
        nodes = parse_nodes(getattr(self, "last_xml", ""))
        focus = self.adb("shell", "dumpsys", "window", "displays", timeout=60, check=False)
        crash = self.adb("logcat", "-d", "-b", "crash", timeout=60, check=False)
        app_log = self.adb("logcat", "-d", "-t", "400", timeout=60, check=False)
        info = {
            "label": label,
            "uiautomator": getattr(self, "last_dump_output", "")[-300:],
            "visible_text": visible_text(nodes)[:40],
            "packages_on_screen": sorted({n.get("package", "") for n in nodes} - {""}),
            "focus": [ln.strip() for ln in focus.splitlines() if "mCurrentFocus" in ln or "mFocusedApp" in ln][:4],
            "crash": crash.strip().splitlines()[-25:],
            "app_errors": [ln for ln in app_log.splitlines() if " E " in ln or "FATAL" in ln][-25:],
        }
        (self.out / f"diagnosis_{label}.json").write_text(json.dumps(info, indent=2) + "\n")
        print(json.dumps({"diagnosis": info}), flush=True)
        return info

    def wait_for(self, timeout: float, **kw) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = find(self.dump(), **kw)
            if n is not None:
                return n
            time.sleep(3)
        return None

    def save_dump(self, label: str) -> None:
        (self.out / f"ui_{label}.xml").write_text(getattr(self, "last_xml", ""))

    def api(self, method: str, path: str, body=None, headers=None, form=False):
        h = dict(headers or {})
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.a.api + path, data=data, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")

    def run(self) -> int:
        try:
            return self._run()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            # The device stopped answering (on a slow emulator, often system_server
            # restarting): a failed step with the reason, not a crash.
            self.step("device_error", False, time.time(), error=str(exc)[:400])
            return self.finish()

    def _run(self) -> int:
        a = self.a
        pkg = a.package
        props = {k: self.adb("shell", "getprop", k).strip() for k in
                 ("ro.build.fingerprint", "ro.build.version.sdk", "ro.product.cpu.abi", "ro.build.characteristics",
                  "sys.boot_completed")}
        (self.out / "device.json").write_text(json.dumps(props, indent=2) + "\n")
        print(json.dumps({"device": props}), flush=True)

        # A fresh emulator: mark setup done (the Wear setup wizard otherwise holds
        # the app's task back), keep the screen on and wake it. The emulator reports that
        # it is on a charger, and Wear OS then puts its charging clock (sysui
        # ChargingActivity) over every app (run 45755139976); report it unplugged, as a
        # watch on a wrist is. `stayon true` keeps the screen on without a charger.
        t0 = time.time()
        for cmd in ("settings put secure user_setup_complete 1", "settings put global device_provisioned 1",
                    "dumpsys battery unplug", "dumpsys battery set status 3",
                    "svc power stayon true", "settings put system screen_off_timeout 2147483647",
                    "input keyevent KEYCODE_WAKEUP"):
            self.adb("shell", cmd, timeout=120, check=False)
        self.step("prepare_device", True, t0)

        t0 = time.time()
        if a.skip_install:
            listed = self.adb("shell", "pm", "path", pkg, check=False)
            if not self.step("already_installed", "package:" in listed, t0):
                return self.finish()
        else:
            r = subprocess.run([a.adb, "-s", a.serial, "install", "-r", a.apk], capture_output=True, text=True, timeout=1800)
            if not self.step("install", r.returncode == 0 and "Success" in r.stdout, t0, output=r.stdout.strip()[-200:]):
                return self.finish()

        self.adb("logcat", "-c", check=False)
        t0 = time.time()
        out = self.adb("shell", "am", "start", "-W", "-n", f"{pkg}/care.ira.watch.ui.MainActivity", timeout=300)
        total = re.search(r"TotalTime: (\d+)", out)
        self.step("launch", "Status: ok" in out or total is not None, t0,
                  total_time_ms=int(total.group(1)) if total else None, am_start=out.strip()[-300:])

        t0 = time.time()
        title = self.wait_for(min(60, a.ui_timeout), text="Pair this watch")
        if title is None and "ChargingActivity" in self.adb("shell", "dumpsys", "window", "displays",
                                                              timeout=60, check=False):
            # The charging clock came up anyway: bring the app to the front once, and say so.
            self.adb("shell", "am", "start", "-n", f"{pkg}/care.ira.watch.ui.MainActivity", timeout=120, check=False)
            print(json.dumps({"note": "charging screen was in front; app brought back"}), flush=True)
        if title is None:
            title = self.wait_for(a.ui_timeout, text="Pair this watch")
        pair_btn = find(self.dump(), text="Pair") if title else None
        self.save_dump("pairing")
        shot = self.shot("pairing_screen")
        if not self.step("pairing_screen_shown", title is not None and pair_btn is not None, t0, screenshot=shot):
            return self.finish()
        if not a.api:
            return self.finish()

        # ── The care journey against the backend ─────────────────────────────
        creds = json.loads(Path(a.credentials).read_text())
        coord = next(l for l in creds["logins"] if l.get("role") == "workspace_admin")
        tok = self.api("POST", "/auth/cloud/login", {"email": coord["email"], "password": coord["password"]}, form=True)
        h = {"Authorization": f"Bearer {tok['access_token']}", "X-Workspace-ID": creds["workspace_id"]}
        person = creds["people"][0]
        t0 = time.time()
        dev = self.api("POST", f"/api/v1/care/people/{person['id']}/devices", {"platform": "wearos"}, h)
        code = dev["pairing_code"]
        self.step("pairing_code_created", bool(code), t0, device_id=dev.get("id"))

        t0 = time.time()
        field = find(self.dump(), cls="android.widget.EditText")
        if field is None:
            self.save_dump("no_field")
            return self.finish(self.step("type_code", False, t0, error="no text field in the UI dump"))
        self.adb("shell", "input", "tap", *map(str, centre(field)))
        time.sleep(2)
        self.adb("shell", "input", "text", code)
        time.sleep(3)
        btn = find(self.dump(), text="Pair")
        shot = self.shot("code_typed")
        self.step("type_code", btn is not None, t0, screenshot=shot)
        t0 = time.time()
        self.adb("shell", "input", "tap", *map(str, centre(btn)))
        gone = None
        deadline = time.time() + a.ui_timeout
        while time.time() < deadline:
            nodes = self.dump()
            if find(nodes, text="Pair this watch") is None:
                gone = True
                break
            if find(nodes, text="That code was not recognised.") or find(nodes, text="No connection. Try again."):
                gone = False
                break
            time.sleep(3)
        self.save_dump("after_pair")
        shot = self.shot("after_pair")
        server = self.api("GET", f"/api/v1/care/people/{person['id']}/devices", None, h)
        paired = [d for d in (server.get("devices", server) if isinstance(server, dict) else server)
                  if str(d.get("id")) == str(dev.get("id"))]
        if not self.step("paired", bool(gone), t0, screenshot=shot,
                         server_device=paired[0] if paired else None):
            return self.finish()

        t0 = time.time()
        granted = []
        for p in PERMISSIONS:
            r = subprocess.run([a.adb, "-s", a.serial, "shell", "pm", "grant", pkg, p], capture_output=True, text=True)
            granted.append({"permission": p, "ok": r.returncode == 0, "error": r.stderr.strip()[:160]})
        self.adb("shell", "am", "start", "-W", "-n", f"{pkg}/care.ira.watch.ui.MainActivity", timeout=300)
        help_btn = self.wait_for(a.ui_timeout, desc_prefix="Help. Tells your family")
        self.save_dump("home")
        shot = self.shot("home_help_button")
        if not self.step("home_help_button_visible", help_btn is not None, t0, screenshot=shot, permissions=granted):
            return self.finish()

        t0 = time.time()
        self.adb("shell", "input", "tap", *map(str, centre(help_btn)))
        received = None
        deadline = time.time() + a.ui_timeout
        while time.time() < deadline:
            nodes = self.dump()
            received = next((n for n in nodes if "Help request received" in n.get("text", "")
                             or "Help request received" in n.get("content-desc", "")), None)
            if received:
                break
            time.sleep(3)
        self.save_dump("after_help")
        shot = self.shot("after_help")
        alerts = self.api("GET", "/api/v1/care/alerts?open_only=true&limit=50", None, h).get("alerts", [])
        mine = [x for x in alerts if x.get("person_id") == person["id"] and x.get("kind") == "help_requested"]
        self.step("help_received_on_wrist", received is not None, t0, screenshot=shot)
        self.step("help_alert_on_server", len(mine) == 1, t0, alerts=[{k: x.get(k) for k in ("id", "kind", "status", "opened_at", "details")} for x in mine])
        return self.finish()

    def finish(self, _=None) -> int:
        try:
            (self.out / "logcat.txt").write_text(self.adb("logcat", "-d", "-v", "time", timeout=120, check=False)[-400_000:])
        except Exception as exc:   # the results still matter without the log
            print(f"logcat not saved: {exc}", file=sys.stderr)
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        if not ok:
            try:
                self.dump()
                self.diagnose(self.steps[-1]["step"] if self.steps else "start")
            except Exception as exc:   # a device that stopped answering still gets its results written
                print(f"diagnosis not taken: {exc}", file=sys.stderr)
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps)}))
        return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--package", default="care.ira.watch.debug")
    ap.add_argument("--skip-install", action="store_true", help="the APK is already installed (a re-run)")
    ap.add_argument("--ui-timeout", type=float, default=300.0)
    ap.add_argument("--api", default=None, help="the backend on host port 8000, e.g. http://127.0.0.1:8000")
    ap.add_argument("--credentials", default=None, help="seed-format JSON (scripts/demo/seed_demo.py --out)")
    a = ap.parse_args(argv)
    if a.api and not a.credentials:
        ap.error("--api needs --credentials")
    return Smoke(a).run()


if __name__ == "__main__":
    sys.exit(main())
