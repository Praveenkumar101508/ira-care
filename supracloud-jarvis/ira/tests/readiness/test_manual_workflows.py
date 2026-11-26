"""The emulator and iOS simulator workflows run when someone asks, or for the pull requests that need them.

Both are long (an emulator run is 20 to 40 runner minutes). They run by hand
(`workflow_dispatch`) or when called by native-checks.yml (`workflow_call`) and
nothing else: a push, schedule or any other trigger on them fails here.
native-checks.yml runs them only on pull requests that change the watch or phone
apps (a path filter); standard runners are free while the repository is public
(docs/evidence/COMPUTE_RESOURCES.md). Every action is pinned to a full commit,
and each job has a time limit.
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github" / "workflows"
MANUAL = {"emulator.yml": 45, "ios-simulator.yml": 60, "phone-emulator.yml": 60}
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict) -> set[str]:
    """The workflow's triggers. PyYAML reads the bare key `on` as True."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set((on or {}).keys())


def manual_only_problems(doc: dict) -> list[str]:
    problems = []
    t = triggers(doc)
    if not t <= {"workflow_dispatch", "workflow_call"} or "workflow_dispatch" not in t:
        problems.append(f"triggers must be exactly workflow_dispatch, found {sorted(t)}")
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job.get("timeout-minutes"), int):
            problems.append(f"{job_id}: no timeout-minutes")
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if uses and not PINNED.match(uses):
                problems.append(f"{job_id}: action not pinned to a commit: {uses}")
    return problems


@pytest.mark.parametrize("name", sorted(MANUAL))
def test_workflow_is_manual_only_pinned_and_time_limited(name):
    assert manual_only_problems(_load(name)) == []


@pytest.mark.parametrize("name", sorted(MANUAL))
def test_job_time_limits(name):
    for job in _load(name)["jobs"].values():
        assert job["timeout-minutes"] <= MANUAL[name]


@pytest.mark.parametrize("extra", [
    {"push": {"branches": ["main"]}},
    {"pull_request": None},
    {"schedule": [{"cron": "0 3 * * *"}]},
    {"workflow_run": {"workflows": ["tests"]}},
])
def test_negative_control_any_other_trigger_is_refused(extra):
    doc = copy.deepcopy(_load("emulator.yml"))
    on = doc.get("on", doc.get(True))
    on.update(extra)
    assert any("triggers must be exactly workflow_dispatch" in p for p in manual_only_problems(doc))


def test_negative_control_unpinned_action_is_refused():
    doc = copy.deepcopy(_load("ios-simulator.yml"))
    doc["jobs"]["ios-simulator-build"]["steps"][0]["uses"] = "actions/checkout@v4"
    assert any("not pinned" in p for p in manual_only_problems(doc))


def test_negative_control_bare_push_string_trigger_is_refused():
    assert manual_only_problems({"on": "push", "jobs": {}}) != []
    assert manual_only_problems({True: ["workflow_dispatch", "push"], "jobs": {}}) != []


def test_ios_header_states_cost_and_manual_only():
    head = (WORKFLOWS / "ios-simulator.yml").read_text(encoding="utf-8").split("\nname:")[0]
    prose = " ".join(line.lstrip("# ").strip() for line in head.splitlines())
    assert "MANUAL ONLY" in prose
    assert "10 times the Linux rate" in prose
    assert "public" in prose
    assert "CODE_SIGNING_ALLOWED=NO" in (WORKFLOWS / "ios-simulator.yml").read_text(encoding="utf-8")


def test_emulator_enables_kvm_and_uploads_evidence():
    text = (WORKFLOWS / "emulator.yml").read_text(encoding="utf-8")
    assert 'KERNEL=="kvm", GROUP="kvm", MODE="0666"' in text
    assert "reactivecircus/android-emulator-runner@" in text
    assert "scripts/emulator/watch_smoke.py" in text
    assert "actions/upload-artifact@" in text
    assert (ROOT / "scripts" / "emulator" / "watch_smoke.py").exists()


# Runtime packages the demo backend needs (its worker scheduler imports them) that the test
# requirements leave out. Run 45753366726 failed because psutil was missing.
DEMO_RUNTIME_EXTRAS = ["psutil"]


def demo_backend_missing(step_run: str, test_requirements: str) -> list[str]:
    """Packages in DEMO_RUNTIME_EXTRAS that neither the test requirements nor the step install."""
    have = {line.split("==")[0].strip().lower() for line in test_requirements.splitlines()}
    return [p for p in DEMO_RUNTIME_EXTRAS
            if p not in have and f"grep -E '^{p}==' requirements.txt" not in step_run]


def _demo_step() -> str:
    steps = _load("emulator.yml")["jobs"]["wear-emulator"]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith("Start the demo backend"))


def test_demo_backend_installs_its_runtime_extras():
    reqs = (ROOT / "supracloud-jarvis" / "ira" / "requirements-test.txt").read_text(encoding="utf-8")
    assert demo_backend_missing(_demo_step(), reqs) == []
    pinned = (ROOT / "supracloud-jarvis" / "ira" / "requirements.txt").read_text(encoding="utf-8")
    assert all(re.search(rf"^{p}==\S+", pinned, re.M) for p in DEMO_RUNTIME_EXTRAS)


def test_negative_control_demo_backend_without_psutil_is_caught():
    step = _demo_step().replace("pip install -q \"$(grep -E '^psutil==' requirements.txt)\"", "")
    assert demo_backend_missing(step, "pytest==8.3.3\n") == ["psutil"]


def test_generated_native_folders_are_not_tracked():
    ignore = (ROOT / "mobile" / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^/ios/$", ignore, re.M) and re.search(r"^/android/$", ignore, re.M)
    tracked = subprocess.run(["git", "ls-files", "--", "mobile/ios", "mobile/android"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked == []


NATIVE_PATHS = {"watch/**", "mobile/**", "scripts/emulator/**", ".github/workflows/emulator.yml",
                ".github/workflows/ios-simulator.yml", ".github/workflows/native-checks.yml",
                ".github/workflows/phone-emulator.yml"}
NATIVE_WORKFLOWS = {"./.github/workflows/emulator.yml", "./.github/workflows/ios-simulator.yml",
                    "./.github/workflows/phone-emulator.yml"}


def native_checks_problems(doc: dict) -> list[str]:
    problems = []
    on = doc.get("on", doc.get(True)) or {}
    if set(on) != {"pull_request"}:
        problems.append(f"native-checks must run on pull_request only, found {sorted(on)}")
    paths = set(((on.get("pull_request") or {}) if isinstance(on, dict) else {}).get("paths") or [])
    if not paths or not paths <= NATIVE_PATHS:
        problems.append(f"native-checks needs a path filter limited to the native apps, found {sorted(paths)}")
    called = {job.get("uses") for job in (doc.get("jobs") or {}).values()}
    if called != NATIVE_WORKFLOWS:
        problems.append(f"native-checks must call exactly the native workflows, found {sorted(map(str, called))}")
    return problems


def test_native_checks_runs_only_for_native_changes():
    assert native_checks_problems(_load("native-checks.yml")) == []


@pytest.mark.parametrize("mutate", [
    lambda on: on["pull_request"].pop("paths"),
    lambda on: on["pull_request"]["paths"].append("**"),
    lambda on: on.update({"push": {"branches": ["main"]}}),
])
def test_negative_control_native_checks_widened_is_refused(mutate):
    doc = copy.deepcopy(_load("native-checks.yml"))
    mutate(doc.get("on", doc.get(True)))
    assert native_checks_problems(doc) != []


def _smoke_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("watch_smoke", ROOT / "scripts" / "emulator" / "watch_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_failure_reports_what_is_on_screen():
    smoke = _smoke_module()
    xml = ('<hierarchy><node text="Pair this watch" content-desc="" package="care.ira.watch.debug"/>'
           '<node text="" content-desc="Allow" package="com.google.android.permissioncontroller"/>'
           '<node text="Pair this watch" package="care.ira.watch.debug"/></hierarchy>')
    assert smoke.visible_text(smoke.parse_nodes(xml)) == ["Pair this watch", "Allow"]
    # Negative control: an empty dump (uiautomator failed) shows nothing, rather than raising.
    assert smoke.visible_text(smoke.parse_nodes("")) == []
    assert "self.diagnose(" in (ROOT / "scripts" / "emulator" / "watch_smoke.py").read_text(encoding="utf-8")


def test_smoke_reports_the_emulator_unplugged():
    # Wear OS puts its charging clock over every app while the emulator reports a charger.
    script = (ROOT / "scripts" / "emulator" / "watch_smoke.py").read_text(encoding="utf-8")
    assert "dumpsys battery unplug" in script and "wait_for_app_screen(" in script


def test_smoke_taps_the_button_not_its_label():
    smoke = _smoke_module()
    xml = ('<hierarchy><node class="android.view.View" clickable="true" bounds="[0,0][454,454]"/>'
           '<node class="android.view.View" clickable="true" enabled="true" bounds="[150,300][300,360]"/>'
           '<node text="Pair" clickable="false" bounds="[200,315][250,345]"/></hierarchy>')
    label = smoke.find(smoke.parse_nodes(xml), text="Pair")
    # The innermost clickable node around the label is the button, not the full-screen container.
    assert smoke.clickable_ancestor(xml, label)["bounds"] == "[150,300][300,360]"
    # Negative control: no label, nothing to tap.
    assert smoke.clickable_ancestor(xml, None) is None


def test_smoke_fails_when_the_app_crashed_on_the_way():
    smoke = _smoke_module()
    # The crash from run 45760714043, as the crash buffer prints it.
    log = ("11-26 05:22:33.830  3802  3802 E AndroidRuntime: FATAL EXCEPTION: main\n"
           "11-26 05:22:33.830  3802  3802 E AndroidRuntime: Process: care.ira.watch.debug, PID: 3802\n"
           "11-26 05:22:33.834  3802  3802 E AndroidRuntime: Caused by: java.lang.SecurityException: Starting FGS\n")
    assert smoke.app_crashes(log, "care.ira.watch.debug") == [
        "11-26 05:22:33.830  3802  3802 E AndroidRuntime: Process: care.ira.watch.debug, PID: 3802"]
    # Negative controls: another app's crash, and an empty buffer, are not this app's.
    other = log.replace("care.ira.watch.debug", "com.google.android.wearable.sysui")
    assert smoke.app_crashes(other, "care.ira.watch.debug") == []
    assert smoke.app_crashes("", "care.ira.watch.debug") == []
    assert '"no_app_crash"' in (ROOT / "scripts" / "emulator" / "watch_smoke.py").read_text(encoding="utf-8")


def test_smoke_brings_the_app_back_when_the_watch_face_covers_it(tmp_path):
    smoke = _smoke_module()

    class Args:
        out, adb, serial, package = str(tmp_path), "adb", "emulator-5554", "care.ira.watch.debug"

    s = smoke.Smoke(Args())
    calls = []
    screens = iter(["face", "face", "app"])
    state = {"screen": "face"}

    def fake_adb(*args, **kw):
        calls.append(args)
        if args[:2] == ("shell", "dumpsys"):
            return f"  mCurrentFocus=Window{{1 u0 {'care.ira.watch.debug' if state['screen'] == 'app' else 'com.google.android.wearable.sysui'}/X}}"
        if args[:3] == ("shell", "am", "start"):
            state["screen"] = next(screens)
        return ""

    s.adb = fake_adb
    s.wait_for = lambda timeout, **kw: {"text": "Pair this watch"} if state["screen"] == "app" else None
    assert s.wait_for_app_screen(600, text="Pair this watch") == {"text": "Pair this watch"}
    assert sum(1 for c in calls if c[:3] == ("shell", "am", "start")) == 3

    # Negative control: a screen that never comes back stops after max_relaunch, returning None.
    state["screen"] = "face"
    screens = iter(["face"] * 10)
    calls.clear()
    s.wait_for = lambda timeout, **kw: None
    import time as _t
    real = _t.time
    clock = [real()]
    smoke.time.time = lambda: clock.__setitem__(0, clock[0] + 40) or clock[0]
    try:
        assert s.wait_for_app_screen(600, max_relaunch=2, text="Pair this watch") is None
    finally:
        smoke.time.time = real
    assert sum(1 for c in calls if c[:3] == ("shell", "am", "start")) == 2


def test_phone_smoke_checks_the_sign_in_screen():
    import importlib.util
    spec = importlib.util.spec_from_file_location("phone_smoke", ROOT / "scripts" / "emulator" / "phone_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    screen = [{"text": mod.SIGN_IN_TEXT}, {"text": "", "content-desc": "Username", "class": "android.widget.EditText"},
              {"text": "", "content-desc": "Password", "class": "android.widget.EditText"}, {"text": "Sign in"}]
    assert mod.sign_in_screen_problems(screen) == []
    # Negative controls: a blank screen and a screen without the button are refused.
    assert mod.sign_in_screen_problems([]) == ["no sign-in explanation", "no Username field", "no Password field",
                                               "no Sign in button"]
    assert mod.sign_in_screen_problems(screen[:3]) == ["no Sign in button"]
    # The text the smoke waits for is the one the app shows.
    login = (ROOT / "mobile" / "src" / "screens" / "LoginScreen.tsx").read_text(encoding="utf-8")
    assert mod.SIGN_IN_TEXT in login


def test_smoke_retries_no_connection_but_not_a_refused_code():
    smoke = _smoke_module()
    script = (ROOT / "scripts" / "emulator" / "watch_smoke.py").read_text(encoding="utf-8")
    assert 1 <= smoke.NO_CONNECTION_RETRIES <= 3
    # A refused code ends the step at once: retrying it would hide a server or pairing defect.
    refused = script.index('find(nodes, text="That code was not recognised.")')
    assert script[refused:refused + 120].count("gone = False") == 1
    assert "no_connection_retries >= NO_CONNECTION_RETRIES" in script
