"""Report summaries and wording (care/reports.py), without a database."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from care import reports
from care.monitoring import no_diagnosis_words

UTC = dt.timezone.utc
DAY = dt.date(2026, 11, 20)


def _r(kind, value, minute):
    return {"kind": kind, "value": value, "unit": "", "recorded_at": dt.datetime(2026, 11, 20, 10, minute, tzinfo=UTC)}


def _c(status, kind="mood", summary="", hour=10):
    return {"id": f"{status}-{hour}", "status": status, "question_kind": kind, "answer_summary": summary,
            "scheduled_at": dt.datetime(2026, 11, 20, hour, 0, tzinfo=UTC)}


def test_readings_are_summarised_against_the_carers_range_or_say_no_range():
    readings = [_r("heart_rate", 70, 1), _r("heart_rate", 150, 2), _r("steps", 40, 3), _r("on_wrist", 1, 4), _r("on_wrist", 0, 5)]
    none = reports.summarise_readings(readings, {"heart_rate_min": None, "heart_rate_max": None})
    assert none["range"] == "no range set" and none["outside_range"] is None
    assert none["heart_rate_min"] == 70 and none["heart_rate_max"] == 150 and none["steps_total"] == 40
    assert none["on_wrist_share"] == 50
    ranged = reports.summarise_readings(readings, {"heart_rate_min": 45, "heart_rate_max": 130})
    assert ranged["range"] == "45 to 130" and ranged["outside_range"] == 1


def test_checkins_count_answers_misses_meals_and_notable_points():
    checkins = [_c("answered", "meal", "Said they had eaten.", 8), _c("answered", "mood", 'Said: "my knee hurts" (mentioned pain or discomfort)', 10),
                _c("missed", hour=12), _c("skipped_quiet", hour=23), _c("scheduled", hour=14), _c("answered", "meal", "Said they had not eaten yet.", 13)]
    c = reports.summarise_checkins(checkins)
    assert (c["asked"], c["answered"], c["missed"], c["quiet_skipped"]) == (4, 3, 1, 1)
    assert (c["meals_asked"], c["meals_eaten"]) == (2, 1)
    assert len(c["notable"]) == 1 and "pain" in c["notable"][0]["summary"]


def test_the_daily_report_describes_and_never_diagnoses():
    person = {"id": "p", "display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"}
    data = reports.ReportData(person=person, period=reports.Period(DAY, DAY, ZoneInfo("UTC")))
    data.readings = [_r("heart_rate", 160, 1), _r("steps", 12, 2)]
    data.settings = {"heart_rate_min": 45, "heart_rate_max": 130}
    data.checkins = [_c("answered", "meal", "Said they had eaten.", 8), _c("missed", hour=12)]
    data.medications = [{"id": "m", "name": "Blue tablet"}]
    data.medication_events = [{"status": "taken"}, {"status": "missed"}]
    data.alerts = [{"id": "a1", "opened_at": dt.datetime(2026, 11, 20, 12, 5, tzinfo=UTC), "kind": "no_answer_no_movement",
                    "severity": "sos", "status": "acknowledged", "current_tier": 1}]
    data.acknowledgements = [{"alert_id": "a1", "display_name": "Family Member", "acknowledged_at": dt.datetime(2026, 11, 20, 12, 9, tzinfo=UTC)}]
    data.device = {"battery_percent": 55, "on_wrist": True, "background_sensor_permission": False, "last_sync_at": None}
    title, text, body = reports.render_daily(data)
    assert title.startswith("Daily report for Test Person")
    assert "1 answered, 1 missed, of 2 asked" in text
    assert "1 dose(s) confirmed taken, 1 said not taken" in text
    assert "range set for this person: 45 to 130; 1 reading(s) outside it" in text
    assert "no answer and no movement (sos, reached tier 1, now acknowledged acknowledged by Family Member)" in text
    assert "sensor permission OFF" in text
    assert no_diagnosis_words(text)
    assert body["alerts"][0]["acknowledged_by"] == "Family Member"


def test_the_weekly_report_is_summaries_only_and_says_so():
    person = {"id": "p", "display_name": "Test Person", "preferred_name": "Tess", "timezone": "UTC"}
    start = dt.date(2026, 11, 16)
    week = reports.ReportData(person=person, period=reports.Period(start, start + dt.timedelta(days=6), ZoneInfo("UTC")))
    days = []
    for i in range(7):
        d = reports.ReportData(person=person, period=reports.Period(start + dt.timedelta(days=i), start + dt.timedelta(days=i), ZoneInfo("UTC")))
        d.checkins = [_c("answered", summary="Said they were doing well.", hour=9), _c("missed" if i == 3 else "answered", hour=15)]
        days.append(d)
    title, text, body = reports.render_weekly(week, days)
    assert title == "Tess's week: 16 November to 22 November 2026"
    assert "Thursday: 1 of 2 check-ins answered" in text
    assert "answered 13 check-ins and missed 1" in text
    assert "summaries only" in text and "Said they were doing well" not in text
    assert no_diagnosis_words(text)
    assert len(body["days"]) == 7


def test_an_unknown_timezone_falls_back_to_utc():
    assert str(reports._tz("Nowhere/Nowhere")) == "UTC"
