"""Every control the threat model names is real (docs/security/THREAT_MODEL.md, C-22.c).

The same check as the hazard log's (test_hazard_log_traceability.py): each threat row names the
code that controls it and the test that proves it, and a renamed file or a deleted test must fail
here rather than leave the model pointing at nothing. A row may say "not controlled"; it may not
name a control that does not exist."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.readiness.test_hazard_log_traceability import _files, references, resolve

ROOT = Path(__file__).resolve().parents[4]
MODEL = ROOT / "docs/security/THREAT_MODEL.md"
STRIDE = set("STRIDE")


def rows(text: str) -> list[list[str]]:
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and re.fullmatch(r"T\d+", cells[0]):
            out.append(cells)
    return out


def problems(text: str, files: list[str]) -> list[str]:
    out = []
    for tid, stride, _threat, _control, code, tests, status in rows(text):
        if not set(stride) <= STRIDE or not stride:
            out.append(f"{tid}: STRIDE letters {stride!r}")
        if "not controlled" not in status and not (references(code) or references(tests)):
            out.append(f"{tid}: claims a control but names no file or test")
        for path, name in references(code) + references(tests):
            found = resolve(path, files)
            if not found:
                out.append(f"{tid}: no file matches `{path}`")
            elif name and not any(re.search(rf"\b{re.escape(name)}\b", (ROOT / f).read_text(errors="ignore")) for f in found):
                out.append(f"{tid}: `{name}` is not in {path}")
    return out


def test_the_threat_model_covers_every_component():
    text = MODEL.read_text()
    ids = [r[0] for r in rows(text)]
    assert len(ids) >= 30 and len(ids) == len(set(ids)), ids
    for component in ("Watch", "Phone", "Dashboard", "Care API", "Worker and providers", "Monitoring-centre webhook",
                      "PostgreSQL", "OTA update server", "Backups"):
        assert f"## {component}" in text, component


def test_every_named_control_and_test_exists():
    assert problems(MODEL.read_text(), _files()) == []


@pytest.mark.parametrize("bad, expected", [
    ("| T99 | S | x | x | `care/no_such_module.py` | `test_care_delivery_db.py` | controlled |", "no file matches `care/no_such_module.py`"),
    ("| T99 | T | x | x | `care/delivery.py` | `test_care_delivery_db.py::test_that_was_deleted` | controlled |", "`test_that_was_deleted` is not in"),
    ("| T99 | I | x | x | — | — | controlled |", "claims a control but names no file or test"),
    ("| T99 | Q | x | x | — | — | **not controlled** |", "STRIDE letters"),
])
def test_a_broken_row_is_caught(bad, expected):
    """Negative controls: a missing file, a deleted test, a control with no evidence, a bad STRIDE letter."""
    found = problems(bad, _files())
    assert len(found) == 1 and expected in found[0], found
