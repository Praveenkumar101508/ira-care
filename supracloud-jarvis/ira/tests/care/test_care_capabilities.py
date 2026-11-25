"""Product promises match the product (register D4).

One list says what IRA CARE measures and what it does not. These tests hold the backend's
accepted readings, the watch's reading kinds and the dashboard's reading cards to that list, so
a measurement marked "not available" can never be ingested, sent, or shown with a value.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from care import capabilities as caps
from care import schemas

ROOT = Path(__file__).resolve().parents[4]
NOT_AVAILABLE = {c["key"] for c in caps.CAPABILITIES if c["status"] == caps.NOT_AVAILABLE}


def test_each_entry_says_how_or_why_not():
    keys = [c["key"] for c in caps.CAPABILITIES]
    assert len(keys) == len(set(keys))
    for c in caps.CAPABILITIES:
        assert c["status"] in (caps.AVAILABLE, caps.NOT_AVAILABLE), c
        if c["status"] == caps.AVAILABLE:
            assert c.get("how") and not c.get("reason"), c
        else:
            assert c.get("reason") and not c.get("how") and not c.get("reading"), c
    # The promises the programme named are all answered, one way or the other.
    for k in ("food_intake", "camera", "spo2", "ecg", "hrv", "sleep", "blood_pressure", "hearing_access"):
        assert k in keys, k


def test_the_backend_accepts_exactly_what_the_watch_measures():
    assert tuple(schemas.VITAL_KINDS) == caps.readings()
    assert not NOT_AVAILABLE & set(schemas.VITAL_KINDS)


@pytest.mark.parametrize("kind", sorted(NOT_AVAILABLE))
def test_a_reading_ira_cannot_measure_is_refused(kind):
    with pytest.raises(ValidationError):
        schemas.Reading(kind=kind, value=97, recorded_at=dt.datetime.now(dt.timezone.utc))


def test_the_watch_sends_only_what_is_listed():
    src = (ROOT / "watch/app/src/main/java/care/ira/watch/sensors/SensorSource.kt").read_text()
    body = src.split("object ReadingKinds", 1)[1].split("}", 1)[0]
    assert set(re.findall(r'"(\w+)"', body)) == set(caps.readings())


def test_the_dashboard_draws_a_reading_card_only_for_what_is_measured():
    src = (ROOT / "dashboard/src/app/people/[id]/page.tsx").read_text()
    measured = re.search(r"const MEASURED = \[([^\]]*)\]", src)
    assert measured, "the dashboard's list of reading cards"
    assert set(re.findall(r"'(\w+)'", measured.group(1))) == set(caps.readings())
    health = src.split("tab === 'health'", 1)[1].split("tab === '", 1)[0]
    for k in NOT_AVAILABLE:
        assert f"'{k}'" not in health, f"{k} must not be drawn as a reading"


def test_the_api_answer_carries_every_entry():
    out = caps.public()["capabilities"]
    assert [c["key"] for c in out] == [c["key"] for c in caps.CAPABILITIES]
