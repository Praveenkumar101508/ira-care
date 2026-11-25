"""Every control and test the hazard log names is real.

The clinical safety case (docs/compliance/CLINICAL_SAFETY_CASE.md) ties each hazard to the
code that controls it and the test that proves the control. A reviewer checks those links
rather than trusting them, so a renamed file or a deleted test must fail here, not quietly
leave the log pointing at nothing. This proves the links exist; it does not prove the
controls are adequate, which is the Clinical Safety Officer's judgement."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
LOG = ROOT / "docs/compliance/CLINICAL_SAFETY_CASE.md"
CODE_EXT = (".py", ".kt", ".ts", ".tsx", ".sh", ".yml", ".yaml", ".sql")


def _files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return out.split()
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def rows(text: str) -> list[tuple[str, str, str]]:
    """(hazard id, code column, tests column) for each hazard row."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 9 and re.fullmatch(r"H\d+", cells[1]):
            out.append((cells[1], cells[6], cells[7]))
    return out


def references(column: str) -> list[tuple[str, str | None]]:
    """(path pattern, optional name) for every backticked file reference in a column."""
    out = []
    for token in re.findall(r"`([^`]+)`", column):
        path, _, name = token.partition("::")
        path = path.strip().split()[-1]          # "dashboard topbar.test.tsx" style
        if path.endswith(CODE_EXT):
            out.append((path, name.strip() or None))
    return out


def resolve(pattern: str, files: list[str]) -> list[str]:
    """A full path, a path with `/.../` for the middle, or a bare file name."""
    if "/.../" in pattern:
        head, tail = pattern.split("/.../", 1)
        return [f for f in files if (f.startswith(head + "/") or f"/{head}/" in f) and f.endswith("/" + tail)]
    if "/" in pattern:
        return [f for f in files if f == pattern or f.endswith("/" + pattern)]
    return [f for f in files if f.rsplit("/", 1)[-1] == pattern]


def problems(text: str, files: list[str]) -> list[str]:
    out = []
    for hid, code, tests in rows(text):
        for path, name in references(code) + references(tests):
            found = resolve(path, files)
            if not found:
                out.append(f"{hid}: no file matches `{path}`")
            elif name and not any(re.search(rf"\b{re.escape(name)}\b", (ROOT / f).read_text(errors="ignore")) for f in found):
                out.append(f"{hid}: `{name}` is not in {path}")
    return out


def test_the_hazard_log_has_its_rows():
    ids = [h for h, _, _ in rows(LOG.read_text())]
    assert ids[:1] == ["H1"] and len(ids) >= 20, ids
    assert len(ids) == len(set(ids)), "hazard ids are unique"


def test_every_named_control_and_test_exists():
    assert problems(LOG.read_text(), _files()) == []


@pytest.mark.parametrize("bad, expected", [
    ("| H99 | x | x | x | x | `care/no_such_module.py` | `test_care_delivery_db.py` | x |", "no file matches `care/no_such_module.py`"),
    ("| H99 | x | x | x | x | `care/escalation.py` | `test_care_delivery_db.py::test_that_was_deleted` | x |", "`test_that_was_deleted` is not in"),
    ("| H99 | x | x | x | x | `watch/.../NoSuchFile.kt` | — | x |", "no file matches `watch/.../NoSuchFile.kt`"),
])
def test_a_broken_link_is_caught(bad, expected):
    """Negative controls: a missing file, a missing test name and a missing `.../` path fail."""
    found = problems(bad, _files())
    assert len(found) == 1 and expected in found[0], found
