#!/usr/bin/env python3
"""Family phone app on an Android phone emulator: install, launch, sign-in screen, and, against the
demo server, the authenticated care journey.

Without --api: the release APK (debug-signed, x86_64, its JavaScript bundled) is installed and
launched and must draw the sign-in screen without crashing. Release builds refuse plain HTTP, so
that part never talks to a server.

With --api and --credentials (the demo seed's JSON): the APK must be the DEBUG build with its
JavaScript bundled (the workflow builds it with `debuggableVariants = []`), because only the debug
build allows cleartext HTTP to the emulator's host (10.0.2.2); the release policy is untouched.
The journey then signs in as the fictional family member, opens the person's record and plan,
sees a Help alert raised by the watch and acknowledges it, talks to IRA as the wearer, changes the
appearance and sees it kept across a restart, switches to the carer's account and sees none of
the family experience, loses and regains connectivity, and signs out with the server's session
ended and nothing left on the screen. Each step is recorded with screenshots, UI dumps and the
server's view; `--api-only` plays the server side alone (no device) to prove the calls.

Usage (a booted emulator and adb on PATH):
    python3 scripts/emulator/phone_smoke.py --apk mobile/android/app/build/outputs/apk/release/app-release.apk \
        --out build/phone-smoke
    python3 scripts/emulator/phone_smoke.py --apk mobile/android/app/build/outputs/apk/debug/app-debug.apk \
        --out build/phone-journey --api http://127.0.0.1:8000 --credentials .demo/demo.local.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adbkit import (  # noqa: E402
    ALL_WELL_WORDS, STALE_WORDS, adb_text, app_sessions, carer_home_problems, find_id, has_text, is_selected,
    nothing_revealed_problems, offline_problems, person_record_problems, plan_problems, recovered_problems,
    revocation_problems,
)
from watch_smoke import app_crashes, centre, find, parse_nodes, visible_text  # noqa: E402

#: The user agent of this script's own API calls; the app's sessions are the others (adbkit.app_sessions).
OWN_AGENT = "ira-care-phone-journey/1"

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


#: How many times the smoke answers another app's dialog or brings the app back.
MAX_RECOVERIES = 3


def anr_answer(recovery: int) -> str:
    """Which button to press on another app's "isn't responding" dialog at the n-th recovery.
    The first time, Wait: the app may just be slow after boot. From the second time on, Close
    app: a launcher that is still hung after a wait and a relaunch (run 45903217691: three
    Waits, 240 s, the dialog back each time) is closed, Android restarts it, and our app is
    brought to the front. The hung app is never ours (foreign_anr), so nothing of IRA's is closed."""
    return "Wait" if recovery <= 1 else "Close app"


def foreign_anr(nodes: list[dict]) -> str | None:
    """The text of a "... isn't responding" dialog for an app other than ours, if one is shown."""
    for n in nodes:
        t = n.get("text", "")
        if "isn't responding" in t and "IRA CARE" not in t:
            return t
    return None


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

    def app_in_front(self) -> bool:
        focus = self.adb("shell", "dumpsys", "window", "displays", timeout=60)
        return any(PACKAGE in ln for ln in focus.splitlines() if "mCurrentFocus" in ln)

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
        recoveries = 0
        deadline = time.time() + a.ui_timeout
        while time.time() < deadline:
            nodes = self.dump()
            problems = sign_in_screen_problems(nodes)
            if not problems:
                break
            # A freshly booted emulator's own apps can hang ("Pixel Launcher isn't responding",
            # run 45766023517) and their dialog covers the screen; answer Wait and bring the app
            # back, at most MAX_RECOVERIES times, each noted.
            other = foreign_anr(nodes)
            if recoveries < MAX_RECOVERIES and (other or not self.app_in_front()):
                recoveries += 1
                answer = anr_answer(recoveries)
                button = find(nodes, text=answer)
                if other and button is not None:
                    self.adb("shell", "input", "tap", *map(str, centre(button)))
                    time.sleep(2)
                self.adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1", timeout=120)
                print(json.dumps({"note": f"recovered the screen ({recoveries})", "dialog": other,
                                  "answered": answer if other else None}), flush=True)
            time.sleep(3)
        (self.out / "ui_sign_in.xml").write_text(self.last_xml)
        shot = self.shot("sign_in")
        self.step("sign_in_screen_shown", not problems, t0, screenshot=shot, missing=problems,
                  visible_text=visible_text(parse_nodes(self.last_xml))[:20])

        if a.api and a.credentials and not problems:
            self.journey()

        t0 = time.time()
        crashes = app_crashes(self.adb("logcat", "-d", "-b", "crash", timeout=60), PACKAGE)
        self.step("no_app_crash", not crashes, t0, crashes=crashes[:5])
        return self.finish()

    # ── the server side ──────────────────────────────────────────────────────

    def api(self, method: str, path: str, body=None, headers: dict | None = None, *, form: bool = False):
        data = None
        hdrs = {"User-Agent": OWN_AGENT, **(headers or {})}
        if body is not None:
            data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        req = urllib.request.Request(self.a.api.rstrip("/") + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - the local demo server
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:300]}") from e

    def sign_in_api(self, login: dict) -> dict:
        tok = self.api("POST", "/auth/cloud/login", {"email": login["email"], "password": login["password"]}, form=True)
        return {"Authorization": f"Bearer {tok['access_token']}", "X-Workspace-ID": self.creds["workspace_id"]}

    def load_credentials(self) -> None:
        self.creds = json.loads(Path(self.a.credentials).read_text())
        by_key = {l["key"]: l for l in self.creds["logins"]}
        self.family, self.wearer, self.coordinator = by_key["family"], by_key["wearer"], by_key["coordinator"]
        self.person = dict(self.creds["people"][0])
        self.h_coord = self.sign_in_api(self.coordinator)
        self.h_family = self.sign_in_api(self.family)
        # The app shows the person's preferred name (PersonScreen, HomeTab); the seed file carries
        # only the display name, so the record is read for it (run 45910155640: "Ada" was shown and
        # "Ada Hartley" looked for).
        try:
            record = self.api("GET", f"/api/v1/care/people/{self.person['id']}", None, self.h_coord)
            self.person.setdefault("preferred_name", record.get("preferred_name") or "")
        except RuntimeError:
            pass
        self.shown_name = self.person.get("preferred_name") or self.person["display_name"].split()[0]

    def person_path(self, tail: str) -> str:
        return f"/api/v1/care/people/{self.person['id']}/{tail}"

    def watch_help(self) -> dict:
        """The watch presses Help: the wearer's device token, an idempotency key of its own."""
        return self.api("POST", "/api/v1/care/devices/me/help", {"via": "button", "note": "phone journey"},
                        {"X-Device-Token": self.person["device_token"], "X-Idempotency-Key": str(uuid.uuid4())})

    def open_help_alerts(self) -> list[dict]:
        alerts = self.api("GET", f"/api/v1/care/alerts?open_only=true&person_id={self.person['id']}&limit=50",
                          None, self.h_coord).get("alerts", [])
        return [x for x in alerts if x.get("kind") == "help_requested"]

    def resolve(self, alert_id: str) -> None:
        self.api("POST", f"/api/v1/care/alerts/{alert_id}/resolve",
                 {"resolution": "phone journey: closed by the coordinator", "outcome": "all_well"}, self.h_coord)

    def family_sessions(self) -> list[dict]:
        return app_sessions(self.api("GET", "/auth/cloud/sessions", None, self.h_family).get("sessions", []), OWN_AGENT)

    # ── driving the app ──────────────────────────────────────────────────────

    def tap_id(self, rid: str, nodes: list[dict] | None = None) -> bool:
        node = find_id(nodes or self.dump(), rid)
        if node is None:
            return False
        self.adb("shell", "input", "tap", *map(str, centre(node)))
        return True

    def type_into(self, rid: str, text: str) -> bool:
        if not self.tap_id(rid):
            return False
        time.sleep(1)
        self.adb("shell", "input", "text", adb_text(text))
        self.adb("shell", "input", "keyevent", "KEYCODE_BACK")   # the keyboard, not the screen
        return True

    def wait_for(self, check, seconds: float) -> list[dict]:
        """Dump until `check(nodes)` is true or the time is up; the last dump either way."""
        deadline = time.time() + seconds
        nodes = self.dump()
        while not check(nodes) and time.time() < deadline:
            time.sleep(3)
            nodes = self.dump()
        return nodes

    def relaunch(self) -> None:
        self.adb("shell", "am", "force-stop", PACKAGE)
        time.sleep(2)
        self.adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1", timeout=120)

    def sign_in_app(self, login: dict, label: str) -> bool:
        t0 = time.time()
        ok = self.type_into("username", login["email"]) and self.type_into("password", login["password"]) \
            and self.tap_id("sign-in")
        if ok:
            nodes = self.wait_for(lambda n: find_id(n, "tab-home") is not None or find_id(n, "tab-settings") is not None
                                  or any(x.get("resource-id", "").endswith("workspace-" + self.creds["workspace_id"]) for x in n),
                                  self.a.ui_timeout)
            if find_id(nodes, "tab-home") is None and self.tap_id("workspace-" + self.creds["workspace_id"], nodes):
                nodes = self.wait_for(lambda n: find_id(n, "tab-home") is not None, self.a.ui_timeout)
            ok = find_id(nodes, "tab-home") is not None
        return self.step(f"{label}_signed_in", ok, t0, screenshot=self.shot(f"{label}_home"),
                         visible_text=visible_text(parse_nodes(self.last_xml))[:12])

    def scroll_to_id(self, rid: str, swipes: int = 4) -> bool:
        """Scroll a list until a test id is on screen (uiautomator dumps only what is drawn)."""
        for _ in range(swipes + 1):
            if find_id(self.dump(), rid) is not None:
                return True
            self.adb("shell", "input", "swipe", "540", "1900", "540", "700", "300")
            time.sleep(1.5)
        return find_id(self.dump(), rid) is not None

    def sign_out_app(self) -> list[dict]:
        """Settings, the Account section, Sign out (run 45910155640: the button lives inside the
        Account section and below the fold, so it was never found)."""
        # An overlay (Talk, the pairing guide) hides the tab bar: go back until it shows
        # (run 45974160883: the carer's sign-in started on the wearer's Talk screen).
        for _ in range(4):
            if find_id(self.dump(), "tab-settings") is not None:
                break
            self.adb("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(1.5)
        self.tap_id("tab-settings")
        time.sleep(2)
        if self.scroll_to_id("settings-account"):
            self.tap_id("settings-account")
            time.sleep(2)
        if self.scroll_to_id("sign-out"):
            self.tap_id("sign-out")
        return self.wait_for(lambda n: find_id(n, "sign-in") is not None, self.a.ui_timeout)

    def journey(self) -> None:
        a = self.a
        self.load_credentials()
        name = self.shown_name

        # 1. The family member signs in and the person is on the home screen.
        if not self.sign_in_app(self.family, "family"):
            return
        t0 = time.time()
        nodes = self.wait_for(lambda n: find_id(n, "card-person") is not None, a.ui_timeout)
        self.step("patient_selected", find_id(nodes, "card-person") is not None and
                  (has_text(nodes, name) or has_text(nodes, self.person["display_name"])), t0,
                  screenshot=self.shot("family_person_card"))

        # 2. The person record shows the right name and the latest readings the API reports.
        t0 = time.time()
        status = self.api("GET", self.person_path("status"), None, self.h_coord)
        self.tap_id("home-care")
        nodes = self.wait_for(lambda n: find_id(n, "tab-plan") is not None, a.ui_timeout)
        problems = person_record_problems(nodes, name, status)
        self.step("person_record_shown", not problems, t0, missing=problems, screenshot=self.shot("person_record"),
                  readings=status.get("latest_readings"))

        # 3. Medication and the care plan.
        t0 = time.time()
        meds = self.api("GET", self.person_path("medications"), None, self.h_coord)
        meds = meds.get("medications", meds) if isinstance(meds, dict) else meds
        try:
            plan = self.api("GET", self.person_path("plan"), None, self.h_coord) or None
        except RuntimeError:
            plan = None
        self.tap_id("tab-plan")
        nodes = self.wait_for(lambda n: find_id(n, "medication-card") is not None, a.ui_timeout)
        problems = plan_problems(nodes, meds, plan)
        self.step("plan_shown", not problems, t0, missing=problems, screenshot=self.shot("plan"))

        # 4. The watch presses Help; the family member sees it and says they are on their way.
        t0 = time.time()
        for stale in self.open_help_alerts():
            self.resolve(stale["id"])
        self.watch_help()
        self.tap_id("tab-home")
        nodes = self.wait_for(lambda n: find_id(n, "urgent-banner") is not None, max(a.ui_timeout, 90))
        alerts = self.open_help_alerts()
        self.step("help_alert_shown", find_id(nodes, "urgent-banner") is not None and len(alerts) == 1, t0,
                  screenshot=self.shot("help_banner"), open_help_alerts=len(alerts))
        t0 = time.time()
        acknowledged = None
        if alerts and self.tap_id("home-on-my-way", nodes):
            deadline = time.time() + a.ui_timeout
            while time.time() < deadline:
                acknowledged = self.api("GET", f"/api/v1/care/alerts/{alerts[0]['id']}", None, self.h_coord)
                if acknowledged.get("status") == "acknowledged":
                    break
                time.sleep(3)
        self.step("acknowledged_in_app", bool(acknowledged) and acknowledged.get("status") == "acknowledged", t0,
                  screenshot=self.shot("acknowledged"), alert={k: acknowledged.get(k) for k in
                                                              ("id", "status", "acknowledged_by", "acknowledged_at")} if acknowledged else None)
        for al in self.open_help_alerts():
            self.resolve(al["id"])

        # 5. Appearance: dark mode is kept across a restart.
        t0 = time.time()
        self.tap_id("tab-settings")
        time.sleep(2)
        if self.scroll_to_id("settings-appearance"):
            self.tap_id("settings-appearance")
        nodes = self.wait_for(lambda n: find_id(n, "mode-dark") is not None, a.ui_timeout)
        self.tap_id("mode-dark", nodes)
        time.sleep(2)
        chosen = is_selected(find_id(self.dump(), "mode-dark"))
        self.relaunch()
        nodes = self.wait_for(lambda n: find_id(n, "tab-settings") is not None, a.ui_timeout)
        self.tap_id("tab-settings", nodes)
        time.sleep(2)
        self.tap_id("settings-appearance")
        nodes = self.wait_for(lambda n: find_id(n, "mode-dark") is not None, a.ui_timeout)
        kept = is_selected(find_id(nodes, "mode-dark"))
        self.step("appearance_kept_across_restart", chosen and kept, t0, chosen=chosen, kept=kept,
                  screenshot=self.shot("appearance_after_restart"))
        self.tap_id("mode-system", nodes)
        time.sleep(1)
        self.tap_id("settings-back")

        # 6. Connectivity lost: the home screen says so and never says all is well; then it recovers.
        t0 = time.time()
        self.adb("shell", "svc", "wifi", "disable")
        self.adb("shell", "svc", "data", "disable")
        self.tap_id("tab-home")
        nodes = self.wait_for(lambda n: has_text(n, STALE_WORDS), max(a.ui_timeout, 90))
        problems = offline_problems(nodes)
        self.step("offline_state_shown", not problems, t0, missing=problems, screenshot=self.shot("offline"))
        t0 = time.time()
        self.adb("shell", "svc", "wifi", "enable")
        self.adb("shell", "svc", "data", "enable")
        nodes = self.wait_for(lambda n: has_text(n, ALL_WELL_WORDS), max(a.ui_timeout, 90))
        problems = recovered_problems(nodes)
        self.step("connectivity_recovered", not problems, t0, missing=problems, screenshot=self.shot("recovered"))

        # 7. Sign out: the server's session for this phone is ended, nothing is left on screen.
        t0 = time.time()
        before = self.family_sessions()
        nodes = self.sign_out_app()
        after = self.family_sessions()
        problems = revocation_problems(before, after)
        self.adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(2)
        back = self.dump()
        problems += nothing_revealed_problems(back, (self.family["name"], name))
        self.relaunch()
        nodes = self.wait_for(lambda n: find_id(n, "sign-in") is not None, a.ui_timeout)
        if find_id(nodes, "sign-in") is None:
            problems.append("sign-in screen not shown after a restart")
        self.step("secure_sign_out", not problems, t0, missing=problems, sessions_before=len(before),
                  sessions_after_live=sum(1 for x in after if not x.get("revoked_at")), screenshot=self.shot("signed_out"))

        # 8. The wearer talks to IRA on the phone (C-7.b: only the wearer may).
        if self.sign_in_app(self.wearer, "wearer"):
            t0 = time.time()
            nodes = self.wait_for(lambda n: find_id(n, "home-talk") is not None, a.ui_timeout)
            self.tap_id("home-talk", nodes)
            nodes = self.wait_for(lambda n: find_id(n, "talk-input") is not None, a.ui_timeout)
            self.type_into("talk-input", "Hello IRA, I had my breakfast")
            self.tap_id("talk-send")
            nodes = self.wait_for(lambda n: any(x.get("resource-id", "").split("/")[-1].startswith("talk-ira-") for x in n),
                                  max(a.ui_timeout, 60))
            replies = [x.get("text", "") for x in nodes if x.get("resource-id", "").split("/")[-1].startswith("talk-ira-")]
            self.step("wearer_talked_to_ira", bool(replies), t0, reply=replies[:1], screenshot=self.shot("talk"))
            self.sign_out_app()

        # 9. The carer signs in: the carer's home, none of the family experience.
        if self.sign_in_app(self.coordinator, "carer"):
            t0 = time.time()
            nodes = self.wait_for(lambda n: find_id(n, "card-people") is not None, a.ui_timeout)
            problems = carer_home_problems(nodes)
            self.step("carer_home_isolated", not problems, t0, missing=problems, screenshot=self.shot("carer_home"))
            self.sign_out_app()

    def api_only(self) -> int:
        """No device: the server side of every step, to prove the calls and the seed."""
        self.a.credentials and self.load_credentials()
        t0 = time.time()
        status = self.api("GET", self.person_path("status"), None, self.h_coord)
        self.step("api_status", bool(status.get("latest_readings")), t0, readings=len(status.get("latest_readings") or []))
        t0 = time.time()
        meds = self.api("GET", self.person_path("medications"), None, self.h_coord)
        meds = meds.get("medications", meds) if isinstance(meds, dict) else meds
        plan = self.api("GET", self.person_path("plan"), None, self.h_coord)
        self.step("api_plan", bool(meds) and bool(plan), t0, medications=len(meds), plan_goals=bool(plan and plan.get("goals")))
        t0 = time.time()
        for stale in self.open_help_alerts():
            self.resolve(stale["id"])
        self.watch_help()
        alerts = self.open_help_alerts()
        acked = self.api("POST", f"/api/v1/care/alerts/{alerts[0]['id']}/acknowledge", {"note": "", "kind": "on_my_way"},
                         self.h_family) if alerts else {}
        self.step("api_help_and_acknowledge", len(alerts) == 1 and acked.get("status") == "acknowledged", t0,
                  alert_status=acked.get("status"))
        for al in self.open_help_alerts():
            self.resolve(al["id"])
        t0 = time.time()
        sessions = self.family_sessions()
        self.step("api_sessions_listed", isinstance(sessions, list), t0, app_sessions=len(sessions))
        return self.finish()

    def finish(self) -> int:
        if not self.a.api_only:
            (self.out / "logcat.txt").write_text(self.adb("logcat", "-d", "-v", "time", timeout=120)[-400_000:])
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps)}))
        return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apk", help="the APK to install (required unless --api-only)")
    ap.add_argument("--out", default="build/phone-smoke")
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--ui-timeout", type=float, default=240.0)
    ap.add_argument("--api", help="the demo server as this machine reaches it, e.g. http://127.0.0.1:8000; "
                                  "the app reaches it as 10.0.2.2 (the debug build only)")
    ap.add_argument("--credentials", help="seed-format JSON (scripts/demo/seed_demo.py --out)")
    ap.add_argument("--api-only", action="store_true", help="no device: play the server side of the journey")
    a = ap.parse_args(argv)
    if a.api_only:
        if not (a.api and a.credentials):
            ap.error("--api-only needs --api and --credentials")
        return PhoneSmoke(a).api_only()
    if not a.apk:
        ap.error("--apk is required")
    return PhoneSmoke(a).run()


if __name__ == "__main__":
    sys.exit(main())
