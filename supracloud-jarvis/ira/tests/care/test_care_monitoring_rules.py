"""The safety rules, the provider interface and the alert wording (phase 4)."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from care import escalation, monitoring, providers

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
SETTINGS = {
    "no_movement_window_s": 180, "movement_floor_steps": 1, "heart_rate_min": None, "heart_rate_max": None,
    "missed_checkins_before_escalation": 3, "off_wrist_alert_minutes": 30, "battery_alert_percent": 15,
    "sync_alert_minutes": 30, "quiet_start": dt.time(22, 0), "quiet_end": dt.time(7, 0),
}
CHECKIN = {"id": "c1", "status": "missed"}


def _r(kind, value, minutes_ago):
    return {"kind": kind, "value": value, "recorded_at": NOW - dt.timedelta(minutes=minutes_ago)}


def test_no_answer_and_no_movement_is_an_sos():
    obs = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 0, 1), _r("steps", 0, 2)], None, [], NOW)
    assert [o.kind for o in obs] == ["no_answer_no_movement"]
    assert obs[0].severity == "sos" and obs[0].checkin_id == "c1"
    assert monitoring.no_diagnosis_words(obs[0].summary)


def test_movement_above_the_floor_is_not_an_sos_and_unknown_is_not_zero():
    moving = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 12, 1)], None, [], NOW)
    assert not moving
    unknown = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [], None, [], NOW)
    assert not unknown, "no reading at all is unknown, never treated as no movement"
    old = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 0, 10)], None, [], NOW)
    assert not old, "a reading outside the window says nothing about the window"


def test_heart_rate_is_judged_only_against_the_carers_range():
    readings = [_r("steps", 20, 1), _r("heart_rate", 150, 1)]
    none = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, readings, None, [], NOW)
    assert not none, "no range set means no heart-rate rule"
    ranged = dict(SETTINGS, heart_rate_min=45, heart_rate_max=130)
    obs = monitoring.evaluate_missed_checkin(CHECKIN, ranged, readings, None, [], NOW)
    assert [o.kind for o in obs] == ["heart_rate_outside_range"] and "150" in obs[0].summary and "130" in obs[0].summary
    low = monitoring.evaluate_missed_checkin(CHECKIN, ranged, [_r("steps", 20, 1), _r("heart_rate", 40, 1)], None, [], NOW)
    assert low and low[0].kind == "heart_rate_outside_range"
    fine = monitoring.evaluate_missed_checkin(CHECKIN, ranged, [_r("steps", 20, 1), _r("heart_rate", 72, 1)], None, [], NOW)
    assert not fine


def test_off_the_wrist_during_the_check_is_an_sos():
    obs = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 20, 1), _r("on_wrist", 0, 1)], None, [], NOW)
    assert [o.kind for o in obs] == ["off_wrist_during_check"]
    worn = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 20, 1), _r("on_wrist", 1, 1)],
                                              {"on_wrist": False}, [], NOW)
    assert not worn, "a fresh reading outranks the device's last state"


def test_three_misses_in_a_row_with_normal_readings_is_urgent():
    recent = [{"status": "missed"}] * 3
    obs = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 20, 1)], None, recent, NOW)
    assert [o.kind for o in obs] == ["missed_checkins"] and obs[0].severity == "urgent"
    two = monitoring.evaluate_missed_checkin(CHECKIN, SETTINGS, [_r("steps", 20, 1)], None, recent[:2], NOW)
    assert not two


def test_device_notices_in_the_day_and_silence_at_night():
    device = {"status": "paired", "on_wrist": False, "battery_percent": 10, "background_sensor_permission": False,
              "last_sync_at": NOW - dt.timedelta(minutes=45)}
    obs = monitoring.evaluate_device(device, SETTINGS, "UTC", NOW, off_wrist_since=NOW - dt.timedelta(minutes=40))
    assert sorted(o.kind for o in obs) == ["low_battery", "no_sync", "off_wrist", "sensor_permission_off"]
    assert all(o.severity == "notice" for o in obs)
    night = dt.datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
    at_night = monitoring.evaluate_device(dict(device, last_sync_at=night, battery_percent=50,
                                               background_sensor_permission=True), SETTINGS, "UTC", night,
                                          off_wrist_since=night - dt.timedelta(hours=2))
    assert not at_night, "off the wrist at night is normal (the watch is charging)"
    unpaired = monitoring.evaluate_device(dict(device, status="revoked"), SETTINGS, "UTC", NOW,
                                          off_wrist_since=NOW - dt.timedelta(hours=1))
    assert not unpaired


def test_alert_wording_describes_and_never_diagnoses():
    person = {"display_name": "Test Person"}
    for kind, severity in (("no_answer_no_movement", "sos"), ("heart_rate_outside_range", "sos"),
                           ("missed_checkins", "urgent"), ("help_requested", "sos"), ("low_battery", "notice")):
        alert = {"kind": kind, "severity": severity, "id": "a", "details": {"summary": "Observed: something."}}
        for tier in (1, 2, 3):
            title, body = escalation.alert_text(person, alert, tier)
            assert "Test Person" in title or "Test Person" in body
            assert monitoring.no_diagnosis_words(title + body)
            if severity != "notice":
                assert "I'm on my way" in body
                assert ("call 999 now" in body) is (tier >= 3)


def test_the_system_never_dials_the_emergency_services():
    for number in ("999", "112", "911", "0999", "000"):
        with pytest.raises(providers.EmergencyNumberRefused):
            providers.assert_not_emergency_number(number)
        with pytest.raises(providers.EmergencyNumberRefused):
            asyncio.run(providers.LogOnlyProvider().call(number, "hello"))
    tw = providers.TwilioProvider("sid", "token", "+440000000009")
    with pytest.raises(providers.EmergencyNumberRefused):
        asyncio.run(tw.send_sms("999", "x"))
    providers.assert_not_emergency_number("+440000000001")


def test_the_default_provider_logs_and_sends_nothing():
    p = providers.provider_from_env({})
    assert isinstance(p, providers.LogOnlyProvider) and p.configured is False
    d = asyncio.run(p.send_sms("+440000000001", "test"))
    assert d.outcome == "logged" and p.log == [("sms", "+440000000001", "test")]
    assert providers.monitoring_centre_from_env({}).configured is False


def test_the_paid_adapter_is_off_unless_every_key_is_set():
    partial = {"IRA_CARE_TWILIO_ACCOUNT_SID": "sid", "IRA_CARE_TWILIO_AUTH_TOKEN": "tok"}
    assert isinstance(providers.provider_from_env(partial), providers.LogOnlyProvider)
    full = dict(partial, IRA_CARE_TWILIO_FROM="+440000000009")
    assert isinstance(providers.provider_from_env(full), providers.TwilioProvider)


def test_circle_roles_map_to_tiers():
    assert escalation.tier_for_role("family") == 1 and escalation.tier_for_role("carer") == 1
    assert escalation.tier_for_role("care_organisation") == 2
