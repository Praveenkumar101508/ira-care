"""P16-R3 — FINDING AO: the copyleft detector did not detect copyleft.

`check_licence_inventory.py` classified every locked package by regex. The
regex was `\\b(GPL|AGPL|LGPL|MPL|...)\\b`, and the trailing `\\b` requires a
non-word character after the abbreviation. So:

    "LGPL-3.0-only"                                  matched
    "GNU Lesser General Public License v3 (LGPLv3)"  did NOT

`psycopg` and `psycopg-binary` are LGPLv3 and went unreported for four batches,
because a `v` followed the word the gate was looking for. `pygithub` was caught
only by the parenthesised `(LGPL)` at the end of its string — luck, not the
check working.

These tests exist so the pattern cannot quietly stop matching again. They are
paired: every positive has a negative control, so a pattern that matched
EVERYTHING — which would also make the gate useless, just in the other
direction — fails here too.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[1]
if str(IRA_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(IRA_ROOT / "scripts"))

from check_licence_inventory import COPYLEFT, NOTABLE, _norm  # noqa: E402

#: Every spelling of a copyleft licence this project has actually seen in PyPI
#: metadata, plus the classifier forms it had not seen and did not match.
COPYLEFT_STRINGS = [
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "GPL-3.0-or-later OR Apache-2.0",
    "AGPL-3.0-or-later",
    "MPL-2.0",
    "MPL-2.0 AND MIT",
    # The forms FINDING AO missed.
    "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU General Public License v3 or later (GPLv3+)",
    "GNU Affero General Public License v3 (AGPLv3)",
    "GNU Library or Lesser General Public License (LGPL)",
    "GPLv2",
    "AGPLv3",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "Eclipse Public License 2.0",
]

#: Negative controls. If the pattern matches these it is not detecting
#: copyleft, it is detecting text — and a gate that flags every package flags
#: nothing.
PERMISSIVE_STRINGS = [
    "MIT",
    "MIT License",
    "BSD-3-Clause",
    "BSD 3-Clause License",
    "Apache-2.0",
    "Apache Software License",
    "Apache License, Version 2.0",
    "PSF-2.0",
    "ISC",
    "Python Software Foundation License",
    "Zope Public License",
    "The Unlicense (Unlicense)",
    "UNDECLARED",
]


@pytest.mark.parametrize("licence", COPYLEFT_STRINGS)
def test_copyleft_strings_are_detected(licence: str) -> None:
    assert COPYLEFT.search(licence), (
        f"{licence!r} is a copyleft licence the detector does not match. This "
        f"is FINDING AO exactly: the gate runs, prints a number, and reports "
        f"clean because its pattern did not fire.")


@pytest.mark.parametrize("licence", PERMISSIVE_STRINGS)
def test_permissive_strings_are_not_flagged(licence: str) -> None:
    assert not COPYLEFT.search(licence), (
        f"{licence!r} is not copyleft but the detector flags it. A pattern "
        f"that matches everything is as useless as one that matches nothing, "
        f"and it would bury the entries that matter.")


def test_the_original_pattern_would_fail_these_tests() -> None:
    """The regression this file exists for, stated as an executable fact.

    Without this, "the tests pass" says nothing about whether the fix was
    needed. The pattern that shipped is reconstructed here and asserted to miss
    the classifier forms — so the tests above are known to be capable of
    failing, and known to have failed before the fix.
    """
    original = re.compile(
        r"\b(GPL|AGPL|LGPL|MPL|EUPL|CDDL|EPL|CeCILL|OSL|SSPL)\b", re.I)
    missed = [s for s in COPYLEFT_STRINGS if not original.search(s)]
    assert "GNU Lesser General Public License v3 (LGPLv3)" in missed
    assert "GPLv2" in missed
    assert "AGPLv3" in missed
    assert len(missed) >= 4, (
        f"the pattern that shipped missed only {missed} — if it missed "
        f"nothing, this test is asserting a defect that did not exist and "
        f"should be deleted rather than weakened.")


def test_every_copyleft_package_in_the_inventory_is_declared() -> None:
    """The end-to-end property, over the real inventory rather than strings."""
    inventory = json.loads(
        (IRA_ROOT / "dependency-licences.json").read_text(encoding="utf-8"))
    packages = {_norm(k): v for k, v in inventory["packages"].items()}
    flagged = {n for n, v in packages.items() if COPYLEFT.search(v["licence"])}
    assert flagged, (
        "no package in the inventory matched the copyleft pattern at all. "
        "This tree definitely contains LGPL packages, so a zero here means "
        "the pattern or the inventory stopped working, not that the position "
        "improved.")
    undeclared = sorted(flagged - {_norm(k) for k in NOTABLE})
    assert not undeclared, (
        f"{undeclared} are copyleft and not declared in NOTABLE. Each needs an "
        f"entry saying what it is and how it arrived, in a diff somebody can "
        f"read.")


def test_psycopg_is_present_and_flagged() -> None:
    """The specific packages FINDING AO hid, named so the fix cannot regress.

    They are unavoidable — `langgraph-checkpoint-postgres` requires them — so
    the requirement is that they are VISIBLE, not that they are gone.
    """
    inventory = json.loads(
        (IRA_ROOT / "dependency-licences.json").read_text(encoding="utf-8"))
    packages = {_norm(k): v for k, v in inventory["packages"].items()}
    for name in ("psycopg", "psycopg-binary"):
        assert name in packages, f"{name} is not in the inventory at all"
        assert COPYLEFT.search(packages[name]["licence"]), (
            f"{name} is LGPLv3 and the detector does not flag it")
        assert _norm(name) in {_norm(k) for k in NOTABLE}


def test_pygithub_was_removed_not_accepted() -> None:
    """FINDING AO's other half: the LGPL dependency that WAS avoidable.

    `pygithub` carried four REST calls that `httpx` makes directly. The
    resolution was removal, and this asserts the removal held — in the lock,
    in the manifest, and in the code.
    """
    lock = (IRA_ROOT / "requirements.lock").read_text(encoding="utf-8")
    manifest = (IRA_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"(?im)^pygithub==", lock), (
        "pygithub is back in the lock")
    assert not re.search(r"(?im)^pygithub==", manifest), (
        "pygithub is back in requirements.txt")

    source = (IRA_ROOT / "utils" / "career_tools.py").read_text(encoding="utf-8")
    assert "from github import" not in source, (
        "utils/career_tools.py imports PyGithub again, so the package is a "
        "hidden runtime requirement that the lock no longer provides")
    assert "httpx" in source, (
        "the replacement HTTP client is gone from career_tools.py — the "
        "GitHub feature was removed rather than reimplemented, which is not "
        "what this finding resolved")
