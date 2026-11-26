#!/usr/bin/env python3
"""Family phone app smoke on an Android phone emulator: install, launch, sign-in screen, no crash.

The release APK (debug-signed, x86_64) bundles its JavaScript, so it runs with no development
server. Release builds refuse plain HTTP, so this smoke does not sign in to the local demo
server; it shows that the native app and its JavaScript start on Android and draw the sign-in
screen without crashing. The watch journey (watch_smoke.py) covers pairing and Help against the
real server.

Usage (on a machine with a booted emulator and adb on PATH):
    python3 scripts/emulator/phone_smoke.py --apk mobile/android/app/build/outputs/apk/release/app-release.apk \
        --out build/phone-smoke
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from watch_smoke import app_crashes, find, parse_nodes, visible_text  # noqa: E402

PACKAGE = "care.ira.family"
#: What the sign-in screen must show (mobile/src/screens/LoginScreen.tsx).
SIGN_IN_TEXT = "Sign in with the account your care circle gave you."


def sign_in_screen_problems(nodes: list[dict]) -> list[str]:
    """What is missing from the sign-in screen, by its texts and accessibility labels."""
    problems = []
    if find(nodes, text=SIGN_IN_TEXT) is None:
        problems.append("no sign-in explanation")
    for label in ("Username", "Password"):
        if not any(n.get("text") == label or n.get("content-desc") == label or n.get("hint") == label
                   for n in nodes):
            problems.append(f"no {label} field")
    if not any("Sign in" == n.get("text") or n.get("content-desc", "").startswith("Sign in") for n in nodes):
        problems.append("no Sign in button")
    return problems


class PhoneSmoke:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.last_xml = ""

    def adb(self, *args, timeout=300, check=False, binary=False):
        r = subprocess.run([self.a.adb, "-s", self.a.serial, *args], capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace')[:400]}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def step(self, name: str, ok: bool, t0: float, **info) -> bool:
        rec = {"step": name, "ok": bool(ok), "seconds": round(time.time() - t0, 1), **info}
        self.steps.append(rec)
        print(json.dumps(rec), flush=True)
        return ok

    def dump(self) -> list[dict]:
        self.adb("shell", "uiautomator", "dump", "/data/local/tmp/ui.xml", timeout=180)
        self.last_xml = self.adb("exec-out", "cat", "/data/local/tmp/ui.xml", timeout=60)
        return parse_nodes(self.last_xml)

    def shot(self, label: str) -> str:
        name = f"{len(self.steps) + 1:02d}_{label}.png"
        (self.out / name).write_bytes(self.adb("exec-out", "screencap", "-p", binary=True, timeout=120))
        return name

    def run(self) -> int:
        a = self.a
        props = {k: self.adb("shell", "getprop", k).strip() for k in
                 ("ro.build.fingerprint", "ro.build.version.sdk", "ro.product.cpu.abi")}
        print(json.dumps({"device": props}), flush=True)
        self.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self.adb("shell", "wm", "dismiss-keyguard")

        t0 = time.time()
        r = subprocess.run([a.adb, "-s", a.serial, "install", "-r", a.apk], capture_output=True, text=True, timeout=1800)
        if not self.step("install", r.returncode == 0 and "Success" in r.stdout, t0, output=r.stdout.strip()[-200:]):
            return self.finish()

        self.adb("logcat", "-c")
        t0 = time.time()
        out = self.adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1", timeout=120)
        self.step("launch", "Events injected: 1" in out, t0, monkey=out.strip()[-160:])

        t0 = time.time()
        problems = ["not checked"]
        deadline = time.time() + a.ui_timeout
        while time.time() < deadline:
            problems = sign_in_screen_problems(self.dump())
            if not problems:
                break
            time.sleep(3)
        (self.out / "ui_sign_in.xml").write_text(self.last_xml)
        shot = self.shot("sign_in")
        self.step("sign_in_screen_shown", not problems, t0, screenshot=shot, missing=problems,
                  visible_text=visible_text(parse_nodes(self.last_xml))[:20])

        t0 = time.time()
        crashes = app_crashes(self.adb("logcat", "-d", "-b", "crash", timeout=60), PACKAGE)
        self.step("no_app_crash", not crashes, t0, crashes=crashes[:5])
        return self.finish()

    def finish(self) -> int:
        (self.out / "logcat.txt").write_text(self.adb("logcat", "-d", "-v", "time", timeout=120)[-400_000:])
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps)}))
        return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out", default="build/phone-smoke")
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--ui-timeout", type=float, default=240.0)
    return PhoneSmoke(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
