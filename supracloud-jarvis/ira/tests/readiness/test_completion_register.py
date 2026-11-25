"""The completion register's percentages are computed, never typed (scripts/completion_summary.py).

Fails when a row's status cell uses no word from the register's own list, or when the weighted
block in the register no longer matches what the rows say (run the script with --write).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("completion_summary", ROOT / "scripts" / "completion_summary.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def _register() -> str:
    return cs.REGISTER.read_text()


def test_every_row_has_a_status_word_from_the_list():
    rows = cs.rows(_register())
    assert len(rows) >= 50
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "row ids are unique"


def test_the_weighted_block_matches_the_rows():
    text = _register()
    start, end = text.index(cs.BEGIN), text.index(cs.END) + len(cs.END)
    assert text[start:end] == cs.block(text), "run: python3 scripts/completion_summary.py --write"


def test_the_check_notices_a_typed_number_and_an_unknown_status():
    text = _register()
    tampered = text.replace("| Overall, weighted | ", "| Overall, weighted | 9", 1)
    start, end = tampered.index(cs.BEGIN), tampered.index(cs.END) + len(cs.END)
    assert tampered[start:end] != cs.block(tampered)
    bad = text.replace("| C-1 | Conservative medication answers | integration tested |",
                       "| C-1 | Conservative medication answers | nearly there |", 1)
    assert bad != text
    with pytest.raises(ValueError):
        cs.rows(bad)


def test_weights_and_scores_are_the_stated_ones():
    assert sum(cs.WEIGHTS.values()) == 100
    assert cs.SCORES["device tested"] == 1.0 and cs.SCORES["not started"] == 0.0
