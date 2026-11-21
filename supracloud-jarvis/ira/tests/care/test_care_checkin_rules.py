"""The check-in rules (care/checkins.py), without a database or a model."""
from __future__ import annotations

import datetime as dt
import random
import uuid

import pytest

from care import checkins as rules

UTC = dt.timezone.utc
SETTINGS = {"checkin_min_minutes": 15, "checkin_max_minutes": 20,
            "quiet_start": dt.time(22, 0), "quiet_end": dt.time(7, 0)}


# ── Quiet hours ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t,inside", [
    (dt.time(22, 0), True), (dt.time(23, 59), True), (dt.time(0, 0), True),
    (dt.time(6, 59), True), (dt.time(7, 0), False), (dt.time(12, 0), False),
    (dt.time(21, 59), False),
])
def test_quiet_hours_across_midnight(t, inside):
    assert rules.in_quiet_hours(t, dt.time(22, 0), dt.time(7, 0)) is inside


def test_quiet_hours_within_one_day_and_the_empty_window():
    assert rules.in_quiet_hours(dt.time(14, 0), dt.time(13, 0), dt.time(15, 0))
    assert not rules.in_quiet_hours(dt.time(16, 0), dt.time(13, 0), dt.time(15, 0))
    assert not rules.in_quiet_hours(dt.time(13, 0), dt.time(13, 0), dt.time(13, 0))


# ── Scheduling ───────────────────────────────────────────────────────────────

def test_the_next_checkin_is_fifteen_to_twenty_minutes_on_during_the_day():
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    for seed in range(200):
        when = rules.next_checkin_at(now, SETTINGS, "Europe/London", random.Random(seed))
        minutes = (when - now).total_seconds() / 60
        assert 15 <= minutes <= 20, minutes
        assert when.tzinfo is not None


def test_a_checkin_that_would_land_in_quiet_hours_waits_until_morning():
    now = dt.datetime(2026, 11, 21, 21, 50, tzinfo=UTC)  # London is UTC in November
    for seed in range(50):
        when = rules.next_checkin_at(now, SETTINGS, "Europe/London", random.Random(seed))
        local = rules.local_now(when, "Europe/London")
        assert local.date() == dt.date(2026, 11, 22)
        assert dt.time(7, 1) <= local.time() <= dt.time(7, 15), local


def test_quiet_hours_follow_the_persons_timezone():
    # 21:50 UTC is 03:20 in Kolkata: inside quiet hours there, so the next
    # check-in is after 07:00 Kolkata time.
    now = dt.datetime(2026, 11, 21, 21, 50, tzinfo=UTC)
    when = rules.next_checkin_at(now, SETTINGS, "Asia/Kolkata", random.Random(1))
    local = rules.local_now(when, "Asia/Kolkata")
    assert dt.time(7, 1) <= local.time() <= dt.time(7, 15)


def test_an_unknown_timezone_falls_back_to_utc_instead_of_failing():
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    when = rules.next_checkin_at(now, SETTINGS, "Mars/Olympus", random.Random(0))
    assert 15 <= (when - now).total_seconds() / 60 <= 20


def test_the_window_uses_the_persons_own_settings():
    now = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    s = dict(SETTINGS, checkin_min_minutes=30, checkin_max_minutes=45)
    when = rules.next_checkin_at(now, s, "UTC", random.Random(3))
    assert 30 <= (when - now).total_seconds() / 60 <= 45


# ── Choosing the question ────────────────────────────────────────────────────

def _local(h, m=0):
    return dt.datetime(2026, 11, 21, h, m, tzinfo=UTC)  # a Saturday


def test_a_medication_due_now_comes_first():
    med = {"id": uuid.uuid4(), "name": "Blue tablet", "times": [dt.time(8, 0)], "active": True}
    choice = rules.choose_question(_local(8, 20), medications=[med])
    assert choice.kind == "medication" and choice.subject == "Blue tablet"
    assert choice.subject_id == str(med["id"])


def test_a_dose_already_recorded_today_is_not_asked_about_again():
    med = {"id": uuid.uuid4(), "name": "Blue tablet", "times": [dt.time(8, 0)], "active": True}
    event = {"medication_id": med["id"], "due_at": dt.datetime(2026, 11, 21, 8, 0, tzinfo=UTC)}
    choice = rules.choose_question(_local(8, 20), medications=[med], medication_events=[event])
    assert choice.kind != "medication"


def test_a_meal_routine_near_now_is_asked_about_once():
    routine = {"id": uuid.uuid4(), "name": "Lunch", "kind": "meal", "time_of_day": dt.time(12, 30),
               "days_of_week": [1, 2, 3, 4, 5, 6, 7], "active": True}
    choice = rules.choose_question(_local(12, 50), routines=[routine])
    assert choice.kind == "meal" and choice.subject == "Lunch"
    asked = {"question_kind": "meal", "scheduled_at": _local(12, 35), "status": "answered", "answer_summary": ""}
    again = rules.choose_question(_local(12, 50), routines=[routine], recent=[asked])
    assert again.kind != "meal" or again.reason != "meal_time"


def test_pain_mentioned_earlier_today_is_followed_up():
    recent = [{"question_kind": "mood", "scheduled_at": _local(9, 0), "status": "answered",
               "answer_summary": "Said: \"my knee hurts\" (mentioned pain or discomfort)"}]
    choice = rules.choose_question(_local(10, 0), recent=recent)
    assert choice.kind == "pain" and choice.reason == "follow_up"
    text = rules.question_text(choice, "Tess", rng=random.Random(0))
    assert "knee" in text


def test_the_rotation_avoids_the_three_most_recent_kinds():
    recent = [{"question_kind": k, "scheduled_at": _local(9, i), "status": "answered", "answer_summary": "fine"}
              for i, k in enumerate(("mood", "wellbeing", "plans"))]
    for seed in range(20):
        choice = rules.choose_question(_local(10, 0), recent=recent, rng=random.Random(seed))
        assert choice.kind in ("pain", "meal")


# ── Wording ──────────────────────────────────────────────────────────────────

def test_questions_are_short_warm_and_use_the_name():
    for kind in rules.QUESTION_KINDS:
        choice = rules.QuestionChoice(kind, "rotation", subject="blue tablet" if kind == "medication" else None)
        text = rules.question_text(choice, "Tess", time_of_day=dt.time(10, 0), rng=random.Random(0))
        assert "Tess" in text
        assert text.count("?") == 1, text
        assert len(text) <= 90, text
        assert not any(w in text.lower() for w in ("diagnos", "symptom", "clinical", "patient")), text


def test_a_missing_name_still_produces_a_question():
    text = rules.question_text(rules.QuestionChoice("mood", "rotation"), "", rng=random.Random(0))
    assert "?" in text and "None" not in text


# ── Understanding the answer ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,said", [
    ("Yes I took it with breakfast", "yes"), ("Not yet, later", "no"), ("I forgot", "no"),
    ("Hmm", "unclear"), ("yes no maybe", "unclear"), ("All done", "yes"),
])
def test_yes_and_no_are_read_for_medication(text, said):
    reading = rules.summarise_answer("medication", text, subject="the blue tablet")
    assert reading.said == said
    assert "the blue tablet" in reading.summary


def test_pain_and_help_are_signals_not_judgements():
    reading = rules.summarise_answer("mood", "my back hurts and I feel dizzy")
    assert "mentioned pain or discomfort" in reading.signals
    assert not reading.asked_for_help
    for bad in ("diagnos", "fall risk", "cardiac", "hypertension"):
        assert bad not in reading.summary.lower()
    help_reading = rules.summarise_answer("wellbeing", "please help I can't get up")
    assert help_reading.asked_for_help and "asked for help" in help_reading.signals


def test_a_good_answer_and_an_empty_answer():
    assert rules.summarise_answer("mood", "I'm fine thanks").summary == "Said they were doing well."
    assert rules.summarise_answer("mood", "").summary == "No answer."
    long = "x" * 300
    assert len(rules.summarise_answer("plans", long).summary) <= 400


def test_missed_in_a_row_counts_until_an_answer():
    rows = [{"status": "missed"}, {"status": "skipped_quiet"}, {"status": "missed"},
            {"status": "answered"}, {"status": "missed"}]
    assert rules.missed_in_a_row(rows) == 2
    assert rules.missed_in_a_row([{"status": "answered"}]) == 0
    assert rules.missed_in_a_row([]) == 0
