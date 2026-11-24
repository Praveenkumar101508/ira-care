"""IRA, personal to the person (110, phase 7): the decisions without a database."""
from __future__ import annotations

import datetime as dt

from care import conversation as conv
from care import personal

NOW = dt.datetime(2026, 11, 23, 10, 0)


def test_a_reminder_is_read_with_its_time_on_the_persons_clock():
    assert personal.parse_reminder("to ring Sravani at three", NOW)[:2] == ("ring Sravani", NOW.replace(hour=15))
    assert personal.parse_reminder("the bins in 20 minutes", NOW)[1] == NOW + dt.timedelta(minutes=20)
    assert personal.parse_reminder("about the doctor tomorrow morning", NOW)[1] == dt.datetime(2026, 11, 24, 9, 0)
    what, due, how = personal.parse_reminder("to water the plants", NOW)
    assert (what, due, how) == ("water the plants", NOW + dt.timedelta(hours=1), "default (in an hour)")
    assert personal.parse_reminder("my tablets at 8am", NOW)[1] == dt.datetime(2026, 11, 24, 8, 0), "8am has passed: tomorrow"
    assert personal.parse_reminder("call the chemist at 4:30 pm", NOW)[1] == NOW.replace(hour=16, minute=30)


def test_the_persons_words_about_a_reminder_are_read_plainly():
    assert personal.read_reminder_answer("yes I have") == "yes"
    assert personal.read_reminder_answer("I took them just now") == "yes"
    assert personal.read_reminder_answer("later please") == "later"
    assert personal.read_reminder_answer("not yet") == "later"
    assert personal.read_reminder_answer("not today thanks") == "skip"
    assert personal.read_reminder_answer("no") == "skip"
    assert personal.read_reminder_answer("hmm") == "unclear"
    assert personal.read_reminder_answer("") == "unclear"


def test_questions_and_confirmations_are_short_warm_and_never_clinical():
    med = {"kind": "medication", "status": "asked", "text": "Metformin"}
    q = personal.question_for(med, "Ada", "Metformin")
    assert q.startswith("Ada, it's time for Metformin") and "Say yes, later, or not today" in q
    assert personal.question_for({**med, "status": "confirming"}, "Ada", "Metformin") == "Ada, you've taken Metformin. Is that right?"
    assert "drink of water" in personal.question_for({"kind": "hydration", "status": "asked", "text": ""}, "Ada")
    assert "you asked me to remind you: ring Sravani" in personal.question_for({"kind": "reminder", "status": "asked", "text": "ring Sravani"}, "Ada")
    for nxt in ("confirm", "done", "snoozed", "skipped", "nothing"):
        line = personal.confirmation_line({"next": nxt}, "Ada")
        assert line and not any(w in line.lower() for w in ("dose", "overdose", "diagnos", "comply"))


def test_the_watch_profile_follows_pace_and_prompting():
    light = personal.watch_profile({"pace": "slow", "prompting_level": "light", "voice_on": False})
    assert (light["speech_rate"], light["repeats"], light["repeat_after_s"], light["voice"]) == (0.8, 0, 90, False)
    full = personal.watch_profile({"pace": "normal", "prompting_level": "full"})
    assert (full["speech_rate"], full["repeats"], full["repeat_after_s"], full["vibration"]) == (0.9, 2, 45, True)


def test_a_repeated_worry_is_met_gently_with_a_familiar_topic_and_a_named_person():
    earlier = ["I'm worried Sravani hasn't come, where is she", "Where is Sravani, I am worried she hasn't come"]
    assert conv.worry_repeated("I'm so worried, where is Sravani, she hasn't come", earlier)
    assert not conv.worry_repeated("I'm so worried, where is Sravani", []), "the first time is answered, not redirected"
    assert not conv.worry_repeated("the garden looks lovely today", earlier), "no worry words: no redirect"
    assert not conv.worry_repeated("I'm worried about the bins", earlier), "a different worry is a new one"
    ctx = conv.TalkContext(name="Ada", carer_name="Praveen", time_of_day="morning", familiar_topics=("the garden",), redirect_name="Sravani")
    t = conv.redirect_reply(ctx)
    assert t.action == {"kind": "offer_contact", "name": "Sravani"} and "the garden" in t.sentences[1]
    assert "I hear you, Ada" in t.sentences[0]
    plain = conv.redirect_reply(conv.TalkContext(name="Ada", carer_name="Praveen", time_of_day="morning"))
    assert plain.action["name"] == "Praveen" and "tell me about" not in plain.sentences[1], "no topics without consent"
    for s in t.sentences + plain.sentences:
        assert conv.guard(s, max_chars=240).ok and "calm down" not in s.lower()


def test_avoided_topics_are_dropped_from_a_model_reply():
    assert conv.mentions_avoided_topic("Have you heard from your late husband's brother?", ("late husband",))
    assert not conv.mentions_avoided_topic("The garden must be lovely.", ("late husband",))
    assert not conv.mentions_avoided_topic("Anything", ())


def test_concern_details_label_the_summary_and_link_the_source():
    d = personal.concern_details("Said something that needs a check.", person_name="Ada", transcript_id="abc", heard_in="talk")
    assert d["summary_by"] == "IRA" and d["said_by"] == "Ada" and d["source"] == {"kind": "transcript", "id": "abc"}
    assert "abc" not in d["summary"], "the words themselves stay in the transcript"


def test_the_editor_role_is_the_person_the_representative_or_a_carer():
    class P:
        user_id = "u1"
    members = [{"user_id": "u1", "role": "family", "relationship": "self"}]
    assert personal.role_for(P(), members) == "person"
    assert personal.role_for(P(), [{"user_id": "u1", "role": "family", "relationship": "daughter"}]) == "representative"
    assert personal.role_for(P(), [{"user_id": "u1", "role": "carer", "relationship": ""}]) == "carer"
    assert personal.role_for(P(), []) == "admin"
