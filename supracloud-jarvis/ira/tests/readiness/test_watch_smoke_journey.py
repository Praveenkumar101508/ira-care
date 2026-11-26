"""The extended Wear OS emulator journey (scripts/emulator/watch_smoke.py): its pure helpers, with
recorded UI dumps and fake adb and API transports, so the logic is proved here and the KVM runner
only has to supply the device. Each check has a negative control that would catch the helper
accepting what it must refuse."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "emulator" / "watch_smoke.py"
PKG = "care.ira.watch.debug"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("watch_smoke_journey", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A home-screen dump as the Wear OS emulator (454 x 454, 320 dpi) produces it: Compose merges each
# button's description onto its clickable node; the system bars belong to other packages.
HOME_DUMP = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" class="android.widget.FrameLayout" package="{PKG}" clickable="false" bounds="[0,0][454,454]">
<node index="0" text="All well" content-desc="" class="android.view.View" package="{PKG}" clickable="false" bounds="[40,80][414,120]"/>
<node index="1" text="I'm OK" content-desc="I'm OK. Answers the question, or says you are fine after a fall." class="android.view.View" package="{PKG}" clickable="true" enabled="true" bounds="[20,140][434,236]"/>
<node index="2" text="Help" content-desc="Help. Tells your family and carers you need them now." class="android.view.View" package="{PKG}" clickable="true" enabled="true" bounds="[20,252][434,348]"/>
<node index="3" text="Unpair watch…" content-desc="Unpair this watch. Asks before doing anything." class="android.view.View" package="{PKG}" clickable="true" enabled="true" bounds="[20,360][434,448]"/>
</node>
<node index="1" text="" content-desc="" class="android.widget.ImageView" package="com.google.android.wearable.sysui" clickable="true" bounds="[200,0][254,20]"/>
</hierarchy>"""


def test_step_groups_are_selected_in_canonical_order_and_cut_by_until(smoke):
    g = smoke.GROUPS
    assert smoke.select_groups(g, "all", None) == list(g)
    assert smoke.select_groups(g, None, None) == list(g)
    assert smoke.select_groups(g, "fall,medication", None) == ["medication", "fall"]
    assert smoke.select_groups(g, "all", "fall") == ["medication", "checkin", "fall"]
    assert smoke.select_groups(g, "unpair,checkin", "fall") == ["checkin"]
    # Negative controls: a misspelt group is an error, never a silent skip.
    with pytest.raises(ValueError):
        smoke.select_groups(g, "medicaton", None)
    with pytest.raises(ValueError):
        smoke.select_groups(g, "all", "falls")


def test_accessibility_checks_on_a_recorded_home_dump(smoke):
    nodes = smoke.app_nodes(smoke.parse_nodes(HOME_DUMP), PKG)
    assert len(nodes) == 5   # the system bar's unlabelled icon is not the app's
    assert smoke.unlabelled_clickables(nodes) == []
    assert smoke.small_targets(nodes, smoke.min_target_px(320)) == []
    # Negative controls: a button without words, and a button too small for a finger.
    bare = HOME_DUMP.replace('text="Help" content-desc="Help. Tells your family and carers you need them now."', 'text="" content-desc=""')
    assert [n["bounds"] for n in smoke.unlabelled_clickables(smoke.app_nodes(smoke.parse_nodes(bare), PKG))] == ["[20,252][434,348]"]
    small = HOME_DUMP.replace("[20,252][434,348]", "[20,252][434,300]")
    found = smoke.small_targets(smoke.app_nodes(smoke.parse_nodes(small), PKG), smoke.min_target_px(320))
    assert [(n["text"], n["height"]) for n in found] == [("Help", 48)]
    # A text field is labelled by the hint beside it, so it is not an unlabelled clickable.
    field = '<node text="" content-desc="" class="android.widget.EditText" package="%s" clickable="true" bounds="[20,200][434,240]"/>' % PKG
    assert smoke.unlabelled_clickables(smoke.parse_nodes(field)) == []


def test_touch_target_threshold_is_the_projects_44_dp_with_a_48_px_floor(smoke):
    assert smoke.MIN_TARGET_DP == 44 and smoke.MIN_TARGET_PX_FLOOR == 48
    assert smoke.density_dpi("Physical density: 320\n") == 320
    assert smoke.density_dpi("Physical density: 320\nOverride density: 280\n") == 280
    assert smoke.density_dpi("") is None
    assert smoke.min_target_px(320) == 88
    assert smoke.min_target_px(160) == 48
    # Negative control: an unknown or absurd density never lowers the floor.
    assert smoke.min_target_px(None) == 48 and smoke.min_target_px(10) == 48
    # The same rule the JVM test keeps on the source: no home-screen button under 44 dp.
    assert "at least 44 dp tall" in (ROOT / "watch/app/src/test/java/care/ira/watch/ui/AccessibilityTest.kt").read_text(encoding="utf-8")


def test_dose_checks_from_server_json(smoke):
    med, other, confirm = "m-1", "m-2", "c-9"
    events = [
        {"medication_id": med, "status": "taken", "confirmed_via": "checkin", "source": "self_report", "checkin_id": confirm},
        {"medication_id": other, "status": "taken", "confirmed_via": "carer", "source": "carer_report", "checkin_id": None},
        {"medication_id": med, "status": "unknown", "confirmed_via": "none", "source": "system", "checkin_id": "c-1"},
    ]
    assert len(smoke.taken_doses(events, med)) == 1
    assert smoke.taken_doses([], med) == []
    assert smoke.watch_dose(events, med, confirm)["checkin_id"] == confirm
    # Negative controls (C-1): a dose written by a carer, through another check-in, or not taken,
    # is never the wrist's two-step confirmation.
    assert smoke.watch_dose(events, other, confirm) is None
    assert smoke.watch_dose(events, med, "c-1") is None
    carer_written = [dict(events[0], confirmed_via="carer", source="carer_report")]
    assert smoke.watch_dose(carer_written, med, confirm) is None


def test_reminder_checkins_are_found_by_reminder_and_status(smoke):
    rows = [
        {"id": "a", "reminder_id": "r1", "status": "answered", "scheduled_at": "2026-11-26T10:00:00+00:00"},
        {"id": "b", "reminder_id": "r1", "status": "asked", "scheduled_at": "2026-11-26T10:01:00+00:00"},
        {"id": "c", "reminder_id": "r2", "status": "asked", "scheduled_at": "2026-11-26T10:02:00+00:00"},
        {"id": "d", "reminder_id": None, "status": "asked", "scheduled_at": "2026-11-26T10:03:00+00:00"},
    ]
    assert smoke.checkin_for_reminder(rows, "r1", ("asked", "repeated"))["id"] == "b"
    assert smoke.checkin_for_reminder(rows, "r1", ("answered",))["id"] == "a"
    # The confirmation question is a second check-in for the same reminder: the first is excluded.
    assert smoke.checkin_for_reminder(rows, "r1", ("asked", "answered"), exclude=("b",))["id"] == "a"
    # Negative controls: another reminder's row, or a plain question, never passes for this reminder's.
    assert smoke.checkin_for_reminder(rows, "r3", ("asked",)) is None
    assert smoke.checkin_for_reminder(rows, "r1", ("missed",)) is None


def test_fall_alert_checks_count_one_alert_with_two_detections(smoke):
    pid = "p-1"
    alert = {"id": "A", "person_id": pid, "kind": "suspected_fall", "status": "asking", "severity": "sos",
             "details": {"detections": [{"incident_id": "i", "detection": 1}, {"incident_id": "i", "detection": 2}]}}
    others = [
        {"id": "B", "person_id": pid, "kind": "suspected_fall", "status": "resolved", "severity": "sos", "details": {}},
        {"id": "C", "person_id": "p-2", "kind": "help_requested", "status": "open", "severity": "sos", "details": {}},
        {"id": "N", "person_id": pid, "kind": "watch_unpaired", "status": "open", "severity": "notice", "details": {}},
    ]
    assert [a["id"] for a in smoke.sos_alerts([alert] + others, pid)] == ["A"]
    assert len(smoke.detections(alert)) == 2
    assert smoke.detections(None) == [] and smoke.detections({"details": None}) == []
    # Negative control: a second unresolved fall alert for the same person is the duplicate D-092 forbids.
    dup = dict(alert, id="A2")
    assert len(smoke.sos_alerts([alert, dup], pid)) == 2
    # New alerts since a moment: notices do not count, earlier ids do not count.
    before = {"A", "B"}
    assert [a["id"] for a in smoke.new_alerts(before, [alert] + others + [{"id": "D", "person_id": pid, "kind": "help_requested", "severity": "sos", "status": "open"}])] == ["C", "D"]
    assert smoke.new_alerts({"A", "B", "C", "D", "N"}, [alert] + others) == []


def test_wrist_words_match_the_watch_sources(smoke):
    face = (ROOT / "watch/app/src/main/java/care/ira/watch/ui/FaceText.kt").read_text(encoding="utf-8")
    sos = (ROOT / "watch/app/src/main/java/care/ira/watch/outbox/SosState.kt").read_text(encoding="utf-8")
    main = (ROOT / "watch/app/src/main/java/care/ira/watch/ui/MainActivity.kt").read_text(encoding="utf-8")
    unpairing = (ROOT / "watch/app/src/main/java/care/ira/watch/pairing/Unpairing.kt").read_text(encoding="utf-8")
    strings = (ROOT / "watch/app/src/main/res/values/strings.xml").read_text(encoding="utf-8")
    assert smoke.HELP_RECEIVED in sos and smoke.WAITING_FOR_CONNECTION in sos
    assert smoke.OFFLINE_LINE in main and smoke.HELP_DESC in main
    assert smoke.UNPAIR_WARNING in unpairing
    assert smoke.PAIR_TITLE in strings and smoke.PERMISSION_TITLE in strings
    assert 'Help in $fallCountdown s' in face and smoke.FALL_LINE == "Help in"
    assert smoke.responder_line("Sravani Perni") == "Sravani Perni is on the way."
    assert '"$name is on the way."' in sos
    # The API-only wrist computes the face the way FaceText does.
    assert smoke.face_text(12, None, None, None, True) == "Did you fall? Are you OK?\nHelp in 12 s"
    assert smoke.face_text(None, "Help request received.", "A question?", None, True) == "Help request received."
    assert smoke.face_text(None, None, None, None, False).startswith("No connection.")
    assert smoke.sos_text_of("on_the_way", "Sravani Perni") == "Sravani Perni is on the way."
    assert smoke.sos_text_of("idle", None) is None and smoke.sos_text_of("asking", None) is None
    # Negative control: an unknown server state reads as received, never as help coming.
    assert smoke.sos_text_of("something_new", "X") == "Help request received."
    assert smoke.reminder_buttons("medication") == ["Done", "Later", "Not today"]
    assert smoke.reminder_buttons("reminder") == ["Done", "Later"] and smoke.reminder_buttons(None) == []


def test_device_state_helpers(smoke):
    assert smoke.boot_completed("1\n") and not smoke.boot_completed("") and not smoke.boot_completed("0")
    running = f"  * ServiceRecord{{3f2 u0 {PKG}/care.ira.watch.service.CareForegroundService}}\n    isForeground=true"
    assert smoke.service_running(running, PKG)
    # Negative controls: another app's service, or no service at all.
    assert not smoke.service_running(running.replace(PKG, "care.ira.watch"), PKG)
    assert not smoke.service_running("ACTIVITY MANAGER SERVICES (dumpsys activity services)\n  (nothing)\n", PKG)
    args = smoke.fall_broadcast(PKG, 3.2, 20, 1_700_000_000_000)
    assert args[:4] == ["shell", "am", "broadcast", "-n"]
    assert args[4] == f"{PKG}/care.ira.watch.debug.SyntheticFallReceiver"
    assert "care.ira.watch.debug.SYNTHETIC_FALL" in args and "--ef" in args and "--ei" in args and "--el" in args
    receiver = (ROOT / "watch/app/src/debug/java/care/ira/watch/debug/SyntheticFallReceiver.kt").read_text(encoding="utf-8")
    for const in ('ACTION = "care.ira.watch.debug.SYNTHETIC_FALL"', 'EXTRA_PEAK_G = "peak_g"', 'EXTRA_STILL_SECONDS = "still_seconds"', 'EXTRA_IMPACT_AT = "impact_at"'):
        assert const in receiver


def test_adb_wrist_taps_the_button_and_reads_the_screen_from_a_recorded_dump(smoke, tmp_path):
    class Args:
        out, adb, serial, package, api_only = str(tmp_path), "adb", "emulator-5554", PKG, False

    s = smoke.Smoke(Args())
    calls = []

    def fake_adb(*args, **kw):
        calls.append(args)
        if args[:2] == ("exec-out", "cat"):
            return HOME_DUMP
        if args[:3] == ("shell", "wm", "density"):
            return "Physical density: 320\n"
        if args[:4] == ("shell", "dumpsys", "activity", "services"):
            return f"ServiceRecord{{1 u0 {PKG}/care.ira.watch.service.CareForegroundService}}"
        if args[:3] == ("shell", "am", "broadcast"):
            return "Broadcasting: Intent { act=care.ira.watch.debug.SYNTHETIC_FALL }\nBroadcast completed: result=0\n"
        return ""

    s.adb = fake_adb
    w = smoke.AdbWrist(s)
    assert w.tap("Help") == "Help"
    assert ("shell", "input", "tap", "227", "300") in calls      # the centre of the Help button
    assert w.tap("Unpair watch…", "Unpair this watch") == "Unpair watch…"
    assert w.wait_text("All well", 1) is not None
    assert w.density() == 320 and w.service_running()
    assert w.inject_fall(3.2, 20, 1)
    assert w.tap("Not on this screen") is None
    assert w.wait_text("Help in", 0.1) is None
    w.network(False)
    assert ("shell", "svc", "wifi", "disable") in calls and ("shell", "svc", "data", "disable") in calls


def test_api_wrist_plays_the_watch_against_a_fake_server(smoke, tmp_path):
    """The API-only wrist makes the calls the app makes: the outbox holds Help while offline, sends
    it once with its key when back, and the fall countdown attaches a second detection (D-092)."""
    class Args:
        out, api, api_only, package = str(tmp_path), "http://fake", True, PKG

    s = smoke.Smoke(Args())
    posts = []
    state = {"sos": {"state": "idle"}, "due": {"checkin": None}}

    def fake_api_status(method, path, body=None, headers=None, form=False):
        if method == "POST":
            posts.append((path, body, (headers or {}).get("X-Idempotency-Key")))
            if path.endswith("/devices/pair"):
                return 200, {"id": "dev-1", "device_token": "tok", "person_id": "p", "status": "paired"}
            if path.endswith("/help"):
                state["sos"] = {"state": "received"}
                return 201, {"id": "al-1"}
            return 201 if path.endswith("/fall") else 200, {}
        if path.endswith("/devices/me"):
            return 200, {"sos": state["sos"], "fall": {"fall_countdown_s": 1}}
        if path.endswith("/checkins/due"):
            return 200, state["due"]
        return 200, {}

    s.api_status = fake_api_status
    w = smoke.ApiWrist(s)
    assert w.pair("ABCD-1234", {"id": "dev-1"}, "") and w.token == "tok"
    assert s.steps[-1]["step"] == "paired" and s.steps[-1]["ok"]
    # Offline: Help is written to the outbox and the wrist says so; nothing reaches the server.
    w.network(False)
    assert w.wait_text(smoke.OFFLINE_LINE, 0.1) is not None
    assert w.tap("Help") == "Help"
    assert w.wait_text(smoke.WAITING_FOR_CONNECTION, 0.1) is not None
    assert [p for p in posts if p[0].endswith("/help")] == []
    # Back online: sent once, with its idempotency key; the server's word is shown.
    w.network(True)
    helps = [p for p in posts if p[0].endswith("/help")]
    assert len(helps) == 1 and helps[0][2] is not None and w.outbox == []
    assert w.wait_text(smoke.HELP_RECEIVED, 0.1) is not None
    # A fall, then a separate detection: the same incident, detection 2, one alert's worth of keys.
    state["sos"] = {"state": "idle"}
    w.refresh()
    assert w.inject_fall(3.2, 20, 10_000) and w.inject_fall(3.3, 20, 10_500) and w.inject_fall(3.4, 20, 20_000)
    falls = [p for p in posts if p[0].endswith("/devices/me/fall")]
    assert [p[1]["detection"] for p in falls] == [1, 2]
    assert len({p[1]["incident_id"] for p in falls}) == 1 and falls[0][2].endswith("-d1") and falls[1][2].endswith("-d2")
    assert w.wait_text("Help in", 0.1) is not None
    # The countdown lapses: Help via fall for the same incident, once.
    w.countdown["deadline"] = 0
    w.refresh()
    fall_help = [p for p in posts if p[0].endswith("/help") and (p[1] or {}).get("via") == "fall"]
    assert len(fall_help) == 1 and fall_help[0][1]["incident_id"] == falls[0][1]["incident_id"]
    # A reminder's buttons: "Later" answers the open check-in with the choice, by button.
    state["due"] = {"checkin": {"id": "c-1", "question_text": "Ada, it's time for the tablet.", "question_kind": "medication", "reminder_id": "r-1"}}
    state["sos"] = {"state": "idle"}
    assert w.wait_text("Later", 0.1) is not None
    assert w.tap("Later") == "Later"
    answers = [p for p in posts if "/checkins/c-1/answer" in p[0]]
    assert answers and answers[0][1] == {"text": "", "via": "button", "choice": "later"}
    # Negative control: once the question is gone, a reminder button is not on the screen, so it is
    # not tapped and nothing is sent.
    state["due"] = {"checkin": None}
    n = len(posts)
    assert w.tap("Not today") is None and len(posts) == n


def test_smoke_still_names_the_eleven_core_steps_and_guards_each_group(smoke):
    text = SCRIPT.read_text(encoding="utf-8")
    for name in ("prepare_device", "install", "already_installed", "launch", "pairing_screen_shown", "pairing_code_created",
                 "type_code", "paired", "home_help_button_visible", "help_received_on_wrist", "help_alert_on_server", "no_app_crash"):
        assert f'"{name}"' in text or f'f"{{prefix}}{name}"' in text, name
    for g in smoke.GROUPS:
        assert f"def journey_{g}(" in text
    # Every group runs under the guard that records the failure and goes on to the next group.
    assert 'self.step(f"{g}_error", False, t0' in text
    assert "voice_exercised=False" in text


def test_workflow_passes_the_steps_input_and_uploads_evidence():
    import yaml
    doc = yaml.safe_load((ROOT / ".github/workflows/emulator.yml").read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert on["workflow_dispatch"]["inputs"]["steps"]["default"] == "all"
    assert on["workflow_call"]["inputs"]["steps"]["default"] == "all"
    steps = doc["jobs"]["wear-emulator"]["steps"]
    emu = next(s for s in steps if "android-emulator-runner" in s.get("uses", ""))
    assert "--steps" in emu["with"]["script"] and "inputs.steps" in emu["with"]["script"]
    assert any("upload-artifact" in s.get("uses", "") for s in steps)
    assert json.dumps(doc["jobs"]["wear-emulator"]["timeout-minutes"]) == "45"
