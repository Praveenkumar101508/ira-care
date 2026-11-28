#!/usr/bin/env python3
"""Smoke journey for the watch app on a running emulator (or a device on adb).

    python3 scripts/emulator/watch_smoke.py --apk watch/app/build/outputs/apk/wearos/debug/app-wearos-debug.apk \\
        --out build/emulator-smoke [--serial emulator-5554] \\
        [--api http://127.0.0.1:8000 --credentials .demo/demo.local.json] \\
        [--steps medication,checkin,fall,help_during_fall,offline,reboot,permission,unpair,accessibility] \\
        [--until fall] [--api-only]

Without --api it installs the debug APK, launches the main activity and checks
the pairing screen is shown with its Pair button. With --api and a seed-format
credentials file it also does the care journey against that (demo) backend:
creates a pairing code for the first fictional person as the coordinator,
types it on the watch, grants the runtime permissions with `pm grant` (as a
person would on the permission screen), checks the home screen shows the Help
button, presses Help, and checks both the wrist ("Help request received") and
the server (a new open help_requested alert for that person). Those are the
eleven core steps; their names never change.

After them come the extended groups (GROUPS, chosen with --steps and cut short
with --until), each guarded so a failure records its steps and the journey
goes on: a medication reminder answered unclearly (nothing recorded) and then
confirmed in two steps; a check-in asked by a family member and answered by
touch; a synthetic fall (the debug build's broadcast receiver) whose countdown
lapses into one alert, with a second detection attached rather than a second
alert (D-092); Help pressed during a fall countdown (one alert) and the
responder's "on the way" on the wrist; a Help queued offline and delivered once;
a reboot after which monitoring resumes and the queue drains; the sensor
permission revoked (shown and reported) and granted again; unpairing with its
warning, the dead token, and pairing again; and the accessibility of the
screens dumped on the way. Voice is never exercised: every answer is by touch.

--api-only runs the same groups without a device: the watch's side is played
over HTTP with a device token (ApiWrist), which proves every server call the
groups make against a running backend. It is a dry run of the API, not of the app.

The emulator reaches the host's 127.0.0.1 as 10.0.2.2, which is where the
debug build points (CARE_API_BASE_URL in watch/app/build.gradle.kts), so the
backend must listen on host port 8000.

Every step is timed. Screenshots (PNG), UI dumps, logcat and results.json go
to --out. Exit status 0 only if every step that ran passed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PERMISSIONS = ("android.permission.BODY_SENSORS", "android.permission.ACTIVITY_RECOGNITION",
               "android.permission.RECORD_AUDIO", "android.permission.POST_NOTIFICATIONS",
               "android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION",
               "android.permission.BODY_SENSORS_BACKGROUND")

#: The extended groups, in the order they run (later ones rely on earlier ones having tidied up).
#: The home column's Unpair button: its text, and the description Compose exposes in its place
#: (watch/.../ui/MainActivity.kt).
UNPAIR_BUTTON = ("Unpair watch", "Unpair this watch")
GROUPS = ("medication", "checkin", "fall", "help_during_fall", "offline", "reboot", "permission", "unpair",
          "accessibility")

#: The words the wrist shows (FaceText, SosState, the screens in MainActivity), matched by substring.
HELP_RECEIVED = "Help request received."
WAITING_FOR_CONNECTION = "Waiting for connection. Help request saved."
OFFLINE_LINE = "Offline: saving for later"
FALL_LINE = "Help in"
PAIR_TITLE = "Pair this watch"
PERMISSION_TITLE = "Background sensors"
UNPAIR_WARNING = "Unpair this watch? IRA will stop watching over you"
HELP_DESC = "Help. Tells your family"

#: The alert kinds the server counts as an SOS for the wrist (care/sos.py SOS_KINDS, the two the watch opens).
SOS_KINDS = ("help_requested", "suspected_fall")
UNRESOLVED = ("asking", "open", "escalating", "acknowledged")

#: The project's touch-target rule: every home-screen button is at least 44 dp tall
#: (watch/app/src/test/.../AccessibilityTest.kt); 48 px is the floor this journey also records.
MIN_TARGET_DP = 44
MIN_TARGET_PX_FLOOR = 48

#: The sensor permissions the permission group revokes and grants again (background first).
#: A position given to the emulator's GPS before Help (latitude, longitude): central London, fictional.
GPS_FIX = (51.5072, -0.1276)
#: The battery level set on the emulator for the battery-report check.
BATTERY_TEST_LEVEL = 23


def gps_matches(alert: dict, fix: tuple, tolerance: float = 0.01) -> bool:
    """True when the alert's stored location is within about a kilometre of the position given."""
    lat, lon = alert.get("location_latitude"), alert.get("location_longitude")
    return lat is not None and lon is not None and abs(float(lat) - fix[0]) <= tolerance and abs(float(lon) - fix[1]) <= tolerance


SENSOR_PERMISSIONS = ("android.permission.BODY_SENSORS_BACKGROUND", "android.permission.BODY_SENSORS")

#: The debug build's synthetic fall (watch/app/src/debug/.../SyntheticFallReceiver.kt).
FALL_ACTION = "care.ira.watch.debug.SYNTHETIC_FALL"
FALL_RECEIVER = "care.ira.watch.debug.SyntheticFallReceiver"

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


#: How many times the smoke presses Pair again after "No connection. Try again." (see _run).
NO_CONNECTION_RETRIES = 3

#: How long to wait, after boot, for the emulator to reach the host (10.0.2.2) before the app is
#: launched. On GitHub's runners the guest's network can come up well after `sys.boot_completed`
#: (runs 45766023517 and 45902249975: the watch said "No connection" for 40 s with the host
#: unreachable from the guest); the app is only started once a ping to the host answers.
HOST_NETWORK_TIMEOUT_S = 120
HOST_ADDRESS = "10.0.2.2"


def host_reachable(probe_output: str) -> bool:
    """Whether a `ping -c 1` answered (toybox and busybox wordings)."""
    return "1 received" in probe_output or "1 packets received" in probe_output


def wait_for_host_network(probe, *, timeout_s: float = HOST_NETWORK_TIMEOUT_S, clock=time.monotonic,
                          sleep=time.sleep) -> tuple[bool, float]:
    """Call `probe()` (a ping to the host) until it answers or `timeout_s` passes.
    Returns (reachable, seconds waited)."""
    start = clock()
    while True:
        if host_reachable(probe()):
            return True, round(clock() - start, 1)
        if clock() - start >= timeout_s:
            return False, round(clock() - start, 1)
        sleep(3)


def app_crashes(crash_log: str, package: str) -> list[str]:
    """The crash-buffer lines that start a crash of this app's process."""
    return [ln.strip() for ln in crash_log.splitlines()
            if ("FATAL EXCEPTION" in ln or "Process: " in ln) and package in ln]


def visible_text(nodes: list[dict]) -> list[str]:
    """The texts and descriptions on screen, in order, without blanks or repeats."""
    seen: list[str] = []
    for n in nodes:
        for key in ("text", "content-desc"):
            v = n.get(key, "").strip()
            if v and v not in seen:
                seen.append(v)
    return seen


def contains_text(nodes: list[dict], part: str) -> dict | None:
    """The first node whose text or description contains `part` (the wrist wraps and prefixes its words)."""
    for n in nodes:
        if part in n.get("text", "") or part in n.get("content-desc", ""):
            return n
    return None


def clickable_ancestor(xml: str, node: dict | None) -> dict | None:
    """The innermost clickable node whose bounds contain `node` (Compose puts the click on the
    Button, not on its Text)."""
    if node is None:
        return None
    x, y = centre(node)
    best = None
    for n in parse_nodes(xml):
        if n.get("clickable") != "true" or "bounds" not in n:
            continue
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", n["bounds"]))
        if x1 <= x <= x2 and y1 <= y <= y2:
            area = (x2 - x1) * (y2 - y1)
            if best is None or area < best[0]:
                best = (area, n)
    return best[1] if best else None


def centre(node: dict) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
    return (x1 + x2) // 2, (y1 + y2) // 2


# ── Pure helpers for the extended groups (unit-tested in tests/readiness) ────────────────────

def select_groups(groups: tuple[str, ...], steps: str | None, until: str | None) -> list[str]:
    """Which extended groups run, in their canonical order. `steps` is a comma list or "all";
    `until` cuts the list after that group. An unknown name is an error, not a silent skip."""
    wanted = list(groups) if not steps or steps.strip() in ("", "all") else [s.strip() for s in steps.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in groups] + ([until] if until and until not in groups else [])
    if unknown:
        raise ValueError(f"unknown step group(s) {unknown}; choose from {', '.join(groups)}")
    chosen = [g for g in groups if g in wanted]
    if until:
        chosen = [g for g in chosen if groups.index(g) <= groups.index(until)]
    return chosen


def app_nodes(nodes: list[dict], package: str) -> list[dict]:
    """The nodes that belong to the app (the dump also holds the system bars and overlays)."""
    return [n for n in nodes if n.get("package") == package]


def bounds_of(node: dict) -> tuple[int, int, int, int] | None:
    found = re.findall(r"\d+", node.get("bounds", ""))
    return tuple(map(int, found[:4])) if len(found) == 4 else None


def _labelled(attrs: dict) -> bool:
    return bool(attrs.get("text", "").strip() or attrs.get("content-desc", "").strip())


def unlabelled_clickables(nodes: list[dict]) -> list[dict]:
    """Clickable nodes with neither text nor a content description: a screen reader would say
    nothing for them. A text field is excluded: its label is the hint beside it. Flat form, for
    callers without the XML; `unlabelled_clickables_xml` also accepts a label on a child."""
    out = []
    for n in nodes:
        if n.get("clickable") != "true" or n.get("class") == "android.widget.EditText":
            continue
        if not _labelled(n):
            out.append({k: n.get(k) for k in ("class", "bounds", "resource-id")})
    return out


def unlabelled_clickables_xml(xml: str, package: str) -> list[dict]:
    """The app's clickable nodes that neither carry a label nor contain one. Compose puts a card's
    or a row's `clickable` on the container and its words on a child node; a screen reader reads
    the child, so such a container is labelled (run 45905347211 flagged every card this way)."""
    import xml.etree.ElementTree as ET

    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for el in root.iter("node"):
        a = el.attrib
        if a.get("package") != package or a.get("clickable") != "true" or a.get("class") == "android.widget.EditText":
            continue
        if _labelled(a) or any(_labelled(child.attrib) for child in el.iter("node") if child is not el):
            continue
        out.append({k: a.get(k) for k in ("class", "bounds", "resource-id")})
    return out


def small_targets(nodes: list[dict], min_px: int) -> list[dict]:
    """Labelled clickable nodes (the buttons) narrower or shorter than `min_px` on screen. The
    project's rule (AccessibilityTest) is about buttons; a clickable row or card whose words are
    on a child is not a button and its height is its content's."""
    out = []
    for n in nodes:
        if n.get("clickable") != "true" or not _labelled(n):
            continue
        b = bounds_of(n)
        if b is None:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if w <= 0 or h <= 0:
            continue
        if w < min_px or h < min_px:
            out.append({"text": n.get("text"), "content-desc": n.get("content-desc", "")[:40], "bounds": n.get("bounds"),
                        "width": w, "height": h})
    return out


def density_dpi(wm_density_output: str) -> int | None:
    """The dpi from `wm density` ("Physical density: 320", with an override line when one is set)."""
    m = re.search(r"Override density: (\d+)", wm_density_output) or re.search(r"Physical density: (\d+)", wm_density_output)
    return int(m.group(1)) if m else None


def min_target_px(dpi: int | None, dp: int = MIN_TARGET_DP) -> int:
    """`dp` in pixels at this density; the 48 px floor when the density is unknown or tiny."""
    px = round(dp * dpi / 160) if dpi else 0
    return max(px, MIN_TARGET_PX_FLOOR)


def boot_completed(getprop_output: str) -> bool:
    return getprop_output.strip() == "1"


def service_running(dumpsys_output: str, package: str) -> bool:
    """Whether the care foreground service is among the package's running services."""
    return any("CareForegroundService" in ln and package in ln for ln in dumpsys_output.splitlines())


def fall_broadcast(package: str, peak_g: float, still_seconds: int, impact_at_ms: int) -> list[str]:
    """The adb shell arguments that inject a synthetic fall into the debug build."""
    return ["shell", "am", "broadcast", "-n", f"{package}/{FALL_RECEIVER}", "-a", FALL_ACTION,
            "--ef", "peak_g", str(peak_g), "--ei", "still_seconds", str(still_seconds), "--el", "impact_at", str(impact_at_ms)]


def sos_alerts(alerts: list[dict], person_id: str) -> list[dict]:
    """The person's unresolved SOS alerts (the ones the wrist shows a state for)."""
    return [a for a in alerts if str(a.get("person_id")) == str(person_id) and a.get("kind") in SOS_KINDS
            and a.get("status") in UNRESOLVED]


def new_alerts(before_ids: set, alerts: list[dict]) -> list[dict]:
    """Alerts (not notices) that were not there before."""
    return [a for a in alerts if str(a.get("id")) not in before_ids and a.get("severity") != "notice"]


def detections(alert: dict | None) -> list[dict]:
    return list(((alert or {}).get("details") or {}).get("detections") or [])


def taken_doses(events: list[dict], medication_id: str) -> list[dict]:
    """The doses recorded as taken for this medicine: after an unclear answer there must be none (C-1)."""
    return [e for e in events if str(e.get("medication_id")) == str(medication_id) and e.get("status") == "taken"]


def watch_dose(events: list[dict], medication_id: str, checkin_id: str) -> dict | None:
    """The taken dose the watch's confirmation wrote: through the check-in named, so its provenance
    is the wrist's own answer (confirmed_via "checkin", source "self_report"), never a carer's word."""
    for e in taken_doses(events, medication_id):
        if str(e.get("checkin_id")) == str(checkin_id) and e.get("confirmed_via") == "checkin" and e.get("source") == "self_report":
            return e
    return None


def checkin_for_reminder(checkins: list[dict], reminder_id: str, statuses: tuple[str, ...],
                         exclude: tuple[str, ...] = ()) -> dict | None:
    """The newest check-in asking about this reminder in one of `statuses`, not among `exclude` ids."""
    rows = [c for c in checkins if str(c.get("reminder_id")) == str(reminder_id) and c.get("status") in statuses
            and str(c.get("id")) not in exclude]
    rows.sort(key=lambda c: str(c.get("scheduled_at")), reverse=True)
    return rows[0] if rows else None


def responder_line(name: str) -> str:
    """What the wrist says once a responder is on the way (SosState.OnTheWay)."""
    return f"{name} is on the way."


def face_text(countdown: int | None, sos_text: str | None, question: str | None, last_answer: str | None, online: bool) -> str:
    """FaceText.main, for the API-only wrist (no visit line: the journey plans no visit)."""
    if countdown is not None:
        return f"Did you fall? Are you OK?\nHelp in {countdown} s"
    if sos_text:
        return sos_text
    if question:
        return question
    if last_answer is not None:
        return "Thank you."
    return "All well" if online else "No connection.\nHelp is saved and sent when it is back."


def sos_text_of(state: str | None, responder_name: str | None) -> str | None:
    """SosState.fromServer(...).text(): the server's word, never the watch's guess."""
    name = responder_name or "Someone"
    return {"received": HELP_RECEIVED, "on_the_way": responder_line(name), "arrived": f"{name} has arrived.",
            "closed": "This is now resolved."}.get(state or "idle", None if state in (None, "idle", "asking") else HELP_RECEIVED)


def reminder_buttons(reminder_kind: str | None) -> list[str]:
    """ReminderButtons.choices: the labels a reminder shows instead of "I'm OK"."""
    if reminder_kind is None:
        return []
    if reminder_kind in ("medication", "hydration"):
        return ["Done", "Later", "Not today"]
    return ["Done", "Later"]


def iso_now(offset_s: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)).isoformat()


# ── The two wrists: the emulator over adb, or the watch's calls over HTTP ───────────────────

class AdbWrist:
    """The watch app on the emulator, through adb and uiautomator dumps."""

    def __init__(self, smoke: "Smoke"):
        self.s = smoke
        self.a = smoke.a

    def snapshot(self, label: str) -> dict:
        nodes = self.s.dump()
        self.s.save_dump(label)
        return {"screenshot": self.s.shot(label), "dump": f"ui_{label}.xml", "visible": visible_text(nodes)[:30]}

    def visible(self) -> list[str]:
        return visible_text(self.s.dump())

    def wait_text(self, part: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = contains_text(self.s.dump(), part)
            if n is not None:
                return n
            time.sleep(3)
        return None

    def tap(self, *labels: str) -> str | None:
        """Taps the first of `labels` on screen (exact text, or a description prefix); the label hit, or None."""
        nodes = self.s.dump()
        for label in labels:
            n = find(nodes, text=label) or find(nodes, desc_prefix=label)
            if n is not None:
                target = clickable_ancestor(self.s.last_xml, n) or n
                # The face can change between the dump and the tap (a question answered, an SOS
                # state arriving): look once more, and only tap a label that is still there, in
                # the same place, so a tap meant for Done never lands on Help.
                again = self.s.dump()
                n2 = find(again, text=label) or find(again, desc_prefix=label)
                if n2 is None:
                    return None
                target2 = clickable_ancestor(self.s.last_xml, n2) or n2
                if target2.get("bounds") != target.get("bounds"):
                    target = target2
                self.s.adb("shell", "input", "tap", *map(str, centre(target)))
                return label
        return None

    def home(self, timeout: float) -> bool:
        self.s.adb("shell", "am", "start", "-n", f"{self.a.package}/care.ira.watch.ui.MainActivity", timeout=300, check=False)
        return self.s.wait_for_app_screen(timeout, desc_prefix=HELP_DESC) is not None

    def scroll_to_text(self, *parts: str, swipes: int = 3) -> dict | None:
        """Scroll the face until one of `parts` is drawn, as text or description (uiautomator dumps
        only what is on screen; the home column's last rows, Unpair watch and About, sit below the
        fold: run 45910155640; Compose merges a button's text into its description, so the Unpair
        button shows only "Unpair this watch. ...": run 45994124214)."""
        def look():
            nodes = self.s.dump()
            return next((n for n in (contains_text(nodes, p) for p in parts) if n is not None), None)
        for _ in range(swipes + 1):
            n = look()
            if n is not None:
                return n
            self.s.adb("shell", "input", "swipe", "227", "400", "227", "120", "300")
            time.sleep(1.5)
        return look()

    def inject_fall(self, peak_g: float, still_seconds: int, impact_at_ms: int) -> bool:
        out = self.s.adb(*fall_broadcast(self.a.package, peak_g, still_seconds, impact_at_ms), timeout=120, check=False)
        return "Broadcast completed" in out

    def network(self, on: bool) -> None:
        state = "enable" if on else "disable"
        for svc in ("wifi", "data"):
            self.s.adb("shell", "svc", svc, state, timeout=60, check=False)

    def reboot(self, timeout: float) -> float:
        """Reboots the device and waits for boot_completed; the seconds it took (or raises)."""
        t0 = time.time()
        subprocess.run([self.a.adb, "-s", self.a.serial, "reboot"], capture_output=True, timeout=120)
        time.sleep(10)
        subprocess.run([self.a.adb, "-s", self.a.serial, "wait-for-device"], capture_output=True, timeout=timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if boot_completed(self.s.adb("shell", "getprop", "sys.boot_completed", timeout=60, check=False)):
                self.s.prepare_device()
                return round(time.time() - t0, 1)
            time.sleep(5)
        raise RuntimeError(f"the device did not finish booting within {timeout} s")

    def set_permission(self, permission: str, granted: bool) -> dict:
        r = subprocess.run([self.a.adb, "-s", self.a.serial, "shell", "pm", "grant" if granted else "revoke", self.a.package, permission],
                           capture_output=True, text=True, timeout=120)
        return {"permission": permission, "granted": granted, "ok": r.returncode == 0, "error": r.stderr.strip()[:160]}

    def service_running(self) -> bool:
        return service_running(self.s.adb("shell", "dumpsys", "activity", "services", self.a.package, timeout=120, check=False), self.a.package)

    def density(self) -> int | None:
        return density_dpi(self.s.adb("shell", "wm", "density", timeout=60, check=False))

    def pair(self, code: str, dev: dict, prefix: str = "") -> bool:
        return self.s.type_code_and_pair(code, dev, prefix)


class ApiWrist:
    """The watch app's server-facing behaviour, played over HTTP with a device token (--api-only).

    It keeps the little the app keeps: the outbox (sent in order, with idempotency keys, held
    while offline), the open question and its buttons, the fall countdown with its incident id
    (FallCountdown's rules for a second detection), and the SOS state the server reports. What
    it "shows" is computed from those the way the screens compute it. No voice, no sensors.
    """

    def __init__(self, smoke: "Smoke"):
        self.s = smoke
        self.token: str | None = None
        self.old_token: str | None = None
        self.device_id: str | None = None
        self.paired = False
        self.permission = True
        self.online = True
        self.outbox: list[dict] = []
        self.question: str | None = None
        self.checkin: dict | None = None
        self.last_answer: str | None = None
        self.countdown: dict | None = None
        self.countdown_s = 30
        self.sos: dict = {}
        self.screen = "pairing"
        self.calls: list[dict] = []

    # The device API, as CareApi makes the calls.
    def call(self, method: str, path: str, body=None, key: str | None = None, token: str | None = None):
        if not self.online:
            return None, "offline"
        headers = {"X-Device-Token": token or self.token or ""}
        if key:
            headers["X-Idempotency-Key"] = key
        code, out = self.s.api_status(method, path, body, headers)
        self.calls.append({"method": method, "path": path, "status": code})
        return code, out

    def enqueue(self, kind: str, path: str, body: dict, key: str | None = None) -> None:
        key = key or str(uuid.uuid4())
        if not any(e["key"] == key for e in self.outbox):
            self.outbox.append({"kind": kind, "path": path, "body": body, "key": key})
        self.drain()

    def drain(self) -> bool:
        for event in list(self.outbox):
            code, _ = self.call("POST", event["path"], event["body"], key=event["key"])
            if code is None:
                return False
            if code == 401:
                self.on_unauthorised()
                return False
            self.outbox.remove(event)   # success or a definite refusal: the server has answered
        return True

    def on_unauthorised(self) -> None:
        self.old_token, self.token, self.paired, self.screen = self.token, None, False, "pairing"

    def refresh(self) -> None:
        if not self.paired:
            return
        if self.countdown and time.time() >= self.countdown["deadline"]:
            inc = self.countdown["incident"]
            self.countdown = None
            self.enqueue("help", "/api/v1/care/devices/me/help", {"via": "fall", "incident_id": inc}, key=f"fall-{inc}-help")
        if not self.online:
            return
        self.drain()
        code, me = self.call("GET", "/api/v1/care/devices/me")
        if code == 401:
            return self.on_unauthorised()
        if code == 200:
            self.sos = me.get("sos") or {}
            self.countdown_s = int((me.get("fall") or {}).get("fall_countdown_s") or 30)
        if self.question is None:
            code, due = self.call("GET", "/api/v1/care/devices/me/checkins/due")
            if code == 200 and (due or {}).get("checkin"):
                self.checkin = due["checkin"]
                self.question = self.checkin["question_text"]

    def sos_text(self) -> str | None:
        if any(e["kind"] == "help" for e in self.outbox):
            return "Sending…" if self.online else WAITING_FOR_CONNECTION
        return sos_text_of(self.sos.get("state"), self.sos.get("responder_name"))

    def countdown_left(self) -> int | None:
        return max(0, int(self.countdown["deadline"] - time.time() + 0.999)) if self.countdown else None

    def visible(self) -> list[str]:
        if self.screen == "pairing" or not self.paired:
            return [PAIR_TITLE, "Pair"]
        if not self.permission:
            return [PERMISSION_TITLE, "Allow"]
        if self.screen == "unpair":
            return ["Unpair this watch? IRA will stop watching over you, and your family and carers will be told.", "Keep paired", "Unpair"]
        sos_active = self.sos_text() is not None and self.sos.get("state") not in (None, "idle", "closed") or self.outbox
        kind = (self.checkin or {}).get("question_kind") if (self.checkin or {}).get("reminder_id") else None
        out = [face_text(self.countdown_left(), self.sos_text(), self.question, self.last_answer, self.online)]
        if not self.online:
            out.append(OFFLINE_LINE)
        buttons = reminder_buttons(kind) if self.countdown is None and not sos_active else []
        out += buttons or ["I'm OK"]
        out.append("Help")
        if self.question is None and self.countdown is None:
            out.append("Talk to IRA")
            if self.sos.get("state") in (None, "idle") and not self.outbox:
                out += ["Unpair watch…", "About"]
        return out

    def snapshot(self, label: str) -> dict:
        return {"visible": self.visible(), "api_only": True}

    def wait_text(self, part: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while True:
            self.refresh()
            for t in self.visible():
                if part in t:
                    return {"text": t}
            if time.time() >= deadline:
                return None
            time.sleep(2)

    def tap(self, *labels: str) -> str | None:
        self.refresh()
        shown = self.visible()
        label = next((lb for lb in labels if any(t == lb or t.startswith(lb) for t in shown)), None)
        if label is None:
            return None
        choice = {"Done": "done", "Later": "later", "Not today": "skip"}.get(label)
        if choice and self.checkin:
            self.answer({"text": "", "via": "button", "choice": choice}, label)
        elif label == "I'm OK":
            if self.countdown:
                inc = self.countdown["incident"]
                self.countdown = None
                self.enqueue("fall_ok", "/api/v1/care/devices/me/fall/ok", {"incident_id": inc}, key=f"fall-{inc}-ok")
            elif self.checkin:
                self.answer({"text": "", "via": "button"}, "I'm OK")
        elif label == "Help":
            if self.countdown:
                inc = self.countdown["incident"]
                self.countdown = None
                self.enqueue("help", "/api/v1/care/devices/me/help", {"via": "fall", "incident_id": inc}, key=f"fall-{inc}-help")
            else:
                self.enqueue("help", "/api/v1/care/devices/me/help", {"via": "button"})
        elif label in ("Unpair watch…", "Unpair this watch"):
            self.screen = "unpair"
        elif label == "Keep paired":
            self.screen = "home"
        elif label == "Unpair":
            code, _ = self.call("POST", "/api/v1/care/devices/me/unpair", {})
            if code in (200, 401):
                self.on_unauthorised()
        return label

    def answer(self, body: dict, label: str) -> None:
        cid = self.checkin["id"]
        self.question, self.checkin, self.last_answer = None, None, label
        self.enqueue("checkin_answer", f"/api/v1/care/devices/me/checkins/{cid}/answer", body)

    def home(self, timeout: float) -> bool:
        self.screen = "home"
        self.refresh()
        return self.paired and self.permission

    def inject_fall(self, peak_g: float, still_seconds: int, impact_at_ms: int) -> bool:
        """FallCountdown.suspectedFall: a new incident, the same impact seen twice, or a separate
        detection attached to the running incident (D-092)."""
        if self.countdown is None:
            inc = str(uuid.uuid4())
            self.countdown = {"incident": inc, "deadline": time.time() + self.countdown_s, "impact": impact_at_ms, "detections": 1}
            self.enqueue("fall_started", "/api/v1/care/devices/me/fall",
                         {"peak_g": peak_g, "still_seconds": still_seconds, "incident_id": inc, "detection": 1}, key=f"fall-{inc}-d1")
            return True
        if abs(impact_at_ms - self.countdown["impact"]) <= 1500:
            return True
        n = self.countdown["detections"] + 1
        self.countdown["detections"] = n
        inc = self.countdown["incident"]
        self.enqueue("fall_started", "/api/v1/care/devices/me/fall",
                     {"peak_g": peak_g, "still_seconds": still_seconds, "incident_id": inc, "detection": n}, key=f"fall-{inc}-d{n}")
        return True

    def network(self, on: bool) -> None:
        self.online = on
        if on:
            self.drain()

    def reboot(self, timeout: float) -> float:
        # BootReceiver: the service comes back and drains what the outbox held.
        self.question, self.checkin, self.countdown = None, None, None
        self.drain()
        return 0.0

    def set_permission(self, permission: str, granted: bool) -> dict:
        self.permission = granted
        code, _ = self.call("POST", "/api/v1/care/devices/me/report", {"readings": [], "background_sensor_permission": granted})
        return {"permission": permission, "granted": granted, "ok": code == 200, "error": "" if code == 200 else f"report answered {code}"}

    def service_running(self) -> bool:
        return self.paired

    def density(self) -> int | None:
        return None

    def pair(self, code: str, dev: dict, prefix: str = "") -> bool:
        t0 = time.time()
        status, body = self.call("POST", "/api/v1/care/devices/pair", {"pairing_code": code, "app_version": "api-only"}, token="")
        ok = status == 200 and isinstance(body, dict) and bool(body.get("device_token"))
        if ok:
            self.token, self.device_id, self.paired, self.screen = body["device_token"], body["id"], True, "home"
        return self.s.step(f"{prefix}paired", ok, t0, api_only=True, status=status,
                           server_device_id=body.get("id") if isinstance(body, dict) else None)


class Smoke:
    def reset_network(self, wait_s: float = 60) -> dict:
        """Airplane mode on, then off, then wait (up to `wait_s`) for the host to answer a ping."""
        self.adb("shell", "cmd", "connectivity", "airplane-mode", "enable", timeout=60, check=False)
        time.sleep(3)
        self.adb("shell", "cmd", "connectivity", "airplane-mode", "disable", timeout=60, check=False)
        ok, waited = wait_for_host_network(
            lambda: self.adb("shell", "ping", "-c", "1", "-W", "2", HOST_ADDRESS, timeout=30, check=False), timeout_s=wait_s)
        return {"host_back": ok, "seconds": round(waited, 1)}

    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.shot_n = 0
        self.dumps: dict[str, str] = {}
        self.wrist = None

    def adb(self, *args, timeout=300, check=True, binary=False):
        r = subprocess.run([self.a.adb, "-s", self.a.serial, *args], capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace')[:400]}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def step(self, name: str, ok: bool, t0: float, **info) -> bool:
        rec = {"step": name, "ok": bool(ok), "seconds": round(time.time() - t0, 1), **info}
        self.steps.append(rec)
        print(json.dumps(rec, default=str), flush=True)
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

    def keyboard_shown(self) -> bool:
        out = self.adb("shell", "dumpsys", "input_method", timeout=60, check=False)
        return "mInputShown=true" in out or "mIsInputViewShown=true" in out

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
            "clickable": [{k: n.get(k) for k in ("text", "class", "bounds", "enabled")}
                          for n in nodes if n.get("clickable") == "true"][:12],
            "keyboard_shown": self.keyboard_shown(),
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

    def app_in_front(self) -> bool:
        focus = self.adb("shell", "dumpsys", "window", "displays", timeout=60, check=False)
        return any(self.a.package in ln for ln in focus.splitlines() if "mCurrentFocus" in ln)

    def wait_for_app_screen(self, timeout: float, max_relaunch: int = 3, **kw) -> dict | None:
        """wait_for, but if Wear OS puts its own screen in front of the app (the watch face or
        the charging clock, as runs 45755139976 and 45762586497 showed), bring the app back, at
        most `max_relaunch` times, each one noted in the log."""
        deadline = time.time() + timeout
        relaunched = 0
        while time.time() < deadline:
            node = self.wait_for(min(30, max(1, deadline - time.time())), **kw)
            if node is not None:
                return node
            if relaunched < max_relaunch and not self.app_in_front():
                relaunched += 1
                self.adb("shell", "am", "start", "-n", f"{self.a.package}/care.ira.watch.ui.MainActivity",
                         timeout=120, check=False)
                print(json.dumps({"note": f"another screen was in front of the app; brought it back ({relaunched})"}),
                      flush=True)
        return None

    def save_dump(self, label: str) -> None:
        xml = getattr(self, "last_xml", "")
        self.dumps[label] = xml
        (self.out / f"ui_{label}.xml").write_text(xml)

    def api(self, method: str, path: str, body=None, headers=None, form=False):
        """A call that must succeed; the status and the server's words are in the error when it does not."""
        status, out = self.api_status(method, path, body, headers, form)
        if status is None or status >= 400:
            raise RuntimeError(f"{method} {path} answered {status}: {str(out)[:300]}")
        return out

    def api_status(self, method: str, path: str, body=None, headers=None, form=False):
        """(status, decoded body); a refusal is an answer here, not an exception."""
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
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except ValueError:
                return exc.code, raw.decode(errors="replace")[:300]

    def prepare_device(self) -> None:
        # A fresh emulator: mark setup done (the Wear setup wizard otherwise holds
        # the app's task back), keep the screen on and wake it. The emulator reports that
        # it is on a charger, and Wear OS then puts its charging clock (sysui
        # ChargingActivity) over every app (run 45755139976); report it unplugged, as a
        # watch on a wrist is. `stayon true` keeps the screen on without a charger.
        for cmd in ("settings put secure user_setup_complete 1", "settings put global device_provisioned 1",
                    "dumpsys battery unplug", "dumpsys battery set status 3",
                    "svc power stayon true", "settings put system screen_off_timeout 2147483647",
                    "input keyevent KEYCODE_WAKEUP"):
            self.adb("shell", cmd, timeout=120, check=False)

    def wait_for_host(self) -> tuple[bool, float]:
        """The guest can reach the host (the development server's address) before the app starts."""
        return wait_for_host_network(
            lambda: self.adb("shell", "ping", "-c", "1", "-W", "2", HOST_ADDRESS, timeout=30, check=False))

    def run(self) -> int:
        try:
            return self._run()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            # The device stopped answering (on a slow emulator, often system_server
            # restarting): a failed step with the reason, not a crash.
            self.step("device_error", False, time.time(), error=str(exc)[:400])
            return self.finish()

    def login(self) -> None:
        """The coordinator's and the family member's sessions, and the person the journey is about."""
        creds = json.loads(Path(self.a.credentials).read_text())
        self.creds = creds
        coord = next(l for l in creds["logins"] if l.get("role") == "workspace_admin")
        tok = self.api("POST", "/auth/cloud/login", {"email": coord["email"], "password": coord["password"]}, form=True)
        self.h = {"Authorization": f"Bearer {tok['access_token']}", "X-Workspace-ID": creds["workspace_id"]}
        self.person = creds["people"][0]
        fam = next((l for l in creds["logins"] if l.get("key") == "family"), None)
        self.fam_h, self.family_name = None, None
        if fam is not None:
            tok = self.api("POST", "/auth/cloud/login", {"email": fam["email"], "password": fam["password"]}, form=True)
            self.fam_h = {"Authorization": f"Bearer {tok['access_token']}", "X-Workspace-ID": creds["workspace_id"]}
            self.family_name = fam.get("name")

    def _run(self) -> int:
        a = self.a
        pkg = a.package
        props = {k: self.adb("shell", "getprop", k).strip() for k in
                 ("ro.build.fingerprint", "ro.build.version.sdk", "ro.product.cpu.abi", "ro.build.characteristics",
                  "sys.boot_completed")}
        (self.out / "device.json").write_text(json.dumps(props, indent=2) + "\n")
        print(json.dumps({"device": props}), flush=True)

        t0 = time.time()
        self.prepare_device()
        host_up, waited = self.wait_for_host() if a.api else (None, 0.0)
        self.step("prepare_device", True, t0, host_reachable=host_up, host_network_wait_s=waited)

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
        title = self.wait_for_app_screen(a.ui_timeout, text=PAIR_TITLE)
        pair_btn = find(self.dump(), text="Pair") if title else None
        self.save_dump("pairing")
        shot = self.shot("pairing_screen")
        if not self.step("pairing_screen_shown", title is not None and pair_btn is not None, t0, screenshot=shot):
            return self.finish()
        if not a.api:
            return self.finish()

        # ── The care journey against the backend ─────────────────────────────
        self.login()
        person = self.person
        h = self.h
        t0 = time.time()
        dev = self.api("POST", f"/api/v1/care/people/{person['id']}/devices", {"platform": "wearos"}, h)
        code = dev["pairing_code"]
        self.step("pairing_code_created", bool(code), t0, device_id=dev.get("id"))
        self.wrist = AdbWrist(self)
        if not self.type_code_and_pair(code, dev, ""):
            return self.finish()
        self.device = dev

        t0 = time.time()
        granted = []
        for p in PERMISSIONS:
            r = subprocess.run([a.adb, "-s", a.serial, "shell", "pm", "grant", pkg, p], capture_output=True, text=True)
            granted.append({"permission": p, "ok": r.returncode == 0, "error": r.stderr.strip()[:160]})
        self.adb("shell", "am", "start", "-W", "-n", f"{pkg}/care.ira.watch.ui.MainActivity", timeout=300)
        help_btn = self.wait_for_app_screen(a.ui_timeout, desc_prefix=HELP_DESC)
        self.save_dump("home")
        shot = self.shot("home_help_button")
        if not self.step("home_help_button_visible", help_btn is not None, t0, screenshot=shot, permissions=granted):
            return self.finish()

        t0 = time.time()
        # D1.f: the emulator's GPS is given a position before Help, so the SOS can carry it.
        self.adb("emu", "geo", "fix", str(GPS_FIX[1]), str(GPS_FIX[0]), timeout=60, check=False)
        time.sleep(3)
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
        # D1.f: the watch sends one location fix with the SOS; the alert carries the position given above.
        t0 = time.time()
        located = None
        if len(mine) == 1:
            located = self.wait_until(lambda: (lambda al: al if gps_matches(al, GPS_FIX) else None)(
                self.api("GET", f"/api/v1/care/alerts/{mine[0]['id']}", None, h)), 90)
        self.step("help_alert_carries_location", located is not None, t0, expected=GPS_FIX,
                  location={k: (located or {}).get(k) for k in ("location_latitude", "location_longitude", "location_accuracy_m")})

        # A journey can pass while the app crashed and restarted on the way (run 45760714043's
        # crash after pairing was hidden that way in an earlier pass), so read the crash buffer.
        t0 = time.time()
        crash = self.adb("logcat", "-d", "-b", "crash", timeout=60, check=False)
        mine_crashes = app_crashes(crash, pkg)
        self.step("no_app_crash", not mine_crashes, t0, crashes=mine_crashes[:5])

        # ── The extended journey ───────────────────────────────────────────────
        groups = select_groups(GROUPS, a.steps, a.until)
        if groups:
            self.core_alert_ids = {str(x["id"]) for x in mine}
            # The Help alert the core opened is closed first: a live SOS outranks a reminder on the face.
            for x in mine:
                self.resolve(x["id"])
            self.extended(groups)
            t0 = time.time()
            crash = self.adb("logcat", "-d", "-b", "crash", timeout=60, check=False)
            mine_crashes = app_crashes(crash, pkg)
            self.step("no_app_crash_after_journey", not mine_crashes, t0, crashes=mine_crashes[:5])
        return self.finish()

    def type_code_and_pair(self, code: str, dev: dict, prefix: str) -> bool:
        """Types the pairing code and presses Pair: the steps `type_code` and `paired` (prefixed when
        pairing again later in the journey)."""
        a = self.a
        t0 = time.time()
        field = find(self.dump(), cls="android.widget.EditText")
        if field is None:
            self.save_dump(f"{prefix}no_field")
            return self.step(f"{prefix}type_code", False, t0, error="no text field in the UI dump")
        self.adb("shell", "input", "tap", *map(str, centre(field)))
        time.sleep(2)
        self.adb("shell", "input", "text", code)
        time.sleep(3)
        # The Wear keyboard can stay over the screen after typing; close it so the tap reaches
        # the Pair button, and record where the button is and whether it is enabled.
        if self.keyboard_shown():
            self.adb("shell", "input", "keyevent", "KEYCODE_ESCAPE", check=False)
            time.sleep(2)
        if self.keyboard_shown():
            self.adb("shell", "input", "keyevent", "KEYCODE_BACK", check=False)
            time.sleep(2)
        btn = find(self.dump(), text="Pair")
        btn_target = clickable_ancestor(self.last_xml, btn) if btn else None
        shot = self.shot(f"{prefix}code_typed")
        self.step(f"{prefix}type_code", btn is not None, t0, screenshot=shot,
                  button={k: (btn_target or btn or {}).get(k) for k in ("bounds", "enabled", "clickable")},
                  screen=self.adb("shell", "wm", "size", check=False).strip(),
                  keyboard_shown=self.keyboard_shown())
        if btn is None:
            return False
        t0 = time.time()
        self.adb("shell", "input", "tap", *map(str, centre(btn_target or btn)))
        retapped = False
        no_connection_retries = 0
        gone = None
        deadline = time.time() + a.ui_timeout
        while time.time() < deadline:
            if not retapped and time.time() - t0 > 20:
                # Nothing changed after the first tap: tap once more (a first tap can be eaten
                # by a keyboard that was still closing), and say so.
                retapped = True
                print(json.dumps({"note": "no change 20 s after tapping Pair; tapped again"}), flush=True)
                again = find(self.dump(), text="Pair")
                if again is not None:
                    self.adb("shell", "input", "tap", *map(str, centre(clickable_ancestor(self.last_xml, again) or again)))
            nodes = self.dump()
            if find(nodes, text=PAIR_TITLE) is None:
                gone = True
                break
            if find(nodes, text="That code was not recognised."):
                gone = False
                break
            if find(nodes, text="No connection. Try again."):
                # Right after boot the emulator's network can lag (run 45766023517); the watch
                # says so and a person would press Pair again. Retry up to NO_CONNECTION_RETRIES
                # times, 10 s apart, each noted; a lasting block still fails.
                if no_connection_retries >= NO_CONNECTION_RETRIES:
                    gone = False
                    break
                no_connection_retries += 1
                probe = self.adb("shell", "ping", "-c", "1", "-W", "2", HOST_ADDRESS, timeout=30, check=False)
                reachable = host_reachable(probe)
                note = {"note": f"watch said No connection; pressing Pair again ({no_connection_retries})", "host_ping": reachable}
                if not reachable:
                    # The emulator's virtual network itself is down (runs 46045629293 and 46068431030:
                    # no ping to the host for the whole retry window). Airplane mode on and off resets
                    # it, as a person would; then wait for the host before pressing Pair again.
                    note["network_reset"] = self.reset_network()
                print(json.dumps(note), flush=True)
                time.sleep(10)
                again = find(self.dump(), text="Pair")
                if again is not None:
                    self.adb("shell", "input", "tap", *map(str, centre(clickable_ancestor(self.last_xml, again) or again)))
            time.sleep(3)
        self.save_dump(f"{prefix}after_pair")
        shot = self.shot(f"{prefix}after_pair")
        paired = [d for d in self.devices() if str(d.get("id")) == str(dev.get("id"))]
        return self.step(f"{prefix}paired", bool(gone), t0, screenshot=shot,
                         server_device=paired[0] if paired else None)

    # ── Server-side views the groups check ───────────────────────────────────

    def person_alerts(self) -> list[dict]:
        return self.api("GET", f"/api/v1/care/alerts?open_only=false&person_id={self.person['id']}&limit=200", None, self.h).get("alerts", [])

    def alert(self, alert_id: str) -> dict:
        return self.api("GET", f"/api/v1/care/alerts/{alert_id}", None, self.h)

    def checkins(self) -> list[dict]:
        return self.api("GET", f"/api/v1/care/people/{self.person['id']}/checkins?limit=100", None, self.h).get("checkins", [])

    def med_events(self) -> list[dict]:
        return self.api("GET", f"/api/v1/care/people/{self.person['id']}/medication-events?limit=200", None, self.h).get("events", [])

    def devices(self) -> list[dict]:
        server = self.api("GET", f"/api/v1/care/people/{self.person['id']}/devices", None, self.h)
        return server.get("devices", server) if isinstance(server, dict) else server

    def reminder(self, reminder_id: str) -> dict | None:
        rows = self.api("GET", f"/api/v1/care/people/{self.person['id']}/reminders?limit=200", None, self.h).get("reminders", [])
        return next((r for r in rows if str(r.get("id")) == str(reminder_id)), None)

    def resolve(self, alert_id: str) -> dict:
        return self.api("POST", f"/api/v1/care/alerts/{alert_id}/resolve",
                        {"resolution": "Emulator journey: closed by the coordinator after the check.", "outcome": "false_alarm"}, self.h)

    def alert_ids(self) -> set:
        return {str(x["id"]) for x in self.person_alerts()}

    @staticmethod
    def brief(alert: dict | None) -> dict | None:
        if alert is None:
            return None
        d = alert.get("details") or {}
        return {"id": alert.get("id"), "kind": alert.get("kind"), "status": alert.get("status"),
                "outcome": d.get("outcome"), "via": d.get("via"), "detections": len(detections(alert)),
                "incident_ids": d.get("incident_ids")}

    def wait_until(self, fn, timeout: float, every: float = 3.0):
        """`fn()`'s first true-ish value within `timeout` seconds, else None."""
        deadline = time.time() + timeout
        while True:
            if isinstance(self.wrist, ApiWrist):
                self.wrist.refresh()   # the app polls the server by itself; the API-only wrist only when asked
            v = fn()
            if v:
                return v
            if time.time() >= deadline:
                return None
            time.sleep(every)

    # ── The extended groups ──────────────────────────────────────────────────

    def quiet_face(self, label: str) -> None:
        """Every open SOS for the person is resolved by the coordinator, and the wrist is given
        until --sos-idle-timeout to show neither an active nor a closed SOS: the face shows a closed
        SOS for ten minutes (care/sos.py `current`), and while it does the reminder buttons and the
        Unpair button are not offered (run 45905347211: the core Help left the face in that state
        through the medication and check-in groups)."""
        t0 = time.time()
        resolved = []
        for al in self.person_alerts():
            if al.get("status") not in ("resolved",) and al.get("kind") in ("help_requested", "suspected_fall"):
                self.resolve(al["id"])
                resolved.append(al["id"])
        quiet_words = (HELP_RECEIVED, "This is now resolved.", "is on the way", "has arrived", "Did you fall")
        quiet = self.wait_until(lambda: (not any(q in t for t in self.wrist.visible() for q in quiet_words)) or None,
                                self.a.sos_idle_timeout, every=15)
        self.step(f"{label}_face_quiet", quiet is not None, t0, resolved_alerts=len(resolved), waited_s=round(time.time() - t0, 1),
                  visible=self.wrist.visible()[:6])

    def extended(self, groups: list[str]) -> None:
        self.journey_settings()
        if any(g in groups for g in ("medication", "checkin")):
            self.quiet_face("before_groups")
        for g in groups:
            t0 = time.time()
            print(json.dumps({"group": g}), flush=True)
            try:
                getattr(self, f"journey_{g}")()
            except subprocess.TimeoutExpired as exc:
                # The device stopped answering: nothing further can be driven.
                self.step(f"{g}_error", False, t0, error=f"device timeout: {str(exc)[:300]}")
                break
            except Exception as exc:  # noqa: BLE001 - one group's failure must not hide the next group's result
                self.step(f"{g}_error", False, t0, error=f"{type(exc).__name__}: {str(exc)[:360]}")
                if not self.a.api_only:
                    try:
                        self.dump()
                        self.diagnose(g)
                    except Exception as diag:  # noqa: BLE001
                        print(f"diagnosis not taken: {diag}", file=sys.stderr)

    def journey_settings(self) -> None:
        """Quiet hours off and the routine check-ins pushed back, so the journey's own questions
        are the only ones asked and they are asked at any hour the runner happens to be in."""
        t0 = time.time()
        pid = self.person["id"]
        current = self.api("GET", f"/api/v1/care/people/{pid}/settings", None, self.h)
        body = {k: v for k, v in current.items() if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
        body.update(quiet_start="00:00:00", quiet_end="00:00:00", checkin_min_minutes=240, checkin_max_minutes=240)
        after = self.api("PUT", f"/api/v1/care/people/{pid}/settings", body, self.h)
        self.step("journey_settings", after.get("quiet_start") == after.get("quiet_end") and after.get("checkin_min_minutes") == 240, t0,
                  previous={k: current.get(k) for k in ("quiet_start", "quiet_end", "checkin_min_minutes", "checkin_max_minutes")},
                  fall_countdown_s=after.get("fall_countdown_s"), fall_detection_enabled=after.get("fall_detection_enabled"))
        self.countdown_s = int(after.get("fall_countdown_s") or 30)
        # A question left open by an earlier run would hide a reminder's buttons: answer it first.
        stray = [c for c in self.checkins() if c.get("status") in ("scheduled", "asked", "repeated") and not c.get("reminder_id")]
        if stray:
            t0 = time.time()
            shown = self.wrist.wait_text("I'm OK", self.a.reminder_timeout)
            tapped = self.wrist.tap("I'm OK") if shown else None
            left = self.wait_until(lambda: not [c for c in self.checkins() if c.get("status") in ("asked", "repeated")] or None, 60)
            self.step("journey_stray_question_answered", tapped is not None and left is not None, t0, stray=[c.get("id") for c in stray])

    def journey_medication(self) -> None:
        """A medicine due now, answered unclearly (nothing recorded, C-1), then confirmed in two steps."""
        w, pid = self.wrist, self.person["id"]
        meds = self.api("GET", f"/api/v1/care/people/{pid}/medications", None, self.h)["medications"]
        med = meds[0]
        t0 = time.time()
        rem = self.api("POST", f"/api/v1/care/people/{pid}/reminders",
                       {"text": "", "due_at": iso_now(-5), "kind": "medication", "medication_id": med["id"]}, self.h)
        self.step("medication_reminder_created", bool(rem.get("id")), t0, reminder_id=rem.get("id"), medication=med["name"],
                  reminder_status=rem.get("status"))
        t0 = time.time()
        asked = self.wait_until(lambda: checkin_for_reminder(self.checkins(), rem["id"], ("asked", "repeated", "answered")), self.a.reminder_timeout)
        shown = w.wait_text("Later", self.a.ui_timeout) if asked else None
        snap = w.snapshot("reminder")
        if not self.step("medication_reminder_shown", asked is not None and shown is not None, t0,
                         question=(asked or {}).get("question_text"), checkin_id=(asked or {}).get("id"), **snap):
            return
        t0 = time.time()
        tapped = w.tap("Later")
        answered = self.wait_until(lambda: checkin_for_reminder(self.checkins(), rem["id"], ("answered",)), self.a.ui_timeout)
        rem_after = self.reminder(rem["id"]) or {}
        events = self.med_events()
        self.step("medication_unclear_not_recorded",
                  tapped is not None and answered is not None and rem_after.get("status") == "snoozed" and taken_doses(events, med["id"]) == [],
                  t0, choice="later", reminder_status=rem_after.get("status"), taken_doses=len(taken_doses(events, med["id"])),
                  answered_via=(answered or {}).get("answered_via"), answer_summary=(answered or {}).get("answer_summary"),
                  voice_exercised=False,
                  note="'Later' is the touch alternative to an unclear spoken answer ('maybe later', 'I think so'); the dose stays open")
        # The snoozed reminder would ask again in ten minutes, in the middle of another group: cancel it.
        self.api("POST", f"/api/v1/care/reminders/{rem['id']}/act", {"action": "cancel"}, self.h)

        t0 = time.time()
        rem2 = self.api("POST", f"/api/v1/care/people/{pid}/reminders",
                        {"text": "", "due_at": iso_now(0), "kind": "medication", "medication_id": med["id"]}, self.h)
        asked2 = self.wait_until(lambda: checkin_for_reminder(self.checkins(), rem2["id"], ("asked", "repeated")), self.a.reminder_timeout)
        shown = w.wait_text("Done", self.a.ui_timeout) if asked2 else None
        tapped = w.tap("Done") if shown else None
        # Step one of two: the server asks again, in other words, before a dose is written.
        confirm = self.wait_until(lambda: checkin_for_reminder(self.checkins(), rem2["id"], ("asked", "repeated"), exclude=(str(asked2["id"]),))
                                  if asked2 else None, self.a.reminder_timeout)
        shown_confirm = w.wait_text("Is that right?", self.a.ui_timeout) if confirm else None
        rem2_mid = self.reminder(rem2["id"]) or {}
        snap = w.snapshot("reminder_confirm")
        if not self.step("medication_confirm_asked", tapped is not None and confirm is not None and shown_confirm is not None
                         and rem2_mid.get("status") == "confirming" and taken_doses(self.med_events(), med["id"]) == [], t0,
                         first_question=(asked2 or {}).get("question_text"), confirm_question=(confirm or {}).get("question_text"),
                         reminder_status=rem2_mid.get("status"), **snap):
            return
        t0 = time.time()
        tapped = w.tap("Done")
        done = self.wait_until(lambda: (self.reminder(rem2["id"]) or {}).get("status") == "confirmed", self.a.ui_timeout)
        events = self.med_events()
        dose = watch_dose(events, med["id"], confirm["id"])
        rows = {str(c["id"]): c for c in self.checkins()}
        by_touch = all(rows.get(str(c["id"]), {}).get("answered_via") == "button" for c in (asked2, confirm))
        self.step("medication_taken_two_step", tapped is not None and done is not None and dose is not None and by_touch, t0,
                  dose={k: dose.get(k) for k in ("status", "confirmed_via", "source", "checkin_id", "due_at")} if dose else None,
                  taken_doses=len(taken_doses(events, med["id"])), answered_via_both="button" if by_touch else "not both by button",
                  voice_exercised=False)

    def journey_checkin(self) -> None:
        """A family member asks IRA to check in now; the question reaches the wrist; "I'm OK" answers it."""
        w, pid = self.wrist, self.person["id"]
        t0 = time.time()
        row = self.api("POST", f"/api/v1/care/people/{pid}/checkins/ask-now", {"kind": "wellbeing"}, self.fam_h or self.h)
        self.step("checkin_requested", bool(row.get("id")), t0, checkin_id=row.get("id"), by="family" if self.fam_h else "coordinator")
        t0 = time.time()
        asked = self.wait_until(lambda: next((c for c in self.checkins() if str(c["id"]) == str(row["id"]) and c["status"] in ("asked", "repeated", "answered")), None),
                                self.a.reminder_timeout)
        shown = w.wait_text((asked.get("question_text") or "")[:24], self.a.ui_timeout) if asked and asked.get("question_text") else None
        snap = w.snapshot("checkin")
        if not self.step("checkin_shown", asked is not None and shown is not None, t0, question=(asked or {}).get("question_text"), **snap):
            return
        t0 = time.time()
        tapped = w.tap("I'm OK")
        answered = self.wait_until(lambda: next((c for c in self.checkins() if str(c["id"]) == str(row["id"]) and c["status"] == "answered"), None),
                                   self.a.ui_timeout)
        self.step("checkin_answered_by_touch", tapped is not None and answered is not None and answered.get("answered_via") == "button", t0,
                  answered_via=(answered or {}).get("answered_via"), answer_summary=(answered or {}).get("answer_summary"), voice_exercised=False)

    def inject_and_wait_countdown(self, label: str) -> tuple[dict | None, dict]:
        """A synthetic fall; the wrist's countdown and the server's asking alert, with the dump saved."""
        w = self.wrist
        before = self.alert_ids()
        sent = w.inject_fall(3.2, 20, int(time.time() * 1000))
        shown = w.wait_text(FALL_LINE, 90) if sent else None
        alert = self.wait_until(lambda: next((a for a in sos_alerts(self.person_alerts(), self.person["id"]) if str(a["id"]) not in before), None), 90)
        snap = w.snapshot(label)
        snap.update(broadcast=sent, countdown_shown=shown is not None, alert=self.brief(alert))
        return alert, snap

    def journey_fall(self) -> None:
        """A fall countdown that lapses: one alert; a second detection during it joins that alert (D-092)."""
        w = self.wrist
        t0 = time.time()
        alert, snap = self.inject_and_wait_countdown("fall")
        if not self.step("fall_countdown_shown", snap["countdown_shown"] and alert is not None and alert.get("kind") == "suspected_fall"
                         and alert.get("status") == "asking", t0, **snap):
            return
        t0 = time.time()
        # A separate detection (an impact well after the first): attached to the incident, no second alert.
        w.inject_fall(3.4, 20, int(time.time() * 1000) + 5000)
        attached = self.wait_until(lambda: len(detections(self.alert(alert["id"]))) >= 2, 60)
        open_now = sos_alerts(self.person_alerts(), self.person["id"])
        self.step("fall_second_detection_attached", bool(attached) and len(open_now) == 1, t0,
                  detections=len(detections(self.alert(alert["id"]))), unresolved_sos_alerts=len(open_now), alert=self.brief(self.alert(alert["id"])))
        t0 = time.time()
        received = w.wait_text(HELP_RECEIVED, self.countdown_s + 120)
        final = self.wait_until(lambda: (lambda a: a if a.get("status") in ("open", "escalating", "acknowledged") else None)(self.alert(alert["id"])), 60)
        open_now = sos_alerts(self.person_alerts(), self.person["id"])
        snap = w.snapshot("fall_alert")
        self.step("fall_alert_after_countdown", received is not None and final is not None and len(open_now) == 1
                  and ((final or {}).get("details") or {}).get("outcome") == "no_answer", t0,
                  alert=self.brief(final), unresolved_sos_alerts=len(open_now), **snap)
        t0 = time.time()
        closed = self.resolve(alert["id"])
        self.step("fall_alert_resolved", closed.get("status") == "resolved", t0, status=closed.get("status"))

    def journey_help_during_fall(self) -> None:
        """Help pressed during a fall countdown: the fall's own alert opens, nothing else; the
        family member says "on my way" and the wrist shows it."""
        w = self.wrist
        t0 = time.time()
        alert, snap = self.inject_and_wait_countdown("fall_help")
        if not self.step("help_during_fall_countdown_shown", snap["countdown_shown"] and alert is not None and alert.get("status") == "asking", t0, **snap):
            return
        t0 = time.time()
        tapped = w.tap("Help")
        received = w.wait_text(HELP_RECEIVED, self.a.ui_timeout) if tapped else None
        open_now = sos_alerts(self.person_alerts(), self.person["id"])
        row = self.alert(alert["id"])
        snap = w.snapshot("help_during_fall")
        self.step("help_during_fall_one_alert", tapped is not None and received is not None and len(open_now) == 1
                  and str(open_now[0]["id"]) == str(alert["id"]) and row.get("status") in ("open", "escalating"), t0,
                  alert=self.brief(row), unresolved_sos_alerts=len(open_now), kinds=[a["kind"] for a in open_now], **snap)
        t0 = time.time()
        if self.fam_h is None:
            self.step("responder_status_on_wrist", False, t0, error="no family login in the credentials file")
        else:
            ack = self.api("POST", f"/api/v1/care/alerts/{alert['id']}/acknowledge", {"kind": "on_my_way", "note": "Emulator journey."}, self.fam_h)
            line = responder_line(self.family_name or "Someone")
            shown = w.wait_text(line, self.a.ui_timeout)
            snap = w.snapshot("responder")
            self.step("responder_status_on_wrist", shown is not None and ack.get("status") == "acknowledged", t0,
                      expected=line, alert_status=ack.get("status"), **snap)
        t0 = time.time()
        closed = self.resolve(alert["id"])
        self.step("help_during_fall_resolved", closed.get("status") == "resolved", t0, status=closed.get("status"))

    def queue_help_offline(self, prefix: str) -> set | None:
        """Connectivity off, Help pressed: queued on the wrist, nothing on the server. The alert ids before, or None."""
        w = self.wrist
        t0 = time.time()
        before = self.alert_ids()
        w.network(False)
        offline = w.wait_text(OFFLINE_LINE, 150)
        tapped = w.tap("Help") if offline else None
        waiting = w.wait_text(WAITING_FOR_CONNECTION, self.a.ui_timeout) if tapped else None
        time.sleep(5)
        arrived = new_alerts(before, self.person_alerts())
        snap = w.snapshot(f"{prefix}offline")
        ok = self.step(f"{prefix}help_queued_offline", offline is not None and tapped is not None and waiting is not None and arrived == [], t0,
                       offline_line_shown=offline is not None, waiting_shown=waiting is not None, alerts_reached_server=len(arrived), **snap)
        return before if ok else None

    def delivered_once(self, prefix: str, before: set) -> None:
        """Connectivity back: the queued Help arrives, exactly once, and the wrist hears it was received."""
        w = self.wrist
        t0 = time.time()
        w.network(True)
        arrived = self.wait_until(lambda: new_alerts(before, self.person_alerts()) or None, 240) or []
        received = w.wait_text(HELP_RECEIVED, self.a.ui_timeout) if arrived else None
        time.sleep(10)   # a duplicate would follow the first within the outbox's next attempt
        arrived = new_alerts(before, self.person_alerts())
        snap = w.snapshot(f"{prefix}online")
        self.step(f"{prefix}queue_delivered_once", len(arrived) == 1 and arrived[0].get("kind") == "help_requested" and received is not None, t0,
                  arrived=[self.brief(a) for a in arrived], **snap)
        for a in arrived:
            self.resolve(a["id"])

    def journey_offline(self) -> None:
        before = self.queue_help_offline("offline_")
        if before is None:
            self.wrist.network(True)
            return
        self.delivered_once("offline_", before)

    def journey_reboot(self) -> None:
        """A Help queued offline, then a reboot: monitoring comes back by itself (BootReceiver), the
        home screen is there, and the queue drains once when connectivity returns."""
        w = self.wrist
        before = self.queue_help_offline("reboot_")
        if before is None:
            w.network(True)
            return
        t0 = time.time()
        seconds = w.reboot(self.a.boot_timeout)
        running = self.wait_until(w.service_running, 180)
        self.step("reboot_monitoring_resumed", bool(running), t0, boot_seconds=seconds, service_running=bool(running))
        t0 = time.time()
        home = w.home(self.a.ui_timeout)
        snap = w.snapshot("after_reboot")
        self.step("reboot_home_screen", home, t0, **snap)
        self.delivered_once("reboot_", before)

    def journey_permission(self) -> None:
        """The body-sensor permission revoked: the permission screen, and the server told (D1.g); then granted again."""
        w = self.wrist
        t0 = time.time()
        changes = [w.set_permission(p, False) for p in SENSOR_PERMISSIONS]
        w.home(30)   # revoking kills the app; bring it back (its permission screen, not home)
        shown = w.wait_text(PERMISSION_TITLE, self.a.ui_timeout)
        snap = w.snapshot("permission")
        self.step("permission_revoked_shown", shown is not None and all(c["ok"] for c in changes), t0, changes=changes, **snap)
        t0 = time.time()
        reported = self.wait_until(lambda: next((d for d in self.devices() if str(d["id"]) == str(self.device["id"])
                                                 and d.get("background_sensor_permission") is False), None), 240)
        self.step("permission_revoked_reported", reported is not None, t0,
                  background_sensor_permission=(reported or {}).get("background_sensor_permission"), last_sync_at=(reported or {}).get("last_sync_at"))
        t0 = time.time()
        changes = [w.set_permission(p, True) for p in reversed(SENSOR_PERMISSIONS)]
        home = w.home(self.a.ui_timeout)
        recovered = self.wait_until(lambda: next((d for d in self.devices() if str(d["id"]) == str(self.device["id"])
                                                  and d.get("background_sensor_permission") is True), None), 240)
        snap = w.snapshot("permission_back")
        self.step("permission_regranted_recovered", home and recovered is not None and all(c["ok"] for c in changes), t0,
                  changes=changes, background_sensor_permission=(recovered or {}).get("background_sensor_permission"), **snap)

        # D1.g: the battery level the watch reports reaches the server's device row.
        t0 = time.time()
        self.adb("shell", "dumpsys", "battery", "set", "level", str(BATTERY_TEST_LEVEL), timeout=60, check=False)
        battery = self.wait_until(lambda: next((d for d in self.devices() if str(d["id"]) == str(self.device["id"])
                                                and d.get("battery_percent") == BATTERY_TEST_LEVEL), None), 300)
        self.adb("shell", "dumpsys", "battery", "set", "level", "80", timeout=60, check=False)
        self.step("battery_level_reported", battery is not None, t0, set_to=BATTERY_TEST_LEVEL,
                  battery_percent=(battery or {}).get("battery_percent"))

    def journey_unpair(self) -> None:
        """Unpairing from the wrist, with its warning; the server's row; a dead token; pairing again."""
        w, pid = self.wrist, self.person["id"]
        self.quiet_face("before_unpair")
        t0 = time.time()
        # The Unpair button waits for a quiet face (the server reports a closed SOS for ten minutes,
        # which quiet_face has already waited out) and sits below the fold of the home column.
        btn = w.scroll_to_text(*UNPAIR_BUTTON) if hasattr(w, "scroll_to_text") else w.wait_text(UNPAIR_BUTTON[0], 30)
        if btn is None:
            btn = w.wait_text(UNPAIR_BUTTON[1], min(self.a.sos_idle_timeout, 300))
            if btn is not None and hasattr(w, "scroll_to_text"):
                btn = w.scroll_to_text(*UNPAIR_BUTTON)
        tapped = w.tap("Unpair watch…", "Unpair this watch") if btn else None
        warning = w.wait_text(UNPAIR_WARNING, self.a.ui_timeout) if tapped else None
        keep = w.wait_text("Keep paired", 30) if warning else None
        snap = w.snapshot("unpair")
        if not self.step("unpair_warning_shown", warning is not None and keep is not None, t0, **snap):
            return
        t0 = time.time()
        tapped = w.tap("Unpair")
        pairing = w.wait_text(PAIR_TITLE, self.a.ui_timeout) if tapped else None
        row = self.wait_until(lambda: next((d for d in self.devices() if str(d["id"]) == str(self.device["id"]) and d.get("status") == "revoked"), None), 60)
        notice = next((a for a in self.person_alerts() if a.get("kind") == "watch_unpaired"), None)
        snap = w.snapshot("unpaired")
        self.step("unpaired_on_server", pairing is not None and row is not None and notice is not None, t0,
                  device_status=(row or {}).get("status"), notice_alert=self.brief(notice) if notice else None, **snap)
        # The dead token, proved with a scratch device of this journey's own: paired by code, unpaired, refused.
        t0 = time.time()
        scratch = self.api("POST", f"/api/v1/care/people/{pid}/devices", {"platform": "wearos"}, self.h)
        s_code, s_body = self.api_status("POST", "/api/v1/care/devices/pair", {"pairing_code": scratch["pairing_code"], "app_version": "journey"})
        token = (s_body or {}).get("device_token") if isinstance(s_body, dict) else None
        alive, _ = self.api_status("GET", "/api/v1/care/devices/me", None, {"X-Device-Token": token or ""})
        unpaired, _ = self.api_status("POST", "/api/v1/care/devices/me/unpair", {}, {"X-Device-Token": token or ""})
        dead, _ = self.api_status("GET", "/api/v1/care/devices/me", None, {"X-Device-Token": token or ""})
        help_dead, _ = self.api_status("POST", "/api/v1/care/devices/me/help", {"via": "button"}, {"X-Device-Token": token or ""})
        self.step("old_token_refused", s_code == 200 and alive == 200 and unpaired == 200 and dead == 401 and help_dead == 401, t0,
                  pair=s_code, before_unpair=alive, unpair=unpaired, after_unpair=dead, help_after_unpair=help_dead)
        # Pair again with a new code: a new device row; the old one stays revoked.
        t0 = time.time()
        dev2 = self.api("POST", f"/api/v1/care/people/{pid}/devices", {"platform": "wearos"}, self.h)
        if not w.pair(dev2["pairing_code"], dev2, "repair_"):
            return
        home = w.home(self.a.ui_timeout)
        rows = {str(d["id"]): d for d in self.devices()}
        old, new = rows.get(str(self.device["id"]), {}), rows.get(str(dev2["id"]), {})
        snap = w.snapshot("repaired")
        self.step("repaired_isolated", home and new.get("status") == "paired" and old.get("status") == "revoked" and str(dev2["id"]) != str(self.device["id"]), t0,
                  old_device={"id": old.get("id"), "status": old.get("status")}, new_device={"id": new.get("id"), "status": new.get("status")}, **snap)
        self.device = dev2

    def journey_accessibility(self) -> None:
        """Every clickable node on the dumped screens is labelled and big enough to tap."""
        t0 = time.time()
        if self.a.api_only:
            self.step("accessibility", True, t0, skipped=True, reason="no UI dumps in --api-only; the emulator run checks them")
            return
        dpi = self.wrist.density()
        min_px = min_target_px(dpi)
        wanted = ("home", "reminder", "reminder_confirm", "checkin", "fall", "fall_alert", "help_during_fall", "responder", "after_help", "permission", "unpair")
        screens = {label: app_nodes(parse_nodes(self.dumps[label]), self.a.package) for label in wanted if label in self.dumps}
        unlabelled = {label: unlabelled_clickables_xml(self.dumps[label], self.a.package) for label in screens}
        small = {label: small_targets(nodes, min_px) for label, nodes in screens.items()}
        clickables = {label: sum(1 for n in nodes if n.get("clickable") == "true") for label, nodes in screens.items()}
        self.step("accessibility_labels", bool(screens) and "home" in screens and not any(unlabelled.values()), t0,
                  screens=list(screens), clickable_nodes=clickables, unlabelled={k: v for k, v in unlabelled.items() if v})
        self.step("accessibility_touch_targets", bool(screens) and not any(small.values()), t0, density_dpi=dpi,
                  min_px=min_px, rule=f"{MIN_TARGET_DP} dp (AccessibilityTest), floor {MIN_TARGET_PX_FLOOR} px",
                  too_small={k: v for k, v in small.items() if v})

    def finish(self, _=None) -> int:
        if not self.a.api_only:
            try:
                (self.out / "logcat.txt").write_text(self.adb("logcat", "-d", "-v", "time", timeout=120, check=False)[-400_000:])
            except Exception as exc:   # the results still matter without the log
                print(f"logcat not saved: {exc}", file=sys.stderr)
        ok = bool(self.steps) and all(s["ok"] for s in self.steps)
        if not ok and not self.a.api_only:
            try:
                self.dump()
                self.diagnose(self.steps[-1]["step"] if self.steps else "start")
            except Exception as exc:   # a device that stopped answering still gets its results written
                print(f"diagnosis not taken: {exc}", file=sys.stderr)
        if isinstance(self.wrist, ApiWrist):
            (self.out / "api_calls.json").write_text(json.dumps(self.wrist.calls, indent=2) + "\n")
        (self.out / "results.json").write_text(json.dumps({"ok": ok, "steps": self.steps}, indent=2, default=str) + "\n")
        print(json.dumps({"ok": ok, "steps": len(self.steps)}))
        return 0 if ok else 1


class ApiOnlySmoke(Smoke):
    """The groups without a device: the server's side of every step, with the watch played over HTTP."""

    def _run(self) -> int:
        self.login()
        t0 = time.time()
        dev = self.api("POST", f"/api/v1/care/people/{self.person['id']}/devices", {"platform": "wearos"}, self.h)
        self.step("pairing_code_created", bool(dev.get("pairing_code")), t0, device_id=dev.get("id"), api_only=True)
        self.wrist = ApiWrist(self)
        if not self.wrist.pair(dev["pairing_code"], dev, ""):
            return self.finish()
        self.device = dev
        groups = select_groups(GROUPS, self.a.steps, self.a.until)
        self.extended(groups)
        return self.finish()

    def run(self) -> int:
        try:
            return self._run()
        except RuntimeError as exc:
            self.step("api_error", False, time.time(), error=str(exc)[:400])
            return self.finish()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apk", default=None, help="the debug APK (required unless --api-only)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--package", default="care.ira.watch.debug")
    ap.add_argument("--skip-install", action="store_true", help="the APK is already installed (a re-run)")
    ap.add_argument("--ui-timeout", type=float, default=300.0)
    ap.add_argument("--api", default=None, help="the backend on host port 8000, e.g. http://127.0.0.1:8000")
    ap.add_argument("--credentials", default=None, help="seed-format JSON (scripts/demo/seed_demo.py --out)")
    ap.add_argument("--steps", default="all", help="extended groups to run, comma separated, or all: " + ", ".join(GROUPS))
    ap.add_argument("--until", default=None, help="stop after this group")
    ap.add_argument("--api-only", action="store_true", help="no device: play the watch's calls over HTTP (needs --api)")
    ap.add_argument("--reminder-timeout", type=float, default=240.0,
                    help="how long a reminder or a requested check-in may take to reach the wrist (the server's cycle runs every 60 s, the watch polls every 60 s)")
    ap.add_argument("--boot-timeout", type=float, default=600.0)
    ap.add_argument("--sos-idle-timeout", type=float, default=780.0,
                    help="how long to wait for the Unpair button (the server reports a closed SOS for ten minutes)")
    a = ap.parse_args(argv)
    if a.api and not a.credentials:
        ap.error("--api needs --credentials")
    if a.api_only and not a.api:
        ap.error("--api-only needs --api")
    if not a.api_only and not a.apk:
        ap.error("--apk is required unless --api-only")
    try:
        select_groups(GROUPS, a.steps, a.until)
    except ValueError as exc:
        ap.error(str(exc))
    return (ApiOnlySmoke(a) if a.api_only else Smoke(a)).run()


if __name__ == "__main__":
    sys.exit(main())
