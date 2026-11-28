"""The talk red-flag head (D-114): a second layer behind the keyword rules that can only add help."""
from __future__ import annotations

import asyncio
import json

import pytest

from care import classifier, conversation as conv


class _Head:
    def __init__(self, flags=(), boom=False):
        self.flags, self.boom = set(flags), boom

    def talk_red_flag(self, text):
        if self.boom:
            raise RuntimeError("encoder gone")
        return text in self.flags


@pytest.fixture
def ctx():
    return conv.TalkContext(name="Ada", carer_name="Sravani")


@pytest.fixture(autouse=True)
def _reset():
    yield
    classifier.use_classifier(None)


def _talk(text, ctx):
    return asyncio.run(conv.talk(text, ctx, model=conv.FakeModel()))


def test_an_emergency_in_new_words_gets_help_through_the_head(ctx):
    said = "I'm wheezing and I can't get enough air"
    classifier.use_classifier(_Head({said}))
    reply = _talk(said, ctx)
    assert reply.needs_help and reply.source == "classifier"
    assert "getting help" in " ".join(reply.sentences)


def test_without_the_head_the_rules_and_model_carry_on_unchanged(ctx):
    classifier.use_classifier(None)
    reply = _talk("I've been watching the birds on the feeder", ctx)
    assert not reply.needs_help and reply.source != "classifier"


def test_a_failing_head_never_breaks_the_conversation(ctx):
    classifier.use_classifier(_Head(boom=True))
    reply = _talk("I've been watching the birds on the feeder", ctx)
    assert not reply.needs_help


def test_the_head_never_takes_away_a_rule_red_flag(ctx):
    classifier.use_classifier(_Head())          # reads nothing as an emergency
    reply = _talk("I've fallen and I can't get up", ctx)
    assert reply.needs_help and reply.red_flag and reply.source == "rules"


def test_the_committed_head_is_a_two_layer_network_with_its_provenance():
    spec = json.loads(classifier.TALK_HEAD_PATH.read_text())
    assert spec["kind"] == "mlp64" and len(spec["W1"]) == 384 and len(spec["W1"][0]) == 64 and len(spec["w2"]) == 64
    assert 0.05 <= spec["threshold"] <= 0.95
    assert spec["corpus"]["source"].startswith("original synthetic") and len(spec["corpus"]["sha256"]) == 64
    assert spec["validation"]["recall"] >= 0.98
