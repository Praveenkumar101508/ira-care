"""The demonstration data follows docs/BUILD_RULES.md "Demonstration data" (round 3, phase 13).

Only the two people who build IRA CARE are named as real people; the people being
looked after are made up and keep one name each; e-mail addresses are at example.com;
telephone numbers are in the UK range reserved for fiction; nothing says "Demo Person".
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEMO_FILES = sorted(
    list((ROOT / "scripts" / "demo").glob("*.py")) + list((ROOT / "scripts" / "demo").glob("*.mjs"))
    + list((ROOT / "scripts" / "demo").glob("*.sh")) + list((ROOT / "scripts" / "screens").glob("*.mjs"))
    + list((ROOT / "docs" / "demo").glob("*.md")) + [ROOT / "docs" / "screens" / "INDEX.md"])

REAL_PEOPLE = {"Praveen Kamineti", "Sravani Perni"}
MADE_UP_PEOPLE = {"Ada Hartley", "Bert Lowe"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE = re.compile(r"(?<![\w.])(\+44\s?7\d{9}|07\d{3}\s?\d{6}|\+44\s?[0-9 ]{9,12})(?!\w)")
FICTION_PHONE = re.compile(r"^(\+447700900\d{3}|07700\s?900\d{3})$")
FULL_NAME = re.compile(r"\b([A-Z][a-z]+ (?:Kamineti|Perni|Hartley|Lowe))\b")


def test_the_demonstration_files_exist():
    assert any(f.name == "seed_demo.py" for f in DEMO_FILES)


def test_every_email_in_the_demonstration_is_at_example_com():
    bad = []
    for f in DEMO_FILES:
        for m in EMAIL.finditer(f.read_text(encoding="utf-8")):
            if m.group(1).lower() != "example.com":
                bad.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert bad == []


def test_every_telephone_number_is_in_the_range_reserved_for_fiction():
    bad = []
    for f in DEMO_FILES:
        for m in PHONE.finditer(f.read_text(encoding="utf-8")):
            number = m.group(1).replace(" ", "")
            if not FICTION_PHONE.match(number):
                bad.append(f"{f.relative_to(ROOT)}: {m.group(1)}")
    assert bad == []


def test_only_the_builders_and_the_made_up_people_are_named():
    names = set()
    for f in DEMO_FILES:
        names.update(FULL_NAME.findall(f.read_text(encoding="utf-8")))
    assert names <= REAL_PEOPLE | MADE_UP_PEOPLE, names - (REAL_PEOPLE | MADE_UP_PEOPLE)
    seed = (ROOT / "scripts" / "demo" / "seed_demo.py").read_text(encoding="utf-8")
    for name in REAL_PEOPLE | MADE_UP_PEOPLE:
        assert name in seed, f"{name} missing from the seed"


def test_nothing_says_demo_person_or_a_placeholder_town():
    bad = [str(f.relative_to(ROOT)) for f in DEMO_FILES
           if re.search(r"Demo Person|Testtown|example\.invalid|\+4400000", f.read_text(encoding="utf-8"))]
    assert bad == []


def test_the_builders_have_their_roles():
    seed = (ROOT / "scripts" / "demo" / "seed_demo.py").read_text(encoding="utf-8")
    assert re.search(r'"Praveen Kamineti", "owner", "workspace_admin"', seed), "Praveen Kamineti coordinates"
    assert re.search(r'"Sravani Perni", "member", "member"', seed), "Sravani Perni is family"
    assert '"self", "wearer"' in seed, "the wearer's own account is linked to the circle"
