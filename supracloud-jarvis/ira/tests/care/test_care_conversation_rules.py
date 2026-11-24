"""The conversation service without a database: the fake model, the guard,
stricter-wins merging, red flags, the talk intents and the rolling memory."""
from __future__ import annotations

import asyncio

import pytest

from care import conversation as conv
from care import checkins as rules

CTX = conv.Context(name="Tess", routines=["lunch at 12:30"], medication_due=["the morning tablets"], carer_name="Sam")


def run(coro):
    return asyncio.run(coro)


def test_the_fake_model_composes_a_warm_one_question_checkin_and_the_template_is_the_fallback(monkeypatch):
    text, source = run(conv.compose_checkin(CTX, "medication", "Hi Tess. Have you taken your tablets?", model=conv.FakeModel(),
                                            subject="the morning tablets"))
    assert source == "model" and text.count("?") == 1 and "Tess" in text and "the morning tablets" in text
    # A composed medication question that does not name the medication is not used.
    vague = conv.FakeModel(reply="Hello Tess. Have you had your tablets?")
    assert run(conv.compose_checkin(CTX, "medication", "T?", model=vague, subject="Blue tablet"))[1] == "template"
    text, source = run(conv.compose_checkin(CTX, "mood", "Hi Tess. How are you?", model=conv.FakeModel(broken=True)))
    assert (text, source) == ("Hi Tess. How are you?", "template")
    # Too slow for the first sentence: the template takes over.
    slow = conv.FakeModel(slow_s=0.5)
    import os
    os.environ["IRA_CARE_MODEL_FIRST_SENTENCE_S"] = "0.1"
    try:
        text, source = run(conv.compose_checkin(CTX, "mood", "Hi Tess. How are you?", model=slow))
    finally:
        del os.environ["IRA_CARE_MODEL_FIRST_SENTENCE_S"]
    assert source == "template"
    # A reply that fails the guard also falls back.
    bad = conv.FakeModel(reply="You probably have an arrhythmia, Tess. Have you eaten?")
    assert run(conv.compose_checkin(CTX, "meal", "T?", model=bad))[1] == "template"
    monkeypatch.setenv("IRA_CARE_MODEL", "off")
    assert run(conv.compose_checkin(CTX, "meal", "T?"))[1] == "template"


def test_the_guard_refuses_diagnosis_advice_links_and_length():
    assert conv.guard("Hello Tess. Have you eaten?").ok
    assert conv.guard("That sounds like a cardiac problem.").reason == "diagnosis word"
    assert conv.guard("You should take two more tablets tonight.").reason == "advice"
    assert conv.guard("I recommend you see a doctor.").reason == "advice"
    assert conv.guard("Read more at https://example.invalid").reason == "link"
    assert conv.guard("x" * 500).reason == "too long"
    assert conv.guard("How are you? Did you eat? Any pain?", one_question=True).reason == "more than one question"
    assert conv.guard('  "Hello."  ').text == "Hello."
    assert conv.asks_for_advice("should I take another one?") and conv.asks_for_advice("is it normal to feel dizzy")
    assert not conv.asks_for_advice("I had a lovely walk")


def test_sentences_arrive_as_they_complete():
    s = conv.SentenceSplitter()
    assert s.feed("Hello Tess. ") == ["Hello Tess."]
    assert s.feed("How are ") == [] and s.feed("you today? I ") == ["How are you today?"]
    assert s.flush() == ["I"]


def test_the_stricter_reading_always_wins():
    kw = rules.summarise_answer("medication", "yes I took them")
    # The model says "no": stricter, so the merge says no.
    s = conv.StructuredAnswer(medication_taken="no", summary="Said the tablets were not taken.")
    m = conv.merge(kw, s, "yes I took them", "medication")
    assert m.reading.said == "no" and m.structured.medication_taken == "no"
    # The keyword rules saw a call for help; the model did not: help stays.
    kw = rules.summarise_answer("wellbeing", "I'm fine but please send help")
    s = conv.StructuredAnswer(needs_help=False, wellbeing="good", summary="Said they were fine.")
    m = conv.merge(kw, s, "I'm fine but please send help", "wellbeing")
    assert m.reading.asked_for_help is True and m.structured.needs_help is True
    # The model saw pain the keywords missed: pain is kept.
    kw = rules.summarise_answer("wellbeing", "my knee is playing up")
    s = conv.StructuredAnswer(pain_mentioned=True, summary="Mentioned a knee bothering them.")
    m = conv.merge(kw, s, "my knee is playing up", "wellbeing")
    assert "mentioned pain or discomfort" in m.reading.signals and m.structured.pain_mentioned
    # A model summary that quotes the person's words is not used.
    kw = rules.summarise_answer("wellbeing", "I went to the shops with my neighbour this morning")
    s = conv.StructuredAnswer(summary="Said: I went to the shops with my neighbour")
    m = conv.merge(kw, s, "I went to the shops with my neighbour this morning", "wellbeing")
    assert m.reading.summary == kw.summary
    assert conv.merge(kw, None, "x", "wellbeing").source == "rules"


def test_red_flag_phrases_go_straight_to_help():
    for phrase in ("I fell in the kitchen", "I can't breathe properly", "there's a pain in my chest", "I can't get up"):
        assert conv.red_flag(phrase)
        m = conv.merge(rules.summarise_answer("wellbeing", phrase), conv.StructuredAnswer(needs_help=False), phrase, "wellbeing")
        assert m.red_flag and m.reading.asked_for_help
    assert conv.red_flag("I fell asleep in front of the telly"), "caught on purpose: asking first costs one question"
    assert not conv.red_flag("the chest of drawers is lovely")


def test_read_answer_uses_the_model_structure_and_survives_bad_json():
    m = run(conv.read_answer("mood", "How are you?", "awful, my hip hurts", model=conv.FakeModel()))
    assert m.structured.wellbeing == "poor" and m.structured.pain_mentioned and m.source == "model+rules"
    assert "sounded low" in m.reading.signals
    broken = conv.FakeModel(json_reply={"wellbeing": "excellent"})   # not in the schema
    m = run(conv.read_answer("mood", "How are you?", "fine thanks", model=broken))
    assert m.source == "rules" and m.reading.summary == "Said they were doing well."
    m = run(conv.read_answer("mood", "How are you?", "fine", model=conv.FakeModel(broken=True)))
    assert m.source == "rules"


def test_talk_answers_from_the_record_before_any_model():
    ctx = conv.TalkContext(name="Tess", routines=["lunch at 12:30"], medication_due=[], carer_name="Sam",
                           contacts=[{"display_name": "Family Member", "relationship": "daughter", "phone": "+440000000001"},
                                     {"display_name": "Carer One", "relationship": "carer", "phone": ""}],
                           doses_today=["the morning tablets at 08:00: taken"])
    t = run(conv.talk("call my daughter", ctx, model=conv.FakeModel(broken=True)))
    assert t.action == {"kind": "call", "name": "Family Member", "phone": "+440000000001"} and t.source == "rules"
    t = run(conv.talk("ring Carer One", ctx, model=conv.FakeModel(broken=True)))
    assert t.action is None and "no phone number" in t.sentences[0]
    t = run(conv.talk("call Bob", ctx, model=conv.FakeModel(broken=True)))
    assert "don't have anyone called bob" in t.sentences[0]
    assert "lunch at 12:30" in run(conv.talk("what's on today?", ctx, model=conv.FakeModel(broken=True))).sentences[0]
    assert "taken" in run(conv.talk("did I take my tablets", ctx, model=conv.FakeModel(broken=True))).sentences[0]
    r = run(conv.talk("remind me to water the plants", ctx, model=conv.FakeModel(broken=True)))
    assert r.action["kind"] == "reminder" and r.action["text"] == "water the plants" and r.action["when"] == "in an hour"
    r = run(conv.talk("remind me to ring Sravani at three", ctx, model=conv.FakeModel(broken=True)))
    assert r.action["text"] == "ring Sravani" and r.action["when"] == "at 15:00" and "15:00" in r.sentences[0]
    a = run(conv.talk("should I take another tablet?", ctx, model=conv.FakeModel()))
    assert a.sentences == ["I can't advise on that, Tess. Shall I let Sam know?"]
    h = run(conv.talk("I've fallen and I can't get up", ctx, model=conv.FakeModel()))
    assert h.needs_help and h.red_flag and "getting help" in h.sentences[0]


def test_talk_streams_guarded_sentences_from_the_model_and_falls_back_when_it_misbehaves(monkeypatch):
    ctx = conv.TalkContext(name="Tess", carer_name="Sam")
    heard: list[str] = []
    t = run(conv.talk("I had a lovely walk this morning", ctx, model=conv.FakeModel(), on_sentence=heard.append))
    assert t.source == "fake" and heard == t.spoken and "Tess" in t.sentences[0]
    assert heard[0] == t.acknowledgement and heard[1:] == t.sentences  # round 4: the acknowledgement comes first
    bad = conv.FakeModel(reply="You should take two more pills. That is a stroke symptom.")
    t = run(conv.talk("I feel odd", ctx, model=bad))
    assert t.source == "template" and t.sentences == ["I'm here with you, Tess. Shall I let Sam know you'd like a word?"]
    monkeypatch.setenv("IRA_CARE_MODEL", "off")
    t = run(conv.talk("hello", ctx))
    assert t.source == "template"


def test_the_rolling_summary_keeps_the_newest_and_drops_the_oldest():
    s = conv.roll_summary("", ["Morning: said they slept well."])
    s = conv.roll_summary(s, ["Lunch: had soup.", "Lunch: had soup."])
    assert s.split("\n") == ["Morning: said they slept well.", "Lunch: had soup."]
    long = conv.roll_summary(s, [f"Note {i}: " + "x" * 100 for i in range(20)], max_chars=600)
    assert len(long) <= 600 and "Morning" not in long and long.endswith("x" * 100)


def test_configured_model_follows_the_setting(monkeypatch):
    monkeypatch.setenv("IRA_CARE_MODEL", "fake")
    assert isinstance(conv.configured_model(), conv.FakeModel)
    monkeypatch.setenv("IRA_CARE_MODEL", "off")
    assert conv.configured_model() is None
    monkeypatch.setenv("IRA_CARE_MODEL", "local")
    assert isinstance(conv.configured_model(), conv.LayerModel)
    conv.use_model(conv.FakeModel())
    try:
        assert isinstance(conv.configured_model(), conv.FakeModel)
    finally:
        conv.use_model(None)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        conv.StructuredAnswer(mood_words="not a list")


# ── Round 4: the instant acknowledgement and the model kept warm ─────────────

def test_the_acknowledgement_fits_the_kind_of_thing_said_and_never_asks():
    first = lambda lines: lines[0]  # noqa: E731
    assert conv.acknowledgement("What's the weather like today?", "Tess", pick=first) == "Let me think, Tess."
    assert conv.acknowledgement("I went to the shops this morning", "Tess", pick=first) == "I see, Tess."
    assert conv.acknowledgement("I'm worried about Sravani, she hasn't rung", "Tess", pick=first) == "I'm here, Tess."
    for text in ("how are you", "The garden is lovely.", "I feel lonely tonight"):
        for pick in (lambda l: l[0], lambda l: l[-1]):
            line = conv.acknowledgement(text, "Tess", pick=pick)
            assert "?" not in line and "Tess" in line and conv.guard(line).ok


def test_talk_speaks_the_acknowledgement_at_once_and_keeps_it_apart_from_the_sentences(monkeypatch):
    ctx = conv.TalkContext(name="Tess", carer_name="Sam", contacts=[])
    model = conv.FakeModel(reply="That sounds lovely, Tess. The roses must be out.")
    heard: list[str] = []
    turn = run(conv.talk("I had a walk in the garden", ctx, model=model, on_sentence=heard.append))
    assert turn.acknowledgement and heard[0] == turn.acknowledgement
    assert turn.sentences == ["That sounds lovely, Tess.", "The roses must be out."] and len(turn.sentences) == 2
    assert heard == turn.spoken == [turn.acknowledgement, *turn.sentences]
    # The rules' own replies are instant and get no acknowledgement.
    heard.clear()
    turn = run(conv.talk("What's on today?", ctx, model=model, on_sentence=heard.append))
    assert turn.acknowledgement is None and heard == turn.sentences
    # Switched off, nothing is spoken before the model.
    monkeypatch.setenv("IRA_CARE_INSTANT_ACK", "off")
    heard.clear()
    turn = run(conv.talk("I had a walk in the garden", ctx, model=model, on_sentence=heard.append))
    assert turn.acknowledgement is None and heard == turn.sentences
    # When the model then fails, the acknowledgement was still heard, then the fallback line.
    monkeypatch.delenv("IRA_CARE_INSTANT_ACK")
    heard.clear()
    turn = run(conv.talk("I had a walk in the garden", ctx, model=conv.FakeModel(broken=True), on_sentence=heard.append))
    assert turn.source == "template" and heard == [turn.acknowledgement, conv.FALLBACK_LINE.format(name="Tess", carer="Sam")]


def test_the_context_is_kept_short_for_the_prompt():
    ctx = conv.TalkContext(name="Tess", routines=[f"routine {i}" for i in range(8)], earlier_today=[f"said {i}" for i in range(8)],
                           summary="x" * 2000, contacts=[], language="en-GB")
    lines = conv._context_lines(ctx)
    assert lines.count("routine") == conv.CONTEXT_ITEMS and lines.count("said") == conv.CONTEXT_ITEMS
    assert len(lines) < 600 and "Memory: " in lines
    assert "British English" in conv.talk_messages(ctx, "hello")[0]["content"]
    assert conv.language_name("xx-YY") == "xx-YY"


def test_the_configured_model_is_warmed_only_when_it_can_be(monkeypatch):
    monkeypatch.setenv("IRA_CARE_MODEL", "fake")
    conv.use_model(None)
    assert run(conv.warm_configured_model()) is None       # the fake model has nothing to load
    monkeypatch.setenv("IRA_CARE_MODEL", "off")
    assert run(conv.warm_configured_model()) is None       # templates only

    class Warmable(conv.FakeModel):
        async def warm(self):
            return 0.25
    conv.use_model(Warmable())
    try:
        assert run(conv.warm_configured_model()) == 0.25
    finally:
        conv.use_model(None)
