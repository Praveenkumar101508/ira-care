"""The evidence pack index generates from the live requirements and links only real files (L.c).

It is checked by generating it, not by comparing a committed copy: `requirements.json`
changes in most pull requests, and a committed index would be stale after every merge
(see `scripts/evidence_index.py`)."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))

import evidence_index as ei  # noqa: E402

LINK = re.compile(r"\]\(([^)]+)\)")


def _doc() -> dict:
    return json.loads((ROOT / "docs/progress/requirements.json").read_text(encoding="utf-8"))


def test_the_index_has_one_row_per_requirement():
    doc = _doc()
    text = ei.render(doc)
    ids = [line.split("|")[1].strip() for line in text.splitlines()
           if line.startswith("| ") and not line.startswith(("| Id ", "| Measure "))
           and line.count("|") == 5]
    assert sorted(ids) == sorted(r["id"] for r in doc["requirements"])


def test_every_file_link_in_the_index_exists():
    text = ei.render(_doc())
    base = ei.OUT.parent
    missing = [t for t in LINK.findall(text)
               if not t.startswith("http") and not (base / t).resolve().exists()]
    assert missing == []


def test_blocked_items_name_their_blocker():
    doc = _doc()
    text = ei.render(doc)
    for r in doc["requirements"]:
        if r["state"] == "blocked_external":
            assert f"blocked: {ei._cell(r['blocked_by'])}" in text, r["id"]


def test_the_measures_match_the_completion_script():
    doc = _doc()
    text = ei.render(doc)
    for key, label, *_ in ei.cs.MEASURES:
        m = ei.cs.summary(doc)["measures"][key]
        assert f"| {label} | {m['numerator']} / {m['denominator']} | {m['percent']} % |" in text


def test_an_invalid_requirements_file_is_refused_not_indexed():
    """Negative control: an accepted item whose only evidence is a document."""
    doc = copy.deepcopy(_doc())
    victim = next(r for r in doc["requirements"] if r["state"] == "implemented")
    victim["state"] = "accepted"
    victim["evidence"] = [{"kind": "docs_only", "ref": "README.md"}]
    with pytest.raises(ValueError, match="does not validate"):
        ei.render(doc)


def test_a_link_to_a_missing_file_would_be_caught():
    """Negative control for the link check itself: a missing path does not resolve."""
    assert not (ei.OUT.parent / ei._link("docs/no_such_file.md", ei.OUT.parent).split("](")[1][:-1]).resolve().exists()
