"""The licences of what ships are inventoried and checked against the policy (K.d).

`scripts/licence_check.py` classifies every licence in the Python lock and the production npm
trees of the dashboard and the phone app; `docs/security/LICENCES.json` is the committed
inventory and `docs/security/licence_exceptions.json` the reviewed exceptions. These tests run
without node_modules or the full Python lock installed: they check the classifier, the committed
inventory against the policy and the lock, the exceptions, and the CI wiring. Every check has a
negative control that breaks a copy and expects the check to fail."""
from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))

import licence_check as lc  # noqa: E402

ALLOWED = [
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "ISC",
    "0BSD",
    "MPL-2.0",
    "Python-2.0",
    "BlueOak-1.0.0",
    "MIT OR GPL-3.0",                    # dual: we take the permissive option
    "GPL-3.0-or-later OR Apache-2.0",    # caldav's real declaration
    "(BSD-3-Clause OR GPL-2.0)",         # node-forge's real declaration
    "MPL-2.0 AND (Apache-2.0 OR MIT)",   # orjson's real declaration
    "Apache-2.0 WITH LLVM-exception",
    "Apache 2.0",                        # legacy free text
    "Apache License, Version 2.0",
    "Modified BSD License",
    "mit",
]

FORBIDDEN = [
    "GPL-3.0-only",
    "GPL-2.0-or-later",
    "AGPL-3.0",
    "AGPL-3.0-or-later",
    "LGPL-3.0-or-later",                 # weak copyleft needs a reviewed exception
    "GNU Lesser General Public License v3 (LGPLv3)",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0",
    "SSPL-1.0",
    "BUSL-1.1",
    "MIT AND GPL-3.0",                   # AND needs every part allowed
    "GPL-3.0 OR AGPL-3.0",               # OR of two forbidden options is still forbidden
    "GPL-2.0-only WITH Classpath-exception-2.0",
    "Proprietary",
    "SEE LICENSE IN LICENSE.txt",
    "UNLICENSED",                        # npm's word for "all rights reserved"
    "UNKNOWN",
    "",
    None,
    "MIT OR",                            # malformed expressions are unknown
    "(MIT",
]


@pytest.mark.parametrize("declared", ALLOWED)
def test_permissive_licences_and_permissive_options_are_allowed(declared):
    v = lc.classify(declared)
    assert v.allowed, (declared, v.reason)
    assert v.reason == ""


@pytest.mark.parametrize("declared", FORBIDDEN)
def test_copyleft_non_commercial_and_unknown_licences_are_forbidden(declared):
    v = lc.classify(declared)
    assert not v.allowed, declared
    assert v.reason, "a forbidden verdict says why"


def test_missing_licence_is_recorded_as_noassertion_not_as_blank():
    for missing in ("", None, "   "):
        assert lc.classify(missing).licence == lc.NOASSERTION


def test_the_reason_names_the_family_that_forbids_it():
    assert "AGPL" in lc.classify("AGPL-3.0").reason
    assert "non-commercial" in lc.classify("CC-BY-NC-4.0").reason
    assert "not on the allowed list" in lc.classify("Proprietary").reason


# --- reading the declarations ---------------------------------------------------------------

def _meta(**fields) -> Message:
    m = Message()
    for k, v in fields.items():
        for item in v if isinstance(v, list) else [v]:
            m[k.replace("_", "-")] = item
    return m


def test_python_metadata_prefers_the_spdx_expression():
    m = _meta(License_Expression="MIT", License="GPL",
              Classifier=["License :: OSI Approved :: BSD License"])
    assert lc.python_declared(m) == ("MIT", "License-Expression")


def test_python_licence_text_in_the_license_field_falls_back_to_classifiers():
    text = "Copyright (c) 2005, NumPy Developers.\nAll rights reserved.\nRedistribution ..."
    m = _meta(License=text, Classifier=["License :: OSI Approved :: BSD License"])
    assert lc.python_declared(m) == ("BSD", "Classifier")
    # Negative control: the same text with no classifier is not read as a licence.
    declared, _ = lc.python_declared(_meta(License=text))
    assert not lc.classify(declared).allowed


def test_python_lgpl_classifier_is_caught():
    lgpl = "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)"
    m = _meta(Classifier=[lgpl])
    declared, source = lc.python_declared(m)
    assert source == "Classifier" and not lc.classify(declared).allowed


def test_python_package_with_no_licence_at_all_is_forbidden():
    declared, source = lc.python_declared(_meta(Name="x"))
    assert (declared, source) == ("", "none")
    assert not lc.classify(declared).allowed


@pytest.mark.parametrize("manifest, declared", [
    ({"license": "MIT"}, "MIT"),
    ({"license": {"type": "ISC"}}, "ISC"),
    ({"licenses": "MIT"}, "MIT"),
    ({"licenses": [{"type": "Apache 2.0"}]}, "Apache-2.0"),
    ({"licenses": [{"type": "MIT"}, {"type": "GPL-2.0"}]}, "(MIT OR GPL-2.0)"),
    ({}, ""),
])
def test_npm_licence_fields_in_every_spelling(manifest, declared):
    assert lc.npm_declared(manifest)[0] == declared


# --- the committed inventory -----------------------------------------------------------------

def _inventory() -> dict:
    return json.loads(lc.INVENTORY.read_text(encoding="utf-8"))


def _exceptions() -> list[dict]:
    """The exceptions for the inventory's own ecosystems. Exceptions for what only the built
    artefacts contain (image-*, native-libraries, os-linked) are checked against the image
    snapshots by tests/readiness/test_supply_chain.py."""
    return [x for x in lc.load_exceptions() if x["ecosystem"] in lc.ECOSYSTEMS]


def _fake(eco: str, name: str, declared: str) -> tuple[str, dict]:
    return f"{name}@1.0.0", {"name": name, "version": "1.0.0", "declared": declared,
                             "source": "package.json license", "licence": declared,
                             "status": "allowed"}


def test_the_inventory_covers_every_ecosystem_it_claims():
    inv = _inventory()
    assert set(inv["ecosystems"]) == set(lc.ECOSYSTEMS)
    for eco, sec in inv["ecosystems"].items():
        assert sec["packages"], eco
        for key, e in sec["packages"].items():
            assert key == f"{e['name']}@{e['version']}"
    # The production trees always contain the frameworks the apps are built on.
    assert any(k.startswith("next@") for k in inv["ecosystems"]["npm-dashboard"]["packages"])
    assert any(k.startswith("react-native@") for k in inv["ecosystems"]["npm-mobile"]["packages"])
    assert any(k.startswith("fastapi@") for k in inv["ecosystems"]["python"]["packages"])
    assert any(k.startswith("org.tensorflow:tensorflow-lite@")
               for k in inv["ecosystems"]["gradle-watch"]["packages"])


def test_the_inventory_is_written_deterministically():
    text = lc.INVENTORY.read_text(encoding="utf-8")
    assert text == lc.dumps(json.loads(text))
    # Negative control: the same data written another way is caught.
    assert json.dumps(json.loads(text), indent=1) + "\n" != text


def test_nothing_forbidden_ships_without_a_reviewed_exception():
    assert lc.policy_problems(_inventory(), _exceptions()) == []
    statuses = {e["status"] for s in _inventory()["ecosystems"].values()
                for e in s["packages"].values()}
    assert statuses <= {"allowed", "excepted"}


def test_negative_control_an_injected_agpl_package_fails_the_check():
    inv = copy.deepcopy(_inventory())
    key, fake = _fake("npm-mobile", "fake-agpl-lib", "AGPL-3.0-only")
    inv["ecosystems"]["npm-mobile"]["packages"][key] = fake
    problems = lc.policy_problems(inv, _exceptions())
    assert any("fake-agpl-lib" in p and "FORBIDDEN" in p for p in problems), problems


def test_negative_control_a_package_with_no_licence_fails_the_check():
    inv = copy.deepcopy(_inventory())
    key, fake = _fake("python", "mystery", "")
    fake["licence"] = lc.NOASSERTION
    inv["ecosystems"]["python"]["packages"][key] = fake
    assert any("mystery" in p for p in lc.policy_problems(inv, _exceptions()))


def test_negative_control_a_hand_edited_verdict_is_caught():
    inv = copy.deepcopy(_inventory())
    key, fake = _fake("npm-dashboard", "sneaky", "GPL-3.0-only")
    inv["ecosystems"]["npm-dashboard"]["packages"][key] = fake  # recorded as "allowed"
    problems = lc.policy_problems(inv, _exceptions())
    assert any("sneaky" in p and "does not follow from the policy" in p for p in problems)


def test_the_cli_check_fails_on_a_tampered_inventory(tmp_path, monkeypatch, capsys):
    """--check end to end, offline: nothing is visible, so only the committed file is judged."""
    monkeypatch.setattr(lc, "visible", lambda eco: (False, "not installed in this test"))
    good = tmp_path / "LICENCES.json"
    good.write_text(lc.INVENTORY.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lc, "INVENTORY", good)
    assert lc.main(["--check"]) == 0
    inv = json.loads(good.read_text(encoding="utf-8"))
    key, fake = _fake("npm-dashboard", "fake-agpl-lib", "AGPL-3.0-or-later")
    fake.update(status="forbidden", reason=lc.classify("AGPL-3.0-or-later").reason)
    inv["ecosystems"]["npm-dashboard"]["packages"][key] = fake
    good.write_text(lc.dumps(inv), encoding="utf-8")
    assert lc.main(["--check"]) == 1
    assert "fake-agpl-lib" in capsys.readouterr().out
    # Naming an ecosystem that cannot be seen is a failure, not a skip.
    assert lc.main(["--check", "--only", "npm-mobile"]) == 1


# --- the Python section follows the lock -----------------------------------------------------

def test_the_python_section_matches_the_lock():
    assert lc.lock_problems(_inventory()) == []
    assert len(_inventory()["ecosystems"]["python"]["packages"]) == len(lc.locked())


def test_negative_control_a_stale_python_version_is_caught():
    inv = copy.deepcopy(_inventory())
    pkgs = inv["ecosystems"]["python"]["packages"]
    key = next(k for k in pkgs if k.startswith("fastapi@"))
    e = pkgs.pop(key)
    e["version"] = "0.0.1"
    pkgs[f"fastapi@{e['version']}"] = e
    problems = lc.lock_problems(inv)
    assert any("in the lock but not the inventory" in p for p in problems)
    assert any("in the inventory but not the lock" in p for p in problems)


# --- the exceptions --------------------------------------------------------------------------

def test_every_exception_has_a_reason_and_matches_a_shipped_package():
    exceptions = _exceptions()
    assert exceptions, "the exceptions file is read"
    assert lc.exception_problems(exceptions) == []
    inv = _inventory()
    for x in exceptions:
        hits = [e for e in inv["ecosystems"][x["ecosystem"]]["packages"].values()
                if e["name"] == x["package"] and e["licence"] == x["licence"]]
        assert hits and all(e["status"] == "excepted" for e in hits), x["package"]


def test_negative_control_an_exception_without_a_reason_is_refused():
    bad = copy.deepcopy(_exceptions())
    bad[0]["reason"] = "fine"
    assert any("reason" in p for p in lc.exception_problems(bad))
    del bad[1]["reason"]
    assert any("fields must be exactly" in p for p in lc.exception_problems(bad))


def test_negative_control_an_exception_for_a_package_that_is_gone_is_refused():
    bad = copy.deepcopy(_exceptions()) + [{
        "ecosystem": "npm-mobile", "package": "left-years-ago", "licence": "GPL-3.0-only",
        "reason": "Was used by the old build and has since been removed from the app."}]
    assert any("left-years-ago" in p and "matches no package" in p
               for p in lc.policy_problems(_inventory(), bad))


def test_negative_control_an_exception_for_an_allowed_licence_is_refused():
    bad = copy.deepcopy(_exceptions()) + [{
        "ecosystem": "npm-dashboard", "package": "next", "licence": "MIT",
        "reason": "An exception that is not needed, because MIT is already allowed."}]
    assert any("is not needed" in p for p in lc.policy_problems(_inventory(), bad))


def test_negative_control_removing_an_exception_makes_the_package_forbidden():
    exceptions = _exceptions()
    dropped = exceptions[0]
    problems = lc.policy_problems(_inventory(), exceptions[1:])
    assert any(dropped["package"] in p and "FORBIDDEN" in p for p in problems), problems


# --- CI runs the check where the npm trees are installed --------------------------------------

def _ci_problems(job: dict, eco: str) -> list[str]:
    runs = [s.get("run", "") for s in job.get("steps", [])]
    ci = next((i for i, r in enumerate(runs) if r.strip() == "npm ci"), None)
    check = next((i for i, r in enumerate(runs)
                  if "licence_check.py --check" in r and f"--only {eco}" in r), None)
    if ci is None:
        return ["no npm ci step"]
    if check is None:
        return [f"no licence check for {eco}"]
    if check < ci:
        return ["the licence check runs before npm ci"]
    return []


def test_ci_runs_the_licence_check_after_npm_ci_in_both_app_jobs():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))["jobs"]
    assert _ci_problems(jobs["dashboard"], "npm-dashboard") == []
    assert _ci_problems(jobs["mobile"], "npm-mobile") == []
    # Negative control: the same job without the step, or with it before npm ci, fails.
    stripped = copy.deepcopy(jobs["dashboard"])
    stripped["steps"] = [s for s in stripped["steps"] if "licence_check" not in s.get("run", "")]
    assert _ci_problems(stripped, "npm-dashboard") == ["no licence check for npm-dashboard"]
    swapped = copy.deepcopy(jobs["mobile"])
    steps = swapped["steps"]
    i = next(i for i, s in enumerate(steps) if "licence_check" in s.get("run", ""))
    steps.insert(0, steps.pop(i))
    assert _ci_problems(swapped, "npm-mobile") == ["the licence check runs before npm ci"]


def _areas_for(changed: str) -> dict[str, str]:
    """Run the detect job's area rules (scripts/ci/changed_areas.sh) on one changed file."""
    text = (ROOT / "scripts/ci/changed_areas.sh").read_text(encoding="utf-8")
    rules = text[text.index("area() {"):]
    script = f"shared=false\nfiles={shlex.quote(changed)}\n{rules}"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.split())


@pytest.mark.parametrize("changed", ["scripts/licence_check.py", "docs/security/LICENCES.json",
                                     "docs/security/licence_exceptions.json",
                                     "scripts/supply_chain.py",
                                     "docs/security/native_libraries.json",
                                     "docs/security/sbom/api-image.inventory.json",
                                     "docs/legal/notices/watch-wearos.txt"])
def test_a_change_to_the_check_runs_the_jobs_that_run_it(changed):
    areas = _areas_for(changed)
    assert areas["mobile"] == areas["dashboard"] == areas["backend"] == areas["watch"] == "true", \
        areas


@pytest.mark.parametrize("doc", ["docs/security/REVIEW.md", "docs/legal/PRIVACY_POLICY.md"])
def test_negative_control_an_unrelated_document_runs_no_app_job(doc):
    assert _areas_for(doc) == {
        "backend": "false", "watch": "false", "mobile": "false", "dashboard": "false"}
