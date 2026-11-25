"""The production API image installs a hashed lock, and nothing that only the tests need.

deploy/Dockerfile.api used to install requirements-test.txt, so pytest, moto and moto's own
dependencies (Werkzeug, responses, xmltodict) shipped in production, and every package the file
did not pin floated to whatever PyPI served on the day of the build. It now installs
requirements-api.lock with ``--no-deps --require-hashes``, a strict subset of requirements.lock
derived by scripts/api_image_lock.py, then requirements-nodeps.txt the same way.

These checks need no network and no Docker. Each has a negative control: a broken copy of the
lock, the Dockerfile or the image snapshot must be caught.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[2]
REPO = IRA_ROOT.parents[1]
sys.path.insert(0, str(IRA_ROOT / "scripts"))
sys.path.insert(0, str(REPO / "scripts"))

import api_image_lock as al  # noqa: E402
import supply_chain as sc  # noqa: E402

DOCKERFILE = REPO / "deploy" / "Dockerfile.api"


def _texts():
    return (al.API_LOCK.read_text(encoding="utf-8"), al.LOCK.read_text(encoding="utf-8"),
            al.requirements(al.API_REQS), al.test_only())


def test_the_api_lock_is_a_strict_subset_of_the_main_lock_with_no_test_tool():
    assert al.check() == []
    api, full = al.blocks(_texts()[0]), al.blocks(_texts()[1])
    assert len(api) > 50 and set(api) < set(full)
    for name, b in api.items():
        assert (b["version"], b["hashes"]) == (full[name]["version"], full[name]["hashes"]), name


def test_the_test_only_set_is_read_from_the_manifests():
    dev = al.test_only()
    assert {"pytest", "pytest-asyncio", "moto"} <= dev
    assert not dev & set(al.requirements(al.REQS))  # nothing the product needs


def test_negative_control_a_test_tool_in_the_api_lock_is_caught():
    api_text, lock_text, reqs, dev = _texts()
    pytest_block = ("pytest==8.3.3 \\\n    --hash=sha256:" + "a" * 64 + "\n"
                    "    # via -r requirements-api.txt\n")
    found = al.problems(api_text + pytest_block, lock_text + pytest_block, reqs, dev)
    assert any("pytest is a test-only package" in p for p in found), found


@pytest.mark.parametrize("breakage, expected", [
    (lambda t: t.replace("fastapi==0.141.1 \\", "fastapi==0.141.0 \\"), "fastapi: the API lock pins"),
    (lambda t: re.sub(r"(fastapi==\S+ \\\n    --hash=sha256:)[0-9a-f]", r"\g<1>0", t, count=1),
     "fastapi: its hashes differ"),
    (lambda t: re.sub(r"(?ms)^fastapi==.*?(?=^[a-z])", "", t), "fastapi (requirements-api.txt) is not in"),
    (lambda t: t + "left-behind==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n    # via nothing-here\n",
     "left-behind is in the API lock but not in requirements.lock"),
])
def test_negative_control_a_drifted_or_edited_api_lock_is_caught(breakage, expected):
    api_text, lock_text, reqs, dev = _texts()
    found = al.problems(breakage(api_text), lock_text, reqs, dev)
    assert any(expected in p for p in found), found


def test_negative_control_a_package_nothing_needs_is_caught():
    api_text, lock_text, reqs, dev = _texts()
    orphan = next(n for n, b in al.blocks(lock_text).items() if n not in al.blocks(api_text))
    block = al.blocks(lock_text)[orphan]["text"]
    found = al.problems(api_text + block, lock_text, reqs, dev)
    assert any(f"{orphan} has no parent in the API lock" in p for p in found), found


def _installs(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(r"\bpip install\b", ln)]


def _dockerfile_problems(text: str) -> list[str]:
    out = []
    lines = _installs(text)
    if not any("--no-deps --require-hashes -r requirements-api.lock" in ln for ln in lines):
        out.append("the API lock is not installed with --no-deps --require-hashes")
    if not any("--no-deps --require-hashes -r requirements-nodeps.txt" in ln for ln in lines):
        out.append("requirements-nodeps.txt is not installed with --no-deps --require-hashes")
    for ln in lines:
        if "--require-hashes" not in ln:
            out.append(f"an install without hashes: {ln.strip()}")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    if any("requirements-test.txt" in ln for ln in code):
        out.append("the image copies or installs requirements-test.txt")
    if "pip check" not in text:
        out.append("the build does not run pip check")
    return out


def test_the_dockerfile_installs_only_the_hashed_locks():
    assert _dockerfile_problems(DOCKERFILE.read_text(encoding="utf-8")) == []


def test_negative_control_the_old_install_of_the_test_requirements_is_caught():
    old = DOCKERFILE.read_text(encoding="utf-8").replace(
        "RUN pip install --no-deps --require-hashes -r requirements-api.lock",
        "COPY supracloud-jarvis/ira/requirements-test.txt ./requirements.txt\n"
        "RUN pip install -r requirements.txt psutil==6.1.0")
    found = _dockerfile_problems(old)
    assert any("requirements-test.txt" in p for p in found)
    assert any("without hashes" in p for p in found)
    assert any("API lock is not installed" in p for p in found)


def test_the_image_snapshot_holds_exactly_the_locked_packages():
    """The committed scan of the image: its own Python packages are the two locks, no more."""
    inv = sc.load_json(sc.image_inventory_path("api"))
    locked = sc.locked_python("api")
    assert sc.python_lock_problems(inv, locked) == []
    names = {k.split("@")[0] for k in inv["python"]}
    assert not names & al.test_only(), names & al.test_only()
    assert "faster-whisper" in names and "boto3" in names  # nodeps; the KMS key provider
    # Negative controls: a test tool added, a locked package missing, a version floated.
    added = copy.deepcopy(inv)
    added["python"]["pytest@8.3.3"] = {**next(iter(inv["python"].values())), "name": "pytest",
                                       "origin": "added by the Dockerfile"}
    assert any("pytest@8.3.3 is in the image but not in its locks" in p
               for p in sc.python_lock_problems(added, locked))
    missing = copy.deepcopy(inv)
    gone = next(k for k in missing["python"] if k.startswith("fastapi@"))
    del missing["python"][gone]
    assert any(f"{gone} from" in p for p in sc.python_lock_problems(missing, locked))
    floated = copy.deepcopy(inv)
    floated["python"]["fastapi@9.9.9"] = floated["python"].pop(gone)
    found = sc.python_lock_problems(floated, locked)
    assert any("fastapi@9.9.9" in p for p in found) and any(gone in p for p in found)
