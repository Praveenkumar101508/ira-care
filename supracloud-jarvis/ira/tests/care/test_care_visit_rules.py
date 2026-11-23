"""Scheduled visits: the decisions, without a database (care/visits.py, 109)."""
from __future__ import annotations

import datetime as dt
import uuid

from care import visits
from care.escalation import alert_text

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def _visit(status="planned", start=T0, end=T0 + dt.timedelta(hours=1), **kw):
    v = {"id": uuid.uuid4(), "status": status, "planned_start": start, "planned_end": end,
         "carer_name": "Sravani", "arrived_at": None}
    v.update(kw)
    return v


def test_a_visit_is_late_after_the_set_minutes_and_missed_at_the_end_of_its_window():
    v = _visit()
    assert visits.decide(v, T0 - dt.timedelta(minutes=1), 15) is None
    assert visits.decide(v, T0 + dt.timedelta(minutes=14), 15) is None
    assert visits.decide(v, T0 + dt.timedelta(minutes=15), 15) == "late"
    assert visits.decide(_visit("late"), T0 + dt.timedelta(minutes=30), 15) is None  # already late: once
    assert visits.decide(_visit("late"), T0 + dt.timedelta(hours=1), 15) == "missed"
    assert visits.decide(v, T0 + dt.timedelta(hours=1), 15) == "missed"  # straight to missed if no pass ran


def test_an_attended_or_finished_visit_is_never_late_or_missed():
    for status in ("arrived", "departed", "cancelled", "missed"):
        assert visits.decide(_visit(status), T0 + dt.timedelta(hours=5), 15) is None


def test_the_watch_hears_who_is_here_or_who_is_coming_and_nothing_else():
    assert visits.watch_state([], T0)["state"] == "none"
    coming = _visit(start=T0 + dt.timedelta(hours=2), end=T0 + dt.timedelta(hours=3))
    s = visits.watch_state([coming], T0)
    assert (s["state"], s["carer_name"]) == ("expected", "Sravani")
    far = _visit(start=T0 + dt.timedelta(days=2), end=T0 + dt.timedelta(days=2, hours=1))
    assert visits.watch_state([far], T0)["state"] == "none"
    here = _visit("arrived", arrived_at=T0 + dt.timedelta(minutes=3))
    s = visits.watch_state([here, coming], T0 + dt.timedelta(minutes=5))
    assert (s["state"], s["carer_name"], s["since"]) == ("here", "Sravani", (T0 + dt.timedelta(minutes=3)).isoformat())
    late = _visit("late")
    assert visits.watch_state([late], T0 + dt.timedelta(minutes=20))["state"] == "late"
    assert visits.watch_state([_visit(carer_name="")], T0)["carer_name"] == "Your carer"
    assert visits.carer_in_the_room([here]) and not visits.carer_in_the_room([coming, late])


def test_visit_alert_words_say_what_was_observed_and_never_dial_anyone():
    person = {"display_name": "Ada Example", "preferred_name": "Ada"}
    late = {"id": uuid.uuid4(), "kind": "visit_late", "severity": "notice",
            "details": {"summary": "Sravani's visit to Ada planned for 10:00 has not started 15 minutes after its start."}}
    title, body = alert_text(person, late, 1)
    assert "late" in title and "Sravani" in body and "999" not in body
    missed = {"id": uuid.uuid4(), "kind": "visit_missed", "severity": "urgent",
              "details": {"summary": "Sravani's visit to Ada planned for 10:00 did not happen."}}
    title, body = alert_text(person, missed, 1)
    assert "Ada" in title and "did not happen" in body and "I'm on my way" in body
    for text in (title, body):
        assert "neglig" not in text.lower() and "diagnos" not in text.lower()
