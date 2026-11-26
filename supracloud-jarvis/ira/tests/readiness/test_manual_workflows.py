"""The emulator and iOS simulator workflows run only when someone asks.

Both are long (an emulator run is 20 to 40 runner minutes; a macOS run would be
billed at ten times the Linux rate if the repository were private), so they run
when someone decides to: `workflow_dispatch` and nothing else. A push, pull_request,
schedule or any other trigger fails here. Every action they use is pinned to a
full commit, and each job has a time limit.
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
    if t != {"workflow_dispatch"}:
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


def test_generated_native_folders_are_not_tracked():
    ignore = (ROOT / "mobile" / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^/ios/$", ignore, re.M) and re.search(r"^/android/$", ignore, re.M)
    tracked = subprocess.run(["git", "ls-files", "--", "mobile/ios", "mobile/android"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    assert tracked == []
