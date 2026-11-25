"""The small classifier adds signals and never removes them (care/classifier.py)."""
from __future__ import annotations

import json

import numpy as np

from care import classifier, conversation as conv
from care import checkins as rules

# Three one-hot "embeddings" and heads built so that the reading is known.
E = {"help": np.array([1.0, 0, 0], np.float32), "fine": np.array([0, 1.0, 0], np.float32), "low": np.array([0, 0, 1.0], np.float32)}
SPEC = {"encoder": "test", "heads": {
    "needs_help": {"classes": ["no", "yes"], "W": [[-2, 2], [2, -2], [2, -2]], "b": [0, 0]},
    "pain": {"classes": ["no", "yes"], "W": [[-2, 2], [2, -2], [1, -1]], "b": [0, 0]},
    "wellbeing": {"classes": ["unknown", "good", "ok", "poor"], "W": [[0, 0, 0, 3], [0, 3, 0, 0], [0, 0, 0, 3]], "b": [0, 0, 0, 0]},
    "said": {"classes": ["none", "yes", "no", "unclear"], "W": [[0, 0, 3, 0], [0, 3, 0, 0], [3, 0, 0, 0]], "b": [0, 0, 0, 0]},
    "advice": {"classes": ["no", "yes"], "W": [[2, -2], [2, -2], [2, -2]], "b": [0, 0]},
}}


def fake_encoder(texts):
    return np.stack([E["help"] if "floor" in t else E["low"] if "lonely" in t else E["fine"] for t in texts])


def clf():
    return classifier.Classifier(fake_encoder, classifier.Heads(SPEC))


def test_heads_read_the_known_vectors():
    c = clf()
    r = c.read("I'm on the floor")
    assert r.needs_help and r.pain and r.wellbeing == "poor" and r.said == "no" and not r.advice
    r = c.read("I'm fine")
    assert not r.needs_help and r.wellbeing == "good" and r.said == "yes"
    assert 0 < r.confidence["wellbeing"] <= 1


def test_the_classifier_can_add_a_help_signal_but_never_remove_one():
    keyword = rules.summarise_answer("wellbeing", "I'm fine")
    plain = conv.merge(keyword, None, "I'm fine", "wellbeing")
    assert not plain.reading.asked_for_help
    # The classifier says help (a phrasing the keywords missed): help it is.
    boosted = conv.merge(keyword, None, "I'm fine", "wellbeing", clf().read("I'm on the floor"))
    assert boosted.reading.asked_for_help and "asked for help" in boosted.reading.signals
    # The keywords say help and the classifier says fine: help stays.
    kw_help = rules.summarise_answer("wellbeing", "help me")
    kept = conv.merge(kw_help, None, "help me", "wellbeing", clf().read("I'm fine"))
    assert kept.reading.asked_for_help


def test_the_classifier_can_say_poor_or_no_but_never_turn_a_dose_answer_into_a_yes():
    kw = rules.summarise_answer("medication", "mm", subject="the tablets")
    assert kw.said == "unclear"
    r = conv.merge(kw, None, "mm", "medication", clf().read("I'm fine"))      # classifier reads "yes"
    # Remediation A2: an unclear dose answer stays unclear whatever the classifier says.
    assert r.reading.said == "unclear" and r.structured.medication_taken == "unknown"
    kw_no = rules.summarise_answer("medication", "not yet", subject="the tablets")
    r = conv.merge(kw_no, None, "not yet", "medication", clf().read("I'm fine"))
    assert r.reading.said == "no", "a clear no from the rules is never overturned"
    r = conv.merge(rules.summarise_answer("mood", "so so"), None, "so so", "mood", clf().read("I'm lonely"))
    assert r.structured.wellbeing == "poor" and "sounded low" in r.reading.signals


async def test_read_answer_uses_the_configured_classifier_and_survives_its_failure(monkeypatch):
    classifier.use_classifier(clf())
    try:
        r = await conv.read_answer("wellbeing", "How are you?", "I'm on the floor", model=conv.FakeModel())
        assert r.reading.asked_for_help

        class Broken:
            def read(self, text):
                raise RuntimeError("encoder gone")
        classifier.use_classifier(Broken())
        r = await conv.read_answer("wellbeing", "How are you?", "I'm fine", model=conv.FakeModel())
        assert not r.reading.asked_for_help and r.structured.wellbeing == "good"
    finally:
        classifier.use_classifier(None)
        classifier._tried = False


def test_off_by_default_and_the_heads_file_shape(monkeypatch):
    monkeypatch.delenv(classifier.ENABLED_ENV, raising=False)
    classifier._instance, classifier._tried = None, False
    assert classifier.configured() is None
    classifier._tried = False
    if classifier.HEADS_PATH.exists():
        spec = json.loads(classifier.HEADS_PATH.read_text())
        assert set(spec["heads"]) >= {"needs_help", "pain", "wellbeing", "said"}
        for h in spec["heads"].values():
            assert len(h["W"]) == 384 and len(h["W"][0]) == len(h["classes"]) == len(h["b"])
