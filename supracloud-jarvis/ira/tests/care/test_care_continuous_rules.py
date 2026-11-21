"""Arrival-time rules (care/continuous.py) and the operations check (care/ops.py)."""
from __future__ import annotations

import datetime as dt

from care import continuous, ops

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 21, 2, 0, tzinfo=UTC)
S = {"heart_rate_min": 45, "heart_rate_max": 130, "heart_rate_confirm_readings": 2, "heart_rate_confirm_seconds": 120}


def _hr(v, seconds):
    return {"kind": "heart_rate", "value": v, "recorded_at": T0 + dt.timedelta(seconds=seconds)}


def test_one_spike_is_ignored():
    assert continuous.confirmed_outside_range([_hr(70, 0), _hr(160, 60)], S) is None


def test_two_in_a_row_over_two_minutes_is_a_breach():
    b = continuous.confirmed_outside_range([_hr(70, 0), _hr(160, 60), _hr(165, 180)], S)
    assert b is not None and b.values == (160.0, 165.0)
    assert "160, 165" in b.summary and "45 to 130" in b.summary and "120 seconds" in b.summary


def test_two_in_a_row_but_too_close_together_is_not_yet_a_breach():
    assert continuous.confirmed_outside_range([_hr(160, 0), _hr(165, 60)], S) is None


def test_a_reading_back_in_range_resets_the_run():
    assert continuous.confirmed_outside_range([_hr(160, 0), _hr(165, 130), _hr(80, 200)], S) is None


def test_low_values_count_too_and_an_unset_range_is_no_rule():
    assert continuous.confirmed_outside_range([_hr(35, 0), _hr(38, 150)], S) is not None
    assert continuous.confirmed_outside_range([_hr(35, 0), _hr(38, 150)], dict(S, heart_rate_min=None, heart_rate_max=None)) is None
    only_low = dict(S, heart_rate_max=None)
    assert continuous.confirmed_outside_range([_hr(200, 0), _hr(200, 150)], only_low) is None
    assert continuous.confirmed_outside_range([_hr(30, 0), _hr(30, 150)], only_low) is not None


def test_other_kinds_and_order_do_not_matter():
    readings = [_hr(165, 180), {"kind": "steps", "value": 0, "recorded_at": T0}, _hr(160, 60), _hr(70, 0)]
    assert continuous.confirmed_outside_range(readings, S) is not None


def test_movement_since_unknown_is_none():
    assert continuous.movement_since([], T0) is None
    assert continuous.movement_since([{"kind": "steps", "value": 3, "recorded_at": T0 + dt.timedelta(minutes=1)}], T0) == 3


def test_ops_assess_flags_a_stale_or_missing_job():
    now = T0
    fresh = {"care_checkins": now - dt.timedelta(seconds=30), "care_monitoring": now - dt.timedelta(seconds=90)}
    assert ops.assess(fresh, now)["monitoring_paused"] is False
    stale = dict(fresh, care_monitoring=now - dt.timedelta(minutes=6))
    r = ops.assess(stale, now)
    assert r["monitoring_paused"] is True and r["jobs"]["care_monitoring"]["stale"] is True
    assert r["jobs"]["care_checkins"]["stale"] is False
    assert ops.assess({"care_checkins": None, "care_monitoring": now}, now)["monitoring_paused"] is True
