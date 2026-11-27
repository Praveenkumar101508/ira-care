"""The WER harness's age buckets (scripts/eval/speech_wer.py --by-age, requirement F8.c's plan):
ages read from years or Common Voice labels, an unreadable age never guessed, and the per-bucket
WER pooled from the same alignment as the overall figure. Fictional rows; no audio, no dataset."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "eval" / "speech_wer.py"


@pytest.fixture(scope="module")
def wer():
    spec = importlib.util.spec_from_file_location("speech_wer_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("age, bucket", [
    (59, "under 60"), (60, "60-69"), (69.9, "60-69"), (70, "70-79"), (79, "70-79"), (80, "80+"), (97, "80+"),
    ("72", "70-79"), ("sixties", "60-69"), ("Seventies", "70-79"), ("eighties", "80+"), ("nineties", "80+"),
    ("fifties", "under 60"), ("twenties", "under 60"),
])
def test_ages_and_common_voice_labels_fall_in_their_bucket(wer, age, bucket):
    assert wer.age_bucket(age) == bucket


@pytest.mark.parametrize("age", [None, "", "old", "elderly", -1, 130, float("nan"), True, [], {}])
def test_an_age_that_cannot_be_read_is_unknown_never_guessed(wer, age):
    assert wer.age_bucket(age) == "unknown"


ROWS = [
    {"id": "a", "reference": "did you take your tablets", "hypothesis": "did you take your tablets", "group": "g", "speaker": "s1", "age": 34},
    {"id": "b", "reference": "i feel a bit dizzy", "hypothesis": "i feel a bit busy", "group": "g", "speaker": "s2", "age": "seventies"},
    {"id": "c", "reference": "help me please", "hypothesis": "help please", "group": "g", "speaker": "s3", "age": 83},
    {"id": "d", "reference": "yes thank you", "hypothesis": "yes thank you", "group": "g", "speaker": "s4"},
]


def test_per_bucket_wer_pools_words_and_older_is_compared_with_younger(wer):
    out = wer.results([dict(r) for r in ROWS], {}, by_age=True)
    b = out["age_buckets"]
    assert list(b) == ["under 60", "70-79", "80+", "unknown"], "in the fixed order, empty buckets left out"
    assert b["under 60"]["wer"] == 0.0 and b["70-79"]["wer"] == 0.2 and b["80+"]["wer"] == pytest.approx(0.3333, abs=1e-4)
    older = out["older_vs_younger"]["60 and over"]
    assert (older["words"], older["substitutions"], older["deletions"]) == (8, 1, 1) and older["utterances"] == 2
    assert out["older_vs_younger"]["unknown"]["utterances"] == 1
    assert [u["age_bucket"] for u in out["per_utterance"]] == ["under 60", "70-79", "80+", "unknown"]
    # the buckets partition the corpus: their words and errors add up to the overall figure
    assert sum(x["words"] for x in b.values()) == out["overall"]["words"]
    assert sum(x["substitutions"] + x["deletions"] + x["insertions"] for x in b.values()) == \
        out["overall"]["substitutions"] + out["overall"]["deletions"] + out["overall"]["insertions"]


def test_without_the_flag_the_results_are_unchanged(wer):
    out = wer.results([dict(r) for r in ROWS], {})
    assert "age_buckets" not in out and "older_vs_younger" not in out
    assert all("age_bucket" not in u for u in out["per_utterance"])


def test_score_mode_with_by_age_on_the_command_line(tmp_path):
    manifest = tmp_path / "m.jsonl"
    manifest.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    out = tmp_path / "r.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "score", str(manifest), "--out", str(out), "--by-age"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["older_vs_younger"]["under 60"]["wer"] == 0.0
