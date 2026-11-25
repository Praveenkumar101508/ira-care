"""The activity data contract and the carer's trend, as pure functions (F7, D-084)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from care import activity, consent, schemas
from care import activity_estimate as est

ROOT = Path(__file__).resolve().parents[4]
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 25, 15, 30, tzinfo=UTC)
VERSION, SHA, CLASSES = activity.known_model()


def period(**over) -> dict:
    p = {"period_start": dt.datetime(2026, 11, 25, 9, tzinfo=UTC), "period_end": dt.datetime(2026, 11, 25, 10, tzinfo=UTC),
         "model_version": VERSION, "model_sha256": SHA, "source": "device",
         "seconds": {"walking": 600, "unknown": 60, "unavailable": 90},
         "reasons": {"low_confidence": 60, "off_wrist": 60, "sensor_gap": 30}, "windows": 75, "mean_confidence": 0.8}
    p.update(over)
    return p


def test_the_server_holds_the_same_model_file_as_the_watch():
    watch = (ROOT / "watch/app/src/main/assets/activity_model.json").read_bytes()
    assert hashlib.sha256(watch).hexdigest() == SHA
    assert VERSION == est.MODEL_FORMAT
    assert activity.labels() == tuple(CLASSES) + ("unknown", "unavailable")


def test_every_class_is_in_exactly_one_group_and_no_group_is_named_after_eating():
    members = [c for g in activity.GROUPS.values() for c in g]
    assert sorted(members) == sorted(CLASSES)
    words = " ".join(list(activity.GROUP_WORDS.values()) + [activity.LABEL]).lower()
    for w in ("eat", "ate", "meal", "drink", "food", "health status", "diagnos"):
        assert not re.search(rf"\b{w}", words), w
    assert activity.LABEL == "Estimated from the watch's motion sensor; not a measurement of health."


def test_a_good_period_passes():
    activity.check_period(period(), now=NOW)
    activity.check_period(period(seconds={}, reasons={}, windows=0, mean_confidence=None), now=NOW)


@pytest.mark.parametrize("over, words", [
    ({"model_sha256": "a" * 64}, "model"),
    ({"source": "phone"}, "source"),
    ({"seconds": {"ate": 10}}, "label"),
    ({"reasons": {"low_confidence": 60, "off_wrist": 60, "sensor_gap": 30, "bored": 10}}, "reason"),
    ({"seconds": {"walking": -10, "unknown": 60, "unavailable": 90}}, "whole numbers"),
    ({"seconds": {"walking": 1.5, "unknown": 60, "unavailable": 90}}, "whole numbers"),
    ({"reasons": {"off_wrist": 60, "sensor_gap": 30}}, "low_confidence"),
    ({"reasons": {"low_confidence": 60, "off_wrist": 60}}, "reasons"),
    ({"windows": 3}, "window count"),
    ({"period_end": dt.datetime(2026, 11, 25, 11, tzinfo=UTC)}, "one hour"),
    ({"period_end": dt.datetime(2026, 11, 25, 9, tzinfo=UTC)}, "one hour"),
    ({"period_start": dt.datetime(2026, 11, 25, 9), "period_end": dt.datetime(2026, 11, 25, 10)}, "timezone"),
    ({"period_start": dt.datetime(2026, 11, 25, 16, tzinfo=UTC), "period_end": dt.datetime(2026, 11, 25, 17, tzinfo=UTC)}, "future"),
    ({"mean_confidence": 1.5}, "mean_confidence"),
])
def test_a_period_that_breaks_the_contract_is_refused(over, words):
    with pytest.raises(activity.PeriodRefused, match=words):
        activity.check_period(period(**over), now=NOW)


def test_the_request_schema_bounds_the_batch_and_the_shapes():
    ok = {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in period().items()}
    schemas.ActivityReport(periods=[ok])
    with pytest.raises(Exception):
        schemas.ActivityReport(periods=[])
    with pytest.raises(Exception):
        schemas.ActivityReport(periods=[dict(ok, model_sha256="xyz")])
    with pytest.raises(Exception):
        schemas.ActivityReport(periods=[dict(ok, windows=361)])


def test_the_consent_kind_is_known_everywhere_it_is_checked():
    assert activity.CONSENT_KIND in schemas.CONSENT_KINDS
    assert activity.CONSENT_KIND in consent.CONSENT_KINDS
    sql = (ROOT / "supracloud-jarvis/postgres/121_care_activity.sql").read_text()
    assert f"'{activity.CONSENT_KIND}'" in sql
    rows = [{"kind": "activity_sharing", "granted": True, "recorded_at": NOW, "revoked_at": None}]
    assert activity.sharing_granted(rows)
    assert not activity.sharing_granted([])
    assert not activity.sharing_granted([dict(rows[0], revoked_at=NOW)])
    assert not activity.sharing_granted(rows + [dict(rows[0], granted=False, recorded_at=NOW + dt.timedelta(seconds=1))])
    # Another consent does not stand in for it.
    assert not activity.sharing_granted([dict(rows[0], kind="health_data_family")])


def _row(start: dt.datetime, seconds: dict, source: str = "device") -> dict:
    return {"period_start": start, "source": source, "seconds": seconds}


def test_minutes_per_day_in_groups_with_what_was_not_recorded_left_unfilled():
    tz = ZoneInfo("UTC")
    rows = [_row(dt.datetime(2026, 11, 24, 9, tzinfo=UTC), {"walking": 1200, "stairs": 600, "sitting": 900, "standing": 300,
                                                            "eating_drinking": 300, "other": 300}),
            _row(dt.datetime(2026, 11, 24, 10, tzinfo=UTC), {"unknown": 600, "unavailable": 1200}),
            _row(dt.datetime(2026, 11, 25, 9, tzinfo=UTC), {"sitting": 3600})]
    out = activity.summarise(rows, tz=tz, today=NOW.date(), now=NOW, days=3, weeks=1)
    assert [d["date"] for d in out["days"]] == ["2026-11-23", "2026-11-24", "2026-11-25"]
    y = out["days"][1]["minutes"]
    assert (y["active"], y["still"], y["other_movement"], y["unknown"], y["unavailable"]) == (30, 20, 10, 10, 20)
    assert y["not_recorded"] == 1440 - 90 and y["unknown_or_unavailable"] == 10 + 20 + 1350
    # Today is counted only up to now (15:30): 930 minutes, of which 60 were sent.
    t = out["days"][2]["minutes"]
    assert t["still"] == 60 and t["not_recorded"] == 930 - 60
    # A day the watch sent nothing for is all "not recorded", never zero minutes of anything else.
    assert out["days"][0]["minutes"]["not_recorded"] == 1440 and out["days"][0]["minutes"]["still"] == 0
    assert out["weeks"][0]["minutes"]["active"] == 30 and out["weeks"][0]["end"] == "2026-11-25"
    assert out["sources"] == ["device"]


def test_an_hour_counts_on_the_persons_own_local_day():
    tz = ZoneInfo("Australia/Sydney")   # UTC+11 in November
    rows = [_row(dt.datetime(2026, 11, 24, 14, tzinfo=UTC), {"walking": 600})]   # 01:00 on the 25th in Sydney
    out = activity.summarise(rows, tz=tz, today=dt.date(2026, 11, 25), now=NOW, days=2, weeks=1)
    assert {d["date"]: d["minutes"]["active"] for d in out["days"]} == {"2026-11-24": 0, "2026-11-25": 10}


def test_weeks_are_seven_day_blocks_ending_today_oldest_first():
    rows = [_row(dt.datetime(2026, 11, 25, 9, tzinfo=UTC) - dt.timedelta(days=d), {"walking": 600}) for d in range(0, 28, 7)]
    out = activity.summarise(rows, tz=ZoneInfo("UTC"), today=NOW.date(), now=NOW, days=7, weeks=4)
    assert [w["end"] for w in out["weeks"]] == ["2026-11-04", "2026-11-11", "2026-11-18", "2026-11-25"]
    assert [w["minutes"]["active"] for w in out["weeks"]] == [10, 10, 10, 10]


def test_the_device_is_told_only_yes_or_no():
    assert activity.device_settings(True) == {"sharing": True}
    assert activity.device_settings(False) == {"sharing": False}
