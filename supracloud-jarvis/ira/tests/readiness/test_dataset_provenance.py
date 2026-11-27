"""scripts/legal/dataset_provenance.py's pure parts: the figshare record read for provenance, and
the verdict that passes only on the expected licence with every file matching figshare's MD5."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(scope="module")
def p():
    spec = importlib.util.spec_from_file_location("provenance_under_test", ROOT / "scripts" / "legal" / "dataset_provenance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RECORD = {"title": "UMA_ADL_FALL_Dataset.zip", "doi": "10.6084/m9.figshare.4214283.v8", "version": 8,
          "published_date": "2025-12-01T00:00:00Z", "license": {"name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"},
          "files": [{"name": "UMAFall.zip", "size": 10, "computed_md5": "abc", "download_url": "https://example.invalid/f"}]}


def test_the_record_is_summarised(p):
    s = p.summarise(RECORD)
    assert s["licence"] == "CC BY 4.0" and s["doi"].endswith(".v8") and s["files"][0]["md5"] == "abc"


def test_the_verdict_needs_the_licence_and_every_checksum(p):
    s = p.summarise(RECORD)
    ok = [{"name": "UMAFall.zip", "md5_matches": True}]
    assert p.verdict(s, "CC BY 4.0", ok)["passed"]
    assert not p.verdict(s, "CC BY-NC 4.0", ok)["passed"], "another licence fails"
    assert not p.verdict(s, "CC BY 4.0", [{"name": "x", "md5_matches": False}])["passed"], "a checksum mismatch fails"
    assert not p.verdict(s, "CC BY 4.0", [])["passed"], "no files checked fails"
