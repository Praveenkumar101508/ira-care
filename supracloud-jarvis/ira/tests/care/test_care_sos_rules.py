"""The SOS state the wrist shows, as a pure function (care/sos.py)."""
from __future__ import annotations

import datetime as dt
import uuid

from care import sos

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 23, 2, 0, tzinfo=UTC)
AID, M1, M2, U1 = (uuid.uuid4() for _ in range(4))


def _alert(status="open", **extra):
    base = {"id": AID, "kind": "help_requested", "status": status, "opened_at": T0, "acknowledged_at": None,
            "resolved_at": None}
    base.update(extra)
    return base


def _ack(kind, at_s, member_id=M1, user_id=None):
    return {"kind": kind, "member_id": member_id, "user_id": user_id, "acknowledged_at": T0 + dt.timedelta(seconds=at_s)}


NAMES = {M1: "Sravani", M2: "Praveen", U1: "A carer"}


def test_no_alert_is_idle():
    assert sos.state_of(None, [], responders={})["state"] == "idle"


def test_an_open_alert_with_nobody_coming_is_received_and_never_says_help_is_coming():
    out = sos.state_of(_alert(), [], responders=NAMES)
    assert out["state"] == "received" and out["responder_name"] is None
    assert sos.state_of(_alert("escalating"), [], responders=NAMES)["state"] == "received"


def test_asking_is_its_own_state():
    assert sos.state_of(_alert("asking"), [], responders=NAMES)["state"] == "asking"


def test_on_my_way_names_the_responder():
    out = sos.state_of(_alert("acknowledged", acknowledged_at=T0), [_ack("on_my_way", 10)], responders=NAMES)
    assert (out["state"], out["responder_name"]) == ("on_the_way", "Sravani")


def test_arrival_beats_on_my_way_whoever_said_it_first():
    acks = [_ack("on_my_way", 10, M1), _ack("on_my_way", 20, M2), _ack("arrived", 30, M1)]
    out = sos.state_of(_alert("acknowledged"), acks, responders=NAMES)
    assert (out["state"], out["responder_name"]) == ("arrived", "Sravani")
    # checked_in_person counts as arrival too.
    out = sos.state_of(_alert("acknowledged"), [_ack("checked_in_person", 40, M2)], responders=NAMES)
    assert (out["state"], out["responder_name"]) == ("arrived", "Praveen")


def test_the_newest_responder_wins_among_those_on_the_way():
    acks = [_ack("on_my_way", 10, M1), _ack("on_my_way", 20, M2)]
    assert sos.state_of(_alert("acknowledged"), acks, responders=NAMES)["responder_name"] == "Praveen"


def test_a_responder_known_only_by_account_is_still_named():
    out = sos.state_of(_alert("acknowledged"), [_ack("on_my_way", 10, member_id=None, user_id=U1)], responders=NAMES)
    assert out["responder_name"] == "A carer"
    out = sos.state_of(_alert("acknowledged"), [_ack("on_my_way", 10, member_id=None, user_id=uuid.uuid4())], responders=NAMES)
    assert out["responder_name"] == "Someone"


def test_resolved_or_cancelled_is_closed():
    for status in ("resolved", "cancelled"):
        out = sos.state_of(_alert(status, resolved_at=T0 + dt.timedelta(minutes=30)), [_ack("arrived", 10)], responders=NAMES)
        assert out["state"] == "closed" and out["alert_id"] == str(AID)


def test_the_watch_settings_carry_the_range_and_the_upload_interval_with_defaults():
    assert sos.watch_settings({}) == {"heart_rate_min": None, "heart_rate_max": None, "reading_upload_interval_s": 60}
    out = sos.watch_settings({"heart_rate_min": 45, "heart_rate_max": 130, "reading_upload_interval_s": 30})
    assert out == {"heart_rate_min": 45, "heart_rate_max": 130, "reading_upload_interval_s": 30}
