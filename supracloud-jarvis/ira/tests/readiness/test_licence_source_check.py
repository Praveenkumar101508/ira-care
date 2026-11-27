"""The licence registers re-read at their sources (requirements F1.a, F1.b).

scripts/legal/verify_licence_sources.py reads where each row's licence is stated and compares it
with the register. These tests hold its parsers to fictional pages and answers (no network), make
sure a different licence or an unreachable source is never counted as agreeing, and check that the
committed record covers every listed source, names only rows that exist, and records no
disagreement.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "legal" / "verify_licence_sources.py"
REGISTERS = {"DATASETS.md": ROOT / "docs" / "DATASETS.md", "MODEL_WEIGHTS.md": ROOT / "docs" / "ai" / "MODEL_WEIGHTS.md"}


@pytest.fixture(scope="module")
def v():
    spec = importlib.util.spec_from_file_location("licence_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("text, ident", [
    ("cc-by-4.0", "cc-by-4.0"), ("Creative Commons Attribution 4.0 International", "cc-by-4.0"),
    ("CC BY 4.0", "cc-by-4.0"), ("cc-by-nc-4.0", "cc-by-nc-4.0"), ("CC BY-NC", "cc-by-nc"),
    ("Attribution-NonCommercial-ShareAlike 4.0", "cc-by-nc-sa-4.0"), ("apache-2.0", "apache-2.0"),
    ("Apache License, Version 2.0", "apache-2.0"), ("mit", "mit"), ("MIT License", "mit"),
    ("Open Data Commons Open Database License", "odbl-1.0"), ("Open Data Commons Attribution License", "odc-by-1.0"),
    ("openrail", "openrail"), ("CC0 1.0", "cc0-1.0"), ("Creative Commons Attribution", "cc-by"),
])
def test_licence_names_are_read_to_one_identifier(v, text, ident):
    assert v.normalise(text) == ident


@pytest.mark.parametrize("text", [None, "", "all rights reserved", "see the paper"])
def test_text_with_no_licence_name_is_none(v, text):
    assert v.normalise(text) is None


def test_zenodo_and_hugging_face_answers(v):
    assert v.from_zenodo(json.dumps({"metadata": {"license": {"id": "cc-by-4.0"}}})) == "cc-by-4.0"
    assert v.from_zenodo(json.dumps({"metadata": {}})) is None
    assert v.from_huggingface(json.dumps({"cardData": {"license": "mit"}})) == "mit"
    assert v.from_huggingface(json.dumps({"tags": ["license:apache-2.0"]})) == "apache-2.0"
    assert v.from_huggingface(json.dumps({"tags": []})) is None


def test_a_page_is_read_where_its_pattern_points_and_tags_are_stripped(v):
    page = "<html><dd>Licensed under a <a>Creative Commons Attribution 4.0 International</a> (CC BY 4.0)</dd></html>"
    found, words = v.from_text(page, r"Creative Commons Attribution 4\.0 International")
    assert found == "cc-by-4.0" and words == "Creative Commons Attribution 4.0 International"
    assert v.from_text(page, r"Open Data Commons") == (None, None)


def test_a_different_licence_or_an_unreachable_source_never_agrees(v):
    assert v.compare("cc-by-4.0", "cc-by-4.0", 200) == "agrees"
    assert v.compare("cc-by-4.0", "cc-by-nc-4.0", 200) == "differs"
    assert v.compare("cc-by-4.0", None, 200) == "not_stated"
    for status in (None, 401, 404, 503, 202):
        assert v.compare("cc-by-4.0", "cc-by-4.0", status) == "unreachable"


def test_check_uses_what_the_source_answers(v):
    entry = {"register": "DATASETS.md", "row": "X", "kind": "zenodo", "url": "https://example.invalid", "expected": "cc-by-4.0"}
    nc = json.dumps({"metadata": {"license": {"id": "cc-by-nc-4.0"}}})
    assert v.check(entry, fetcher=lambda _u: (200, nc))["result"] == "differs"
    assert v.check(entry, fetcher=lambda _u: (None, ""))["result"] == "unreachable"


def test_every_source_names_a_row_that_exists_in_its_register(v):
    texts = {k: p.read_text(encoding="utf-8") for k, p in REGISTERS.items()}
    for e in v.load_sources():
        assert e["register"] in texts, e
        assert any(line.startswith("| " + e["row"]) for line in texts[e["register"]].splitlines()), e["row"]
        assert e["kind"] in ("zenodo", "huggingface", "text") and (e["kind"] != "text" or e.get("pattern"))


def test_the_committed_record_covers_every_source_and_records_no_disagreement(v):
    record = json.loads(v.RECORD.read_text(encoding="utf-8"))
    listed = {(e["register"], e["row"], e["url"]) for e in v.load_sources()}
    recorded = {(r["register"], r["row"], r["url"]) for r in record["results"]}
    assert listed == recorded, "rerun scripts/legal/verify_licence_sources.py after changing the source list"
    assert record["counts"]["differs"] == 0
    assert all(r["result"] != "agrees" or r["found"] == r["expected"] for r in record["results"])
