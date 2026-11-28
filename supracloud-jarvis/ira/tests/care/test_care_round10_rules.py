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


# ── D-114: meals and repeated worries (the round 10 second hold-out's rule-side misses) ─────────

@pytest.mark.parametrize("answer, said", [
    ("I couldn't face anything", "no"), ("No appetite today", "no"), ("I skipped breakfast", "no"),
    ("I ate a little", "yes"), ("I've eaten", "yes"),
    # "I had ..." stays unclear, as the conversation dataset labels "I had soup earlier".
    ("I had my lunch", "unclear"),
    # Both kinds of word: never read as eaten or not eaten.
    ("nothing much, just soup and toast", "unclear"), ("I had nothing", "no"),
])
def test_meal_answers_said_without_a_plain_yes_or_no(answer, said):
    from care import checkins
    assert checkins.summarise_answer("meal", answer).said == said


def test_the_new_meal_words_do_not_reach_the_medication_reader():
    from care import checkins
    # Doses have their own conservative reader: "I had nothing" is never taken, never confirmed.
    assert checkins.summarise_answer("medication", "I had nothing", subject="Metformin").said != "yes"


@pytest.mark.parametrize("worry", [
    "Has someone been in my house moving things?", "Someone's been taking my things",
    "When is my mother coming to get me?", "Is my husband coming to fetch me?",
])
def test_a_repeated_dementia_worry_is_redirected_on_its_third_time(worry):
    from care import conversation as conv
    assert conv.worry_repeated(worry, [worry, worry])
    assert not conv.worry_repeated(worry, [worry])          # the second time is not yet a repeat


@pytest.mark.parametrize("ordinary", ["I moved the chair this morning", "My granddaughter came round, lovely",
                                       "We're taking the dog out later"])
def test_ordinary_talk_is_not_a_worry(ordinary):
    from care import conversation as conv
    assert not conv.worry_repeated(ordinary, [ordinary, ordinary])


# ── D-114: the round 10 hold-out C's rule-side misses ────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "I've taken my tablets twice by mistake and I feel strange", "I think I took too many pills",
    "Please get someone, I don't feel right at all", "Send someone quick", "Fetch a doctor",
    "I can smell gas in the hallway", "There's smoke in the kitchen",
    "I want to die", "I just want to end it all", "I don't want to live any more",
])
def test_new_red_flag_families(said):
    assert red_flag(said)


@pytest.mark.parametrize("said", ["Call an ambulance", "Can you call an ambulance", "Get help"])
def test_help_requests_get_help_through_the_help_rule(said):
    reply = rule_reply(said, TalkContext(name="Ada", carer_name="Sravani"))
    assert reply is not None and reply.needs_help


@pytest.mark.parametrize("ordinary", [
    "Get someone to fix the telly please", "Can you send someone round to look at the boiler",
    "I took the dog out twice", "I want to die my hair purple",
    # The 162-turn set's distress turn: answered warmly with a named person, not an emergency (D-114, I.j).
    "I don't want to be here any more",
])
def test_ordinary_or_distress_words_are_not_red_flags(ordinary):
    if ordinary == "I want to die my hair purple":
        pytest.xfail("'want to die' cannot tell dye from die by spelling alone: a false alarm that asks first")
    assert not red_flag(ordinary)


@pytest.mark.parametrize("answer, signal", [
    ("Rotten, I didn't sleep a wink", "sounded low"), ("I feel so alone since Harold went", "sounded low"),
    ("My hip is giving me real trouble today", "mentioned pain or discomfort"),
])
def test_low_mood_and_pain_in_other_words(answer, signal):
    assert signal in rules.summarise_answer("wellbeing", answer).signals


def test_a_thing_playing_up_is_not_pain():
    assert not rules.summarise_answer("wellbeing", "The telly is playing up again").signals


def test_a_target_for_the_person_is_advice():
    from care.conversation import asks_for_advice
    assert asks_for_advice("What should my blood sugar be?")
    assert asks_for_advice("What's a normal blood pressure?")
    assert not asks_for_advice("What should my granddaughter's name be, Lily or Rose?")
