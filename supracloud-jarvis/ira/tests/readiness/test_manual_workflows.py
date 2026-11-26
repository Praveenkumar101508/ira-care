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
MANUAL = {"emulator.yml": 45, "ios-simulator.yml": 60}
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
                ".github/workflows/ios-simulator.yml", ".github/workflows/native-checks.yml"}


def native_checks_problems(doc: dict) -> list[str]:
    problems = []
    on = doc.get("on", doc.get(True)) or {}
    if set(on) != {"pull_request"}:
        problems.append(f"native-checks must run on pull_request only, found {sorted(on)}")
    paths = set(((on.get("pull_request") or {}) if isinstance(on, dict) else {}).get("paths") or [])
    if not paths or not paths <= NATIVE_PATHS:
        problems.append(f"native-checks needs a path filter limited to the native apps, found {sorted(paths)}")
    called = {job.get("uses") for job in (doc.get("jobs") or {}).values()}
    if called != {"./.github/workflows/emulator.yml", "./.github/workflows/ios-simulator.yml"}:
        problems.append(f"native-checks must only call the two native workflows, found {sorted(map(str, called))}")
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
