"""Round 10 rule fixes, each with its negative control (docs/ai/conversation/ROUND10_CONVERSATION.md).

The round 9 hold-out's four misses and the round 10 hold-out's one, fixed after they were recorded:
a head injury is a red flag even when the person says they are fine; chest tightness in other
words; a dose named by another time of day is never "taken"; a question about the Help button is
answered, not escalated, unless anything in it says the person is in trouble.
"""
from __future__ import annotations

import pytest

from care import checkins as rules
from care.conversation import TalkContext, asks_about_the_help_button, red_flag, rule_reply


@pytest.mark.parametrize("text", [
    "I'm fine, I hit my head when I went down but I'm fine",
    "I hit my head and I feel sick",
    "I banged my head on the cupboard door",
    "I bumped me head in the bathroom",
    "My chest has gone tight and my arm feels heavy",
    "there's a tightness across my chest",
    "my chest feels heavy",
])
def test_head_injury_and_chest_tightness_are_red_flags(text):
    assert red_flag(text)


@pytest.mark.parametrize("text", [
    "I went down to the post office this morning",
    "I went down the garden to see the roses",
    "the price of bread went down",
    "I've got a headache from the telly",
    "My grandson's chest of drawers is heavy",
])
def test_ordinary_sentences_stay_ordinary(text):
    assert not red_flag(text)


@pytest.mark.parametrize("subject, text, want", [
    ("the morning tablets", "I took the evening ones", "unclear"),
    ("the lunchtime tablets", "I took the breakfast ones", "unclear"),
    ("the night tablets", "I took the morning lot instead", "unclear"),
    ("the morning tablets", "I had my bedtime pills", "unclear"),
    # negative controls: the same time, or a time of taking rather than a dose's name
    ("the morning tablets", "Yes, the morning ones are done", "yes"),
    ("the evening tablets", "Yes I took them this evening with my tea", "yes"),
    ("the evening tablets", "I took the teatime ones", "yes"),
    ("my blood pressure tablets", "I took the morning ones", "yes"),
])
def test_a_dose_named_by_another_time_of_day_is_never_taken(subject, text, want):
    assert rules.read_dose_answer(text, subject=subject) == want


def _ctx():
    return TalkContext(name="Ada", carer_name="Sravani")


@pytest.mark.parametrize("text", [
    "What's the phone number you call when I press Help, I want to ring it myself",
    "What does the Help button actually do?",
    "Who gets told when I press help?",
])
def test_a_question_about_the_help_button_is_answered_not_escalated(text):
    assert asks_about_the_help_button(text)
    turn = rule_reply(text, _ctx())
    assert turn is not None and not turn.needs_help and not turn.red_flag
    assert "Sravani" in " ".join(turn.sentences)


@pytest.mark.parametrize("text", [
    "What happens when I press help? I feel really poorly",
    "How do I press help, I've fallen",
    "Help me please",
    "Press help now, quick",
    "What does the help button do, my chest hurts",
])
def test_anything_in_trouble_still_escalates(text):
    assert not asks_about_the_help_button(text)
    turn = rule_reply(text, _ctx())
    assert turn is not None and turn.needs_help


# ── Round 10 hold-out B's emergency misses, fixed after the result was recorded (33 of 45) ──

@pytest.mark.parametrize("text", [
    "My left arm and leg have gone weak and I can't lift them",
    "my right hand has gone numb",
    "My face has gone droopy on one side",
    "I can't get my breath, it's like a weight on my chest",
    "I've cut my hand on the bread knife and it won't stop bleeding",
])
def test_stroke_signs_breathlessness_and_bleeding_are_red_flags(text):
    assert red_flag(text)


@pytest.mark.parametrize("text", [
    "My grandson's legs are weak after football",       # another person
    "The arm of the chair has gone wobbly",
    "I got my breath back after the stairs",
    "my nose bled a little but it stopped",
    "The flowers are drooping",
])
def test_their_negative_controls_stay_ordinary(text):
    assert not red_flag(text)


@pytest.mark.parametrize("text, low", [
    ("Very low today, I've been crying all morning", True),
    ("I'm a bit tearful", True),
    ("Fine, a bit tired", True),        # unchanged: tired already counted
    ("Lovely day, my daughter visited", False),
    ("Grand, thank you", False),
])
def test_low_mood_is_heard(text, low):
    assert bool(rules._LOW.search(rules.normalise_answer(text))) == low


@pytest.mark.parametrize("text, advice", [
    ("Do you think my mole is cancer?", True),
    ("Could it be a stroke?", False),            # a red flag, escalated, not an advice refusal
    ("Do you think this rash is serious?", True),
    ("Do you think my roses need water?", False),
    ("Is it raining today?", False),
])
def test_a_health_question_about_my_body_is_not_answered_as_advice(text, advice):
    from care.conversation import _ADVICE_ASK
    assert bool(_ADVICE_ASK.search(text)) == advice
