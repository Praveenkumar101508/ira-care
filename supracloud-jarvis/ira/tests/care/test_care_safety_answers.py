"""Remediation group A, the pure rules: IRA never records something as true
when it does not know it is true.

* one conservative reader for every answer that can become a dose;
* neither the model nor the classifier can upgrade an answer to "taken", and
  every field of the merged reading agrees;
* the safety question has three outcomes, and "no request for help" is not one
  that means the person is safe;
* the notification log never carries the message.
"""
from __future__ import annotations

import logging

import pytest

from care import checkins as rules
from care import conversation as conv
from care import personal, providers

# ── A1: the dose reader ──────────────────────────────────────────────────────

MANDATORY = [
    ("I have not taken it", "later"), ("No, I haven't taken it", "later"), ("I will take it", "later"),
    ("Yes, but not yet", "later"), ("Okay", "unclear"), ("I took it yesterday", "unclear"),
    ("I took a different tablet", "unclear"), ("I think I took it", "unclear"), ("I cannot remember", "unclear"),
    ("I might have taken it", "unclear"), ("I forgot to take it", "later"), ("I haven't taken it", "later"),
    ("I havent taken it", "later"), ("I'll take it now", "later"), ("Not today", "skip"), ("No", "skip"),
    ("Yes I have", "yes"), ("I took it just now", "yes"),
]

MORE = [
    # Unicode apostrophes, case, punctuation
    ("I HAVEN’T TAKEN IT!!", "later"), ("i didn’t", "later"), ("I’ll do it in a minute", "later"),
    # Bare acknowledgements are never a dose
    ("ok", "unclear"), ("right", "unclear"), ("alright", "unclear"), ("all right then", "unclear"), ("sure", "unclear"),
    ("mm", "unclear"), ("", "unclear"), ("   ", "unclear"),
    # Doubt
    ("probably", "unclear"), ("maybe I did", "unclear"), ("I'm not sure", "unclear"), ("I don't remember", "unclear"),
    ("I believe so", "unclear"), ("I should have taken it", "unclear"),
    # Another time, another medicine
    ("I took them last night", "unclear"), ("I took my other tablet", "unclear"), ("I took two", "unclear"),
    ("I took it twice", "unclear"),
    # Contradictions and mixed statements never become yes
    ("yes no", "unclear"), ("yes I took it, no wait", "unclear"), ("I took the morning one but not the evening one", "unclear"),
    ("no I took it", "unclear"),
    # Speech-recognition shapes
    ("yeah I've took them", "yes"), ("yes yes", "yes"), ("yep all done", "yes"), ("yes I did take it", "yes"),
    ("yes I take it", "yes"), ("I tuk it", "unclear"), ("yes please", "yes"),
    # Intentions and delays
    ("going to take it after breakfast", "later"), ("gonna take it soon", "later"), ("ask me again later", "later"),
    ("not now", "later"), ("I still need to take it", "later"),
    # Skips
    ("skip it", "skip"), ("I don't want it today", "skip"), ("no thanks", "skip"), ("nope", "skip"),
    ("I won't be taking it", "skip"),
    # Clear confirmations
    ("Yes I took it with breakfast", "yes"), ("I've had them", "yes"), ("already taken", "yes"),
    ("yes that's right", "yes"), ("correct", "yes"),
]


@pytest.mark.parametrize("text,expected", MANDATORY + MORE)
def test_the_dose_reader_is_conservative(text, expected):
    assert rules.read_dose_answer(text) == expected


def test_naming_a_different_medicine_is_unclear():
    meds = ("Metformin", "Aspirin")
    assert rules.read_dose_answer("yes I took my aspirin", subject="Metformin", other_medications=meds) == "unclear"
    assert rules.read_dose_answer("yes I took my metformin", subject="Metformin", other_medications=meds) == "yes"
    assert rules.read_dose_answer("yes I took it", subject="Metformin", other_medications=meds) == "yes"


@pytest.mark.parametrize("text,expected", MANDATORY)
def test_a_medication_reminder_uses_the_same_reader(text, expected):
    assert personal.read_reminder_answer(text, "medication") == expected
    assert personal.read_reminder_answer(text) == expected, "the default is the safe reading"


def test_other_reminders_keep_their_plainer_reading():
    assert personal.read_reminder_answer("okay", "reminder") == "yes"
    assert personal.read_reminder_answer("I will", "hydration") == "yes"
    assert personal.read_reminder_answer("later", "reminder") == "later"


def test_summaries_agree_with_the_dose_reading():
    for text, dose in MANDATORY:
        r = rules.summarise_answer("medication", text, subject="the blue tablet")
        assert r.dose == dose
        assert r.said == rules.dose_said(dose)
        assert ("had taken" in r.summary) == (dose == "yes"), (text, r.summary)


def test_the_meal_reading_is_unchanged():
    assert rules.summarise_answer("meal", "yes I had porridge").said == "yes"
    assert rules.summarise_answer("meal", "not yet").said == "no"
    assert rules.summarise_answer("meal", "ok").said == "unclear"


# ── A2: nothing upgrades a dose to "taken" ───────────────────────────────────

class _Clf:
    """A classifier that is certain of whatever it is told."""

    def __init__(self, said="yes", needs_help=False):
        self.said, self.needs_help, self.pain, self.wellbeing = said, needs_help, False, "good"
        self.confidence = {"said": 1.0, "needs_help": 1.0, "pain": 1.0, "wellbeing": 1.0}


LIAR = conv.StructuredAnswer(wellbeing="good", medication_taken="yes", summary="Said they took them.")


@pytest.mark.parametrize("text,dose", [t for t in MANDATORY if t[1] != "yes"])
def test_a_lying_model_and_classifier_cannot_confirm_a_dose(text, dose):
    kw = rules.summarise_answer("medication", text, subject="the blue tablet")
    m = conv.merge(kw, LIAR, text, "medication", _Clf("yes"), subject="the blue tablet")
    assert m.reading.said != "yes" and m.structured.medication_taken != "yes" and m.reading.dose != "yes"
    assert "had taken" not in m.reading.summary and "had taken" not in m.structured.summary


def test_a_doubting_model_makes_a_yes_more_cautious_and_every_field_agrees():
    kw = rules.summarise_answer("medication", "yes I have", subject="the blue tablet")
    doubt = conv.StructuredAnswer(medication_taken="no", summary="Said not taken.")
    m = conv.merge(kw, doubt, "yes I have", "medication", subject="the blue tablet")
    assert m.reading.said == "no" and m.reading.dose == "unclear" and m.structured.medication_taken == "no"
    assert "had taken" not in m.reading.summary
    m = conv.merge(kw, None, "yes I have", "medication", _Clf("no"), subject="the blue tablet")
    assert m.reading.said == "no" and m.structured.medication_taken == "no"


def test_an_unclear_answer_is_reported_as_unknown_not_the_models_yes():
    kw = rules.summarise_answer("medication", "hmm", subject="the blue tablet")
    m = conv.merge(kw, LIAR, "hmm", "medication", subject="the blue tablet")
    assert m.structured.medication_taken == "unknown" and m.reading.said == "unclear"


def test_only_a_medication_question_can_say_anything_about_a_dose():
    kw = rules.summarise_answer("mood", "fine, I took my pills")
    m = conv.merge(kw, LIAR, "fine, I took my pills", "mood")
    assert m.structured.medication_taken == "unknown"


# ── A4: the safety question ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("I'm not OK", "help"), ("not fine", "help"), ("I don't feel right", "help"), ("I'm dizzy", "help"),
    ("Yes, but I cannot get up", "help"), ("help", "help"), ("no", "help"), ("I'm on the floor", "help"),
    ("Maybe", "unclear"), ("I don't know", "unclear"), ("I can't remember", "unclear"), ("", "unclear"),
    ("what's for lunch", "unclear"), ("I think so", "unclear"), ("OK but my hip", "unclear"),
    ("I'm OK", "ok"), ("yes I'm fine thanks", "ok"), ("I'm all right", "ok"), ("yes", "ok"),
    ("I don't need help", "ok"), ("okay", "ok"),
])
def test_the_safety_question_has_three_outcomes(text, expected):
    assert rules.read_safety_answer(text) == expected


def test_a_safety_answer_is_ok_only_without_any_sign_of_trouble():
    from care.checkin_cycle import _safety_outcome
    fine = conv.merge(rules.summarise_answer("safety", "I'm OK"), None, "I'm OK", "safety")
    assert _safety_outcome("I'm OK", "voice", fine) == "ok"
    assert _safety_outcome("", "button", fine) == "ok"
    assert _safety_outcome("", "voice", fine) == "unclear", "empty speech recognition is not the OK button"
    assert _safety_outcome("", "phone", fine) == "unclear"
    # The model or the classifier hears trouble the words hide: never OK.
    worried = conv.StructuredAnswer(needs_help=True)
    m = conv.merge(rules.summarise_answer("safety", "I'm OK"), worried, "I'm OK", "safety")
    assert _safety_outcome("I'm OK", "voice", m) == "help"
    m = conv.merge(rules.summarise_answer("safety", "I'm OK"), None, "I'm OK", "safety", _Clf("unknown", needs_help=True))
    assert _safety_outcome("I'm OK", "voice", m) == "help"
    low = conv.merge(rules.summarise_answer("safety", "I'm OK"), conv.StructuredAnswer(wellbeing="poor"), "I'm OK", "safety")
    assert _safety_outcome("I'm OK", "voice", low) == "unclear"


# ── A5: the notification log never carries the message ──────────────────────

async def test_the_log_only_provider_never_logs_the_message(caplog):
    p = providers.LogOnlyProvider()
    secret = "Tess Example at 1 Example Road may have fallen; heart rate 140"
    with caplog.at_level(logging.DEBUG, logger="ira.care.providers"):
        await p.send_sms("+447700900123", secret)
        await p.call("+447700900123", secret)
        await p.raise_alarm(person_label="Tess Example", summary=secret, reference="ref-1")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "the outcome is still logged"
    for leak in ("Tess", "Example", "fallen", "heart", "140", "900123"[:4]):
        assert leak not in logged, leak
    assert "0123" in logged, "the destination is masked to its last four digits"
