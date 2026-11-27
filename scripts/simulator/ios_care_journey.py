#!/usr/bin/env python3
"""The family app's authenticated care journey in the iOS simulator (register D2.h).

    python3 scripts/simulator/ios_care_journey.py --udid <simulator> --api http://127.0.0.1:8000 \\
        --credentials .demo/demo.local.json --out build/ios-care-journey \\
        --api-cmd "python3 scripts/demo/api.py"

The same journey as the Android one (scripts/emulator/phone_smoke.py `journey`), on iOS:
the family member signs in; the person and their record; medication and the plan; the watch
presses Help and the family member acknowledges it in the app; dark mode kept across a restart;
the care server unreachable and back; sign-out with the server's session ended; the wearer talks
to IRA; the carer's home, isolated from the family experience. The server side is the Android
script's own (`phone_smoke.PhoneSmoke`'s API helpers), and every screen check is the same pure
function (`adbkit`), so both platforms are held to one standard.

How iOS is driven: Maestro (an open-source UI driver) runs one small flow per action against
the simulator, and `maestro hierarchy` gives the screen's accessibility tree, which
`nodes_from_hierarchy` turns into the node form the shared checks read (React Native's testID is
iOS's accessibility identifier, Maestro's `resource-id`). The one difference from Android: a
simulator's network cannot be switched off, so "connection lost" is the care server stopped
(`--api-cmd` starts it again); the result records that.

Every function that parses or builds is pure and tested
(supracloud-jarvis/ira/tests/readiness/test_ios_care_journey.py); only `Journey` runs tools.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "emulator"))
from adbkit import (  # noqa: E402
    ALL_WELL_WORDS, STALE_WORDS, carer_home_problems, find_id, has_text, is_selected, nothing_revealed_problems,
    offline_problems, person_record_problems, plan_problems, recovered_problems, revocation_problems,
)

BUNDLE_ID = "care.ira.family"


# ── pure parts ───────────────────────────────────────────────────────────────

def nodes_from_hierarchy(tree: dict) -> list[dict]:
    """Maestro's `hierarchy` JSON as a flat list of nodes in the uiautomator form the shared checks
    read: `text` (the visible text, else the accessibility label), `content-desc` (the label),
    `resource-id` (the accessibility identifier, React Native's testID), `selected`."""
    out: list[dict] = []

    def walk(n: dict) -> None:
        a = n.get("attributes") or {}
        label = a.get("accessibilityText") or ""
        text = a.get("text") or a.get("title") or a.get("value") or label
        node = {"text": text, "content-desc": label, "resource-id": a.get("resource-id") or a.get("identifier") or "",
                "selected": str(a.get("selected", "false")).lower(), "bounds": a.get("bounds", "")}
        if any(node[k] for k in ("text", "content-desc", "resource-id")):
            out.append(node)
        for c in n.get("children") or []:
            walk(c)

    walk(tree)
    return out


def _q(s: str) -> str:
    return json.dumps(s)   # YAML accepts JSON strings, which quotes every character safely


def flow(steps: list[dict], bundle_id: str = BUNDLE_ID) -> str:
    """A Maestro flow from steps: {"tap": id} (with "up": True to scroll up to it), {"type": [id, text]}, {"launch": True},
    {"stop": True}, {"back": True}, {"scroll_to": id}, {"tap_text": text}."""
    lines = [f"appId: {bundle_id}", "---"]

    def into_view(rid: str, direction: str = "DOWN") -> list[str]:
        # Maestro taps only what is on screen: on a small iPhone the sign-in fields sit below the
        # fold once "Advanced" is open (run 46032445452), so every element is scrolled to first.
        # Half visible is enough (the keyboard can cover the lower part of a field), and the element
        # is centred so the tap that follows lands on it (run 46040834373).
        # Optional: when the scroll cannot confirm the element (iOS secure fields report their
        # visibility oddly: run 46045629293 stopped at the password field), the tap that follows
        # still tries it, and a truly absent element fails there.
        return ["- scrollUntilVisible:", "    element:", f"      id: {_q(rid)}", f"    direction: {direction}", "    timeout: 10000",
                "    visibilityPercentage: 50", "    centerElement: true", "    optional: true"]

    for s in steps:
        if "tap" in s:
            lines += into_view(s["tap"], "UP" if s.get("up") else "DOWN") + ["- tapOn:", f"    id: {_q(s['tap'])}"]
        elif "tap_text" in s:
            lines += ["- tapOn:", f"    text: {_q(s['tap_text'])}"]
        elif "type" in s:
            rid, text = s["type"]
            # Not Maestro's hideKeyboard: on iOS it taps elsewhere on the screen, and on the sign-in
            # screen it hit "I have an invitation link" (run 46053932767 showed the Join screen). The
            # keyboard is dismissed by tapping the screen's heading, which has no action.
            # Cleared first: Maestro types after what a field holds, and the server field holds the
            # app's default address (run 46057616511 signed in to "http://10.0.2.2:8000http://...").
            # The keyboard is closed with Return: a single-line React Native field ends editing on it,
            # and the sign-in fields submit nothing on Return. Tapping the heading did not close it, so
            # the next tap landed on the keyboard (run 46060791832: the server address went into the
            # password field).
            lines += into_view(rid) + ["- tapOn:", f"    id: {_q(rid)}", "- eraseText: 80", f"- inputText: {_q(text)}",
                                       "- pressKey: Enter"]
        elif "launch" in s:
            lines += ["- launchApp:", "    stopApp: false"]
        elif "stop" in s:
            lines += ["- stopApp"]
        elif "scroll_to" in s:
            lines += ["- scrollUntilVisible:", "    element:", f"      id: {_q(s['scroll_to'])}", "    direction: DOWN",
                      "    timeout: 15000"]
        else:
            raise ValueError(f"unknown flow step {s!r}")
    return "\n".join(lines) + "\n"


# ── the journey ──────────────────────────────────────────────────────────────

class Journey:
    """The API side comes from the Android journey (`phone_smoke.PhoneSmoke`), unchanged."""

    def __init__(self, a):
        import phone_smoke
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.api_proc: subprocess.Popen | None = None
        self.server = phone_smoke.PhoneSmoke(argparse.Namespace(out=str(self.out / "api"), api=a.api, credentials=a.credentials))

    # recording
    def step(self, name: str, ok: bool, t0: float, **info) -> bool:
        rec = {"step": name, "ok": bool(ok), "seconds": round(time.time() - t0, 1), **info}
        if not ok and "screen" not in rec:
            # What was on screen when the step failed, in the job log: the artifacts cannot always be
            # fetched (run 46071161313's plan step failed with nothing to show why).
            rec["screen"] = [{k: x[k] for k in ("resource-id", "text") if x.get(k)} for x in self.nodes()
                             if x.get("resource-id")][:60]
        self.steps.append(rec)
        print(json.dumps(rec), flush=True)
        return ok

    def finish(self) -> int:
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps), "passed": sum(s["ok"] for s in self.steps)}))
        return 0 if ok else 1

    # the device
    def maestro(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run([self.a.maestro, "--device", self.a.udid, *args], capture_output=True, text=True, timeout=timeout)

    def run(self, steps: list[dict]) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(flow(steps))
        r = self.maestro("test", f.name)
        os.unlink(f.name)
        if r.returncode != 0:
            # Which command failed, the screen as it was, and Maestro's own record of it.
            said = [ln.strip() for ln in (r.stdout + r.stderr).splitlines() if "COMPLETED" in ln or "FAILED" in ln]
            n = len(self.steps)
            screen = self.nodes()
            (self.out / f"failed_{n:02d}_screen.json").write_text(json.dumps(screen, indent=1))
            # The screen goes into the job log too: artifacts cannot always be fetched.
            print(json.dumps({"maestro_failed": steps, "commands": said[-20:], "screenshot": self.shot("failed"),
                              "screen": [{k: x[k] for k in ("resource-id", "text", "content-desc") if x.get(k)} for x in screen][:60]}),
                  flush=True)
        return r.returncode == 0

    def nodes(self) -> list[dict]:
        r = self.maestro("hierarchy", timeout=120)
        try:
            start = r.stdout.index("{")
            return nodes_from_hierarchy(json.loads(r.stdout[start:]))
        except (ValueError, json.JSONDecodeError):
            return []

    def wait_for(self, check, seconds: float) -> list[dict]:
        deadline = time.time() + seconds
        nodes = self.nodes()
        while not check(nodes) and time.time() < deadline:
            time.sleep(3)
            nodes = self.nodes()
        return nodes

    def shot(self, name: str) -> str:
        path = self.out / f"{len(self.steps):02d}_{name}.png"
        subprocess.run(["xcrun", "simctl", "io", self.a.udid, "screenshot", str(path)], capture_output=True, timeout=60)
        return path.name

    def relaunch(self) -> None:
        self.run([{"stop": True}, {"launch": True}])

    # the care server, started here so the journey can stop it for the "connection lost" step
    def start_api(self) -> bool:
        if not self.a.api_cmd:
            return True
        log = open(self.out / "api.log", "a")
        self.api_proc = subprocess.Popen(shlex.split(self.a.api_cmd), stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        for _ in range(60):
            try:
                with urllib.request.urlopen(self.a.api.rstrip("/") + "/health", timeout=3):  # noqa: S310 - loopback
                    return True
            except OSError:
                time.sleep(2)
        return False

    def stop_api(self) -> None:
        if self.api_proc is not None:
            os.killpg(self.api_proc.pid, signal.SIGTERM)
            self.api_proc.wait(timeout=60)
            self.api_proc = None

    # the app
    def sign_in(self, login: dict, label: str) -> bool:
        t0 = time.time()
        nodes = self.wait_for(lambda n: find_id(n, "sign-in") is not None, self.a.ui_timeout)
        # Username and password first, then the server behind "Advanced" (below them on screen).
        steps = [{"type": ["username", login["email"]]}, {"type": ["password", login["password"]]}]
        # The test build's default server is this one (the workflow sets it, as the Android test build's
        # is the emulator's host), so the server field is typed only when it holds something else.
        # Typed only with --type-server: on iOS the field could not be cleared reliably (run 46064671882
        # signed in to "http://127.0.0.1:8000http://127.0.0.1"), and the test build's default is already
        # this server.
        if self.a.type_server:
            server = find_id(nodes, "server")
            steps += [] if server is not None else [{"tap": "advanced"}]
            steps += [{"type": ["server", self.a.api]}]
        # On screen: username, password, Sign in, then Advanced and the server; so back up to Sign in.
        steps += [{"tap": "sign-in", "up": True}]
        ok = self.run(steps)
        ws = "workspace-" + self.server.creds["workspace_id"]
        nodes = self.wait_for(lambda n: find_id(n, "tab-home") is not None or find_id(n, ws) is not None, self.a.ui_timeout)
        if find_id(nodes, "tab-home") is None and find_id(nodes, ws) is not None:
            self.run([{"tap": ws}])
            nodes = self.wait_for(lambda n: find_id(n, "tab-home") is not None, self.a.ui_timeout)
        signed_in = ok and find_id(nodes, "tab-home") is not None
        extra = {} if signed_in else {"screen": [{k: x[k] for k in ("resource-id", "text") if x.get(k)} for x in nodes][:40]}
        return self.step(f"{label}_signed_in", signed_in, t0, screenshot=self.shot(f"{label}_home"), **extra)

    def sign_out(self) -> list[dict]:
        for _ in range(3):
            nodes = self.nodes()
            if find_id(nodes, "tab-settings") is not None:
                break
            self.run([{"tap_text": "Back"}])
        self.run([{"tap": "tab-settings"}, {"scroll_to": "settings-account"}, {"tap": "settings-account"},
                  {"scroll_to": "sign-out"}, {"tap": "sign-out"}])
        return self.wait_for(lambda n: find_id(n, "sign-in") is not None, self.a.ui_timeout)

    def main(self) -> int:
        a, srv = self.a, self.server
        t0 = time.time()
        if not self.step("care_server_started", self.start_api(), t0, api=a.api):
            return self.finish()
        srv.load_credentials()
        name = srv.shown_name
        t0 = time.time()
        self.step("launched", self.run([{"launch": True}]), t0)

        # 1. The family member signs in; the person is on the home screen.
        if not self.sign_in(srv.family, "family"):
            self.stop_api()
            return self.finish()
        t0 = time.time()
        nodes = self.wait_for(lambda n: find_id(n, "card-person") is not None, a.ui_timeout)
        self.step("patient_selected", find_id(nodes, "card-person") is not None and
                  (has_text(nodes, name) or has_text(nodes, srv.person["display_name"])), t0, screenshot=self.shot("person_card"))

        # 2. The person record: the name and every latest reading the API reports.
        t0 = time.time()
        status = srv.api("GET", srv.person_path("status"), None, srv.h_coord)
        self.run([{"tap": "home-care"}])
        nodes = self.wait_for(lambda n: find_id(n, "tab-plan") is not None, a.ui_timeout)
        problems = person_record_problems(nodes, name, status)
        self.step("person_record_shown", not problems, t0, missing=problems, screenshot=self.shot("person_record"))

        # 3. Medication and the care plan.
        t0 = time.time()
        meds = srv.api("GET", srv.person_path("medications"), None, srv.h_coord)
        meds = meds.get("medications", meds) if isinstance(meds, dict) else meds
        try:
            plan = srv.api("GET", srv.person_path("plan"), None, srv.h_coord) or None
        except RuntimeError:
            plan = None
        self.run([{"tap": "tab-plan"}])
        nodes = self.wait_for(lambda n: find_id(n, "medication-card") is not None, a.ui_timeout)
        problems = plan_problems(nodes, meds, plan)
        self.step("plan_shown", not problems, t0, missing=problems, screenshot=self.shot("plan"))

        # 4. The watch presses Help; the family member acknowledges it in the app.
        t0 = time.time()
        for stale in srv.open_help_alerts():
            srv.resolve(stale["id"])
        srv.watch_help()
        self.run([{"tap": "tab-home"}])
        nodes = self.wait_for(lambda n: find_id(n, "urgent-banner") is not None, max(a.ui_timeout, 90))
        alerts = srv.open_help_alerts()
        self.step("help_alert_shown", find_id(nodes, "urgent-banner") is not None and len(alerts) == 1, t0,
                  screenshot=self.shot("help_banner"), open_help_alerts=len(alerts))
        t0 = time.time()
        acknowledged = None
        if alerts and self.run([{"tap": "home-on-my-way"}]):
            deadline = time.time() + a.ui_timeout
            while time.time() < deadline:
                acknowledged = srv.api("GET", f"/api/v1/care/alerts/{alerts[0]['id']}", None, srv.h_coord)
                if acknowledged.get("status") == "acknowledged":
                    break
                time.sleep(3)
        self.step("acknowledged_in_app", bool(acknowledged) and acknowledged.get("status") == "acknowledged", t0,
                  screenshot=self.shot("acknowledged"))
        for al in srv.open_help_alerts():
            srv.resolve(al["id"])

        # 5. Dark mode, kept across a restart.
        t0 = time.time()
        self.run([{"tap": "tab-settings"}, {"scroll_to": "settings-appearance"}, {"tap": "settings-appearance"}, {"tap": "mode-dark"}])
        chosen = is_selected(find_id(self.nodes(), "mode-dark"))
        self.relaunch()
        self.wait_for(lambda n: find_id(n, "tab-settings") is not None, a.ui_timeout)
        self.run([{"tap": "tab-settings"}, {"scroll_to": "settings-appearance"}, {"tap": "settings-appearance"}])
        nodes = self.wait_for(lambda n: find_id(n, "mode-dark") is not None, a.ui_timeout)
        kept = is_selected(find_id(nodes, "mode-dark"))
        self.step("appearance_kept_across_restart", chosen and kept, t0, chosen=chosen, kept=kept,
                  screenshot=self.shot("appearance_after_restart"))
        self.run([{"tap": "mode-system"}, {"tap": "settings-back"}])

        # 6. The care server unreachable (stopped: a simulator's network cannot be switched off), then back.
        t0 = time.time()
        self.stop_api()
        self.run([{"tap": "tab-home"}])
        nodes = self.wait_for(lambda n: has_text(n, STALE_WORDS), max(a.ui_timeout, 90))
        problems = offline_problems(nodes)
        self.step("offline_state_shown", not problems, t0, missing=problems, how="care server stopped",
                  screenshot=self.shot("offline"))
        t0 = time.time()
        started = self.start_api()
        nodes = self.wait_for(lambda n: has_text(n, ALL_WELL_WORDS), max(a.ui_timeout, 90))
        problems = recovered_problems(nodes) + ([] if started else ["care server did not start again"])
        self.step("connectivity_recovered", not problems, t0, missing=problems, screenshot=self.shot("recovered"))

        # 7. Sign out: the server's session for this phone is ended; nothing is left on screen.
        t0 = time.time()
        srv.h_family = srv.sign_in_api(srv.family)    # the earlier token was issued before the restart
        before = srv.family_sessions()
        nodes = self.sign_out()
        after = srv.family_sessions()
        problems = revocation_problems(before, after)
        problems += nothing_revealed_problems(nodes, (srv.family["name"], name))
        self.relaunch()
        nodes = self.wait_for(lambda n: find_id(n, "sign-in") is not None, a.ui_timeout)
        if find_id(nodes, "sign-in") is None:
            problems.append("sign-in screen not shown after a restart")
        self.step("secure_sign_out", not problems, t0, missing=problems, sessions_before=len(before),
                  screenshot=self.shot("signed_out"))

        # 8. The wearer talks to IRA.
        if self.sign_in(srv.wearer, "wearer"):
            t0 = time.time()
            self.wait_for(lambda n: find_id(n, "home-talk") is not None, a.ui_timeout)
            self.run([{"tap": "home-talk"}, {"type": ["talk-input", "Hello IRA, I had my breakfast"]}, {"tap": "talk-send"}])
            nodes = self.wait_for(lambda n: any(x["resource-id"].startswith("talk-ira-") for x in n), max(a.ui_timeout, 60))
            replies = [x["text"] for x in nodes if x["resource-id"].startswith("talk-ira-")]
            self.step("wearer_talked_to_ira", bool(replies), t0, reply=replies[:1], screenshot=self.shot("talk"))
            self.sign_out()

        # 9. The carer: the carer's home, none of the family experience.
        if self.sign_in(srv.coordinator, "carer"):
            t0 = time.time()
            nodes = self.wait_for(lambda n: find_id(n, "card-people") is not None, a.ui_timeout)
            problems = carer_home_problems(nodes)
            self.step("carer_home_isolated", not problems, t0, missing=problems, screenshot=self.shot("carer_home"))
            self.sign_out()

        self.stop_api()
        return self.finish()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--udid", required=True, help="the booted simulator, with the app installed")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--credentials", required=True, help="the demo seed file (fictional logins)")
    ap.add_argument("--out", default="build/ios-care-journey")
    ap.add_argument("--api-cmd", default="", help="starts the care server; the journey stops and restarts it")
    ap.add_argument("--type-server", action="store_true",
                    help="type the care server's address on the sign-in screen (default: use the build's default)")
    ap.add_argument("--maestro", default=os.path.expanduser("~/.maestro/bin/maestro"))
    ap.add_argument("--ui-timeout", type=float, default=45.0)
    return Journey(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
