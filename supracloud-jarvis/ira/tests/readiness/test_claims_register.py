"""The claims register and the product's capability list say the same thing.

`care/capabilities.py` is what the API, dashboard and watch show. The register
(docs/compliance/CLAIMS_REGISTER.md) is what a reviewer reads. A capability added, removed or
changed from available to not available in one must show in the other."""
from __future__ import annotations

from pathlib import Path

from care.capabilities import AVAILABLE, CAPABILITIES

ROOT = Path(__file__).resolve().parents[4]
REGISTER = ROOT / "docs/compliance/CLAIMS_REGISTER.md"


def register_rows(text: str) -> dict[str, str]:
    """key -> evidence level, from the table under "What the watch measures or reports"."""
    section = text.split("## What the watch measures or reports", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] not in ("key",) and not set(cells[0]) <= set("-"):
            out[cells[0]] = cells[3]
    return out


def disagreements(text: str, capabilities=CAPABILITIES) -> list[str]:
    rows = register_rows(text)
    out = []
    for c in capabilities:
        level = rows.get(c["key"])
        if level is None:
            out.append(f"{c['key']}: missing from the claims register")
        elif (c["status"] == AVAILABLE) == level.startswith("not claimed"):
            out.append(f"{c['key']}: capabilities says {c['status']}, the register says '{level}'")
    keys = {c["key"] for c in capabilities}
    out += [f"{k}: in the register but not in capabilities" for k in rows if k not in keys]
    return out


def test_the_register_matches_the_capability_list():
    assert disagreements(REGISTER.read_text()) == []


def test_a_new_capability_without_a_row_is_caught():
    extra = CAPABILITIES + ({"key": "glucose", "label": "Glucose", "status": AVAILABLE, "how": "x"},)
    assert disagreements(REGISTER.read_text(), extra) == ["glucose: missing from the claims register"]


def test_a_capability_that_changes_status_is_caught():
    flipped = tuple(dict(c, status="not_available") if c["key"] == "heart_rate" else c for c in CAPABILITIES)
    found = disagreements(REGISTER.read_text(), flipped)
    assert len(found) == 1 and found[0].startswith("heart_rate:")


def test_every_file_and_test_the_register_cites_exists():
    from tests.readiness.test_hazard_log_traceability import _files, references, resolve
    files = _files()
    missing = []
    for path, name in references(REGISTER.read_text()):
        found = resolve(path, files)
        if not found:
            missing.append(path)
        elif name and not any(name in (ROOT / f).read_text(errors="ignore") for f in found):
            missing.append(f"{path}::{name}")
    assert missing == []
