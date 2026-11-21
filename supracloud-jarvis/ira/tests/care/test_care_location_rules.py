"""Where the person is (care/location.py) and who may open the access notes."""
from __future__ import annotations

import datetime as dt
import uuid

from care import accessnotes, escalation, location

HOME = {"home_address": "1 Example Street, Testtown, TE5 7ST", "home_latitude": 51.5000, "home_longitude": -0.1200,
        "home_radius_m": 150, "display_name": "Test Person", "id": uuid.uuid4()}
AT_HOME = location.Location(51.5003, -0.1201, 20)       # about 35 m away
AWAY = location.Location(51.5200, -0.1200, 15)          # about 2.2 km north


def test_distance_and_at_home_use_the_radius_and_the_accuracy():
    assert 30 < location.distance_m(51.5, -0.12, 51.5003, -0.1201) < 40
    assert location.at_home(AT_HOME, HOME) is True
    assert location.at_home(AWAY, HOME) is False
    # A poor fix that might still be at home is not reported as away.
    edge = location.Location(51.5015, -0.12, 200)        # about 165 m, accuracy 200 m
    assert location.at_home(edge, HOME) is True
    assert location.at_home(location.Location(51.5015, -0.12, 5), HOME) is False


def test_unknown_is_unknown_never_a_guess():
    assert location.at_home(None, HOME) is None
    assert location.at_home(AT_HOME, dict(HOME, home_latitude=None, home_longitude=None)) is None
    assert location.from_body(None) is None and location.from_body({"latitude": 1}) is None


def test_map_link_is_a_plain_web_link_with_no_key():
    link = location.map_link(AT_HOME)
    assert link.startswith("https://www.openstreetmap.org/?mlat=51.50030&mlon=-0.12010")
    assert "key=" not in link and "token" not in link


def _alert(loc=None, home=None):
    a = {"id": uuid.uuid4(), "kind": "help_requested", "severity": "sos", "details": {"summary": "x"}, "at_home": home}
    if loc:
        a.update(location_latitude=loc.latitude, location_longitude=loc.longitude, location_accuracy_m=loc.accuracy_m)
    return a


def test_whereabouts_in_plain_words():
    assert location.whereabouts(_alert(AT_HOME, True), HOME).startswith("The person appears to be AT HOME: 1 Example Street")
    away = location.whereabouts(_alert(AWAY, False), HOME)
    assert "AWAY FROM HOME, about 2.2 km from 1 Example Street" in away and "openstreetmap" in away
    assert "accuracy 15 m" in away
    none = location.whereabouts(_alert(), HOME)
    assert none.startswith("Location unavailable") and "Home address: 1 Example Street" in none
    unknown = location.whereabouts(_alert(AT_HOME), dict(HOME, home_latitude=None, home_longitude=None))
    assert "unknown" in unknown and "openstreetmap" in unknown


def test_sos_messages_carry_the_address_the_whereabouts_and_the_alert_link(monkeypatch):
    monkeypatch.setenv("IRA_CARE_PUBLIC_URL", "https://care.example.invalid/")
    alert = _alert(AWAY, False)
    for tier in (1, 2, 3):
        title, body = escalation.alert_text(HOME, alert, tier)
        assert "Test Person" in title
        assert "AWAY FROM HOME" in body and "1 Example Street" in body
        assert f"https://care.example.invalid/alerts/{alert['id']}" in body
    _t, body = escalation.alert_text(HOME, _alert(), 1)
    assert "Location unavailable" in body and "1 Example Street" in body
    _t, notice = escalation.alert_text(HOME, {"id": uuid.uuid4(), "kind": "low_battery", "severity": "notice", "details": {"summary": "Battery at 10 %."}}, 1)
    assert "Example Street" not in notice, "a notice about the watch does not broadcast the address"


def test_access_notes_are_for_tier_1_and_2_while_the_alert_is_open():
    me, other = uuid.uuid4(), uuid.uuid4()
    circle = [{"user_id": me, "tier": 1, "active": True}, {"user_id": other, "tier": 3, "active": True}]
    assert accessnotes.can_view("open", viewer_user_id=me, circle=circle, is_manager=False)
    assert accessnotes.can_view("acknowledged", viewer_user_id=me, circle=circle, is_manager=False)
    assert not accessnotes.can_view("resolved", viewer_user_id=me, circle=circle, is_manager=False)
    assert not accessnotes.can_view("open", viewer_user_id=other, circle=circle, is_manager=False), "tier 3 never"
    assert not accessnotes.can_view("open", viewer_user_id=uuid.uuid4(), circle=circle, is_manager=False)
    assert accessnotes.can_view("open", viewer_user_id=uuid.uuid4(), circle=[], is_manager=True)
    assert not accessnotes.can_view("resolved", viewer_user_id=uuid.uuid4(), circle=[], is_manager=True)
    inactive = [{"user_id": me, "tier": 1, "active": False}]
    assert not accessnotes.can_view("open", viewer_user_id=me, circle=inactive, is_manager=False)


def test_sealed_notes_open_only_for_their_own_person(monkeypatch):
    import base64, os
    import pytest
    monkeypatch.setenv("IRA_CONNECTOR_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("IRA_CONNECTOR_ENCRYPTION_KEY_ID", raising=False)
    t, p1, p2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cols = accessnotes.seal("Key safe by the side door, code on file. Small dog.", tenant_id=t, person_id=p1)
    assert set(cols) == set(accessnotes.COLUMNS) and all(v is not None for v in cols.values())
    assert b"Key safe" not in cols["access_notes_ciphertext"] and cols["access_notes_key_version"] == 1
    row = dict(cols, id=p1)
    assert accessnotes.use_notes(row, lambda s: s, tenant_id=t).startswith("Key safe")
    with pytest.raises(accessnotes.AccessNotesUnavailable):
        accessnotes.use_notes(dict(row, id=p2), lambda s: s, tenant_id=t)   # lifted onto another person's row
    with pytest.raises(accessnotes.AccessNotesUnavailable):
        accessnotes.use_notes(row, lambda s: s, tenant_id=uuid.uuid4())      # or another tenant's
    # A different key-encryption key cannot open it either (same message: no oracle).
    monkeypatch.setenv("IRA_CONNECTOR_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(accessnotes.AccessNotesUnavailable):
        accessnotes.use_notes(row, lambda s: s, tenant_id=t)
    assert all(v is None for v in accessnotes.seal("", tenant_id=t, person_id=p1).values())
    assert accessnotes.use_notes({"id": p1, "access_notes_ciphertext": None}, lambda s: s, tenant_id=t) == ""
    assert isinstance(dt.datetime.now(), dt.datetime)
