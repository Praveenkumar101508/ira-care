"""The documents written for the remaining requirements cite only things that exist.

Every backticked repository path in them must resolve, and every requirement id they name in
parentheses, e.g. (I.j) or (G1.e), must be in requirements.json. A document can promise or
describe; it may not point at a file, test or requirement that is not there."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
DOCS = [
    "docs/compliance/PILOT_PROTOCOL.md",
    "docs/compliance/INTENDED_PURPOSE.md",
    "docs/compliance/DPIA.md",
    "docs/compliance/MHRA_ADVISER_QUESTIONS.md",
    "docs/ops/HOSTING_PROPOSAL.md",
    "docs/hardware/SENSOR_EVALUATION_PLAN.md",
    "docs/hardware/ODM_SPECIFICATION.md",
    "docs/hardware/EVT_DVT_PVT_ROADMAP.md",
    "docs/hardware/QUALCOMM_ENGAGEMENT.md",
    "docs/evidence/ENDORSEMENT_PACK.md",
    "docs/progress/GO_NO_GO.md",
    "docs/evidence/device/GALAXY_WATCH4_TEST.md",
    "docs/evidence/device/TUNNEL_CHOICE.md",
]
EXT = (".md", ".json", ".py", ".kt", ".ts", ".tsx", ".sh", ".yml", ".sql", ".svg", ".glb")
REQ_ID = re.compile(r"\b((?:[A-L]\d?(?:–H4)?|C-\d+|D\db?|H1–H4)\.[a-z])\b")


def _ids() -> set[str]:
    return {i["id"] for i in json.loads((ROOT / "docs/progress/requirements.json").read_text())["requirements"]}


def paths_in(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"`([^`\s]+)`", text):
        tok = tok.split("::")[0].rstrip("/")
        if "/" in tok and (tok.endswith(EXT) or tok.endswith("__tests__") or (ROOT / tok).is_dir() or "." not in tok.rsplit("/", 1)[-1]):
            out.append(tok)
    return out


def missing_paths(text: str, base: Path) -> list[str]:
    bad = []
    for p in paths_in(text):
        if "..." in p:
            continue
        candidates = [ROOT / p, base / p, ROOT / "supracloud-jarvis/ira" / p, ROOT / "docs" / p, ROOT / "docs/hardware" / p]
        if not any(c.exists() for c in candidates):
            bad.append(p)
    return bad


@pytest.mark.parametrize("doc", DOCS)
def test_every_cited_path_exists(doc):
    path = ROOT / doc
    assert missing_paths(path.read_text(), path.parent) == []


@pytest.mark.parametrize("doc", DOCS)
def test_every_cited_requirement_exists(doc):
    ids = _ids()
    cited = set(REQ_ID.findall((ROOT / doc).read_text()))
    assert cited - ids == set(), sorted(cited - ids)


def test_a_missing_path_and_a_missing_requirement_are_caught():
    """Negative controls."""
    assert missing_paths("see `docs/no/such/file.md`", ROOT) == ["docs/no/such/file.md"]
    assert set(REQ_ID.findall("blocked (Z9.q) and (G1.e)")) == {"G1.e"}
    assert "Q.z" not in _ids()
