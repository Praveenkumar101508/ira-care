"""Telemetry records how the connection behaved, never what was said.

This is the claim Phase 6 makes to users about live sessions, so it gets tested
as a claim rather than as a code path. The property is not "we remembered not
to log transcripts" — it is that a transcript has nowhere to go:

  * the allow-list bounds WHICH keys exist;
  * the type rule bounds WHAT can be in them, and is the load-bearing half,
    because a blocked key name is defeated by renaming the key;
  * the database CHECK constraint enforces the same thing where no code path
    can go around it (asserted in tests/tenancy/test_realtime_isolation_rls.py
    against a real database).

Also here: the latency budget. `within_budget` is frequently False, and the
tests assert it reports honestly rather than flatteringly.
"""
from __future__ import annotations

import pytest

from core.realtime.capability import CapabilityLevel, LADDER
from core.realtime.telemetry import (
    ALLOWED_METRIC_KEYS,
    ContentInTelemetry,
    assess,
    is_measurement_only,
    sanitise_metrics,
)


# ── Nothing that could be content gets through ───────────────────────────────


def test_a_transcript_is_refused():
    with pytest.raises(ContentInTelemetry):
        sanitise_metrics({"transcript": "I told IRA about my divorce"})


def test_renaming_the_key_does_not_help():
    """The load-bearing assertion. A guard that blocklists the word
    'transcript' is defeated by calling it 'note'."""
    for key in ("note", "text", "utterance", "content", "said", "detail", "x"):
        with pytest.raises(ContentInTelemetry):
            sanitise_metrics({key: "I told IRA about my divorce"})


def test_a_string_under_a_permitted_key_is_still_refused():
    """The type rule, not just the key rule. `rtt_ms` is allowed; a string in
    it is not, because the key was never the point."""
    with pytest.raises(ContentInTelemetry) as exc:
        sanitise_metrics({"rtt_ms": "48ms, and by the way she said..."})
    assert "transcript" in str(exc.value).lower(), (
        "the error should say WHY strings are refused — the next person to hit "
        "it will otherwise assume it is a type nit and cast to str"
    )


@pytest.mark.parametrize("value", [
    "text", b"audio-bytes", ["a", "list"], {"nested": "object"}, None, object(),
])
def test_only_numbers_and_booleans_are_measurements(value):
    with pytest.raises(ContentInTelemetry):
        sanitise_metrics({"rtt_ms": value})


def test_base64_audio_has_nowhere_to_go():
    """The specific shape a leak would take: audio smuggled as a string."""
    with pytest.raises(ContentInTelemetry):
        sanitise_metrics({"first_audio_ms": "UklGRiQAAABXQVZFZm10IBAAAAAB"})
    with pytest.raises(ContentInTelemetry):
        sanitise_metrics({"audio_b64": "UklGRiQAAABXQVZFZm10IBAAAAAB"})


def test_refusal_is_loud_rather_than_silent():
    """A guard that quietly discards the key leaves the caller believing
    something was recorded, and the next reader unable to tell which fields
    survive."""
    with pytest.raises(ContentInTelemetry) as exc:
        sanitise_metrics({"rtt_ms": 40, "transcript": "hello"})
    assert "transcript" in str(exc.value), "the error does not name the offender"


def test_the_error_says_how_to_add_a_metric_legitimately():
    """Both places must change together, and a developer hitting this at 2am
    should not have to go and find that out."""
    with pytest.raises(ContentInTelemetry) as exc:
        sanitise_metrics({"new_measurement": 1})
    message = str(exc.value)
    assert "ALLOWED_METRIC_KEYS" in message
    assert "027_realtime.sql" in message


def test_a_non_mapping_is_refused():
    for value in ("just a string", 42, ["a"], object()):
        with pytest.raises(ContentInTelemetry):
            sanitise_metrics(value)


# ── Real measurements are accepted, and keep their types ─────────────────────


def test_real_measurements_pass_through():
    metrics = {"first_audio_ms": 430, "budget_ms": 1200, "rtt_ms": 48.5,
               "packet_loss_pct": 0.0, "within_budget": True}
    assert sanitise_metrics(metrics) == metrics


def test_every_allowed_key_actually_accepts_a_number():
    """Guards a typo in the allow-list: a key nothing can be written under is
    an allow-list entry that does nothing."""
    for key in ALLOWED_METRIC_KEYS:
        assert sanitise_metrics({key: 1}) == {key: 1}


def test_booleans_stay_booleans():
    """`isinstance(True, int)` is True in Python. `{"within_budget": 1}` is not
    the same statement as `{"within_budget": true}`, and a UI reading it as a
    number renders "1" where it meant "yes"."""
    cleaned = sanitise_metrics({"within_budget": True})
    assert cleaned["within_budget"] is True
    assert isinstance(cleaned["within_budget"], bool)


def test_nan_and_infinity_are_refused():
    """Neither is a measurement, and neither is valid JSON — a NaN reaches the
    database as a serialisation error at the moment a problem is being
    recorded."""
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ContentInTelemetry):
            sanitise_metrics({"rtt_ms": value})


def test_none_and_empty_are_empty():
    assert sanitise_metrics(None) == {}
    assert sanitise_metrics({}) == {}


def test_the_non_raising_form_agrees_with_the_raising_one():
    assert is_measurement_only({"rtt_ms": 40}) is True
    assert is_measurement_only({"transcript": "hello"}) is False


# ── The latency budget reports honestly ──────────────────────────────────────


def test_a_fast_session_is_within_budget():
    report = assess(CapabilityLevel.VOICE_ONLY, 400)
    assert report.within_budget is True
    assert report.budget_ms == 900
    assert report.overrun_ms == 0


def test_a_slow_session_is_reported_as_over_budget():
    """The report doing its job. `within_budget` is frequently False, and
    Phase 6 claims IRA measures itself against these budgets — not that it
    meets them."""
    report = assess(CapabilityLevel.VOICE_ONLY, 3_000)
    assert report.within_budget is False
    assert report.overrun_ms == 2_100
    assert "over" in report.describe()


def test_exactly_on_budget_counts_as_within():
    assert assess(CapabilityLevel.VOICE_ONLY, 900).within_budget is True


def test_a_silent_rung_has_no_verdict_rather_than_a_failing_one():
    """text_only produces no audio. Reporting it as having missed a target it
    never had would make every text conversation look like a latency
    regression."""
    report = assess(CapabilityLevel.TEXT_ONLY, None)
    assert report.budget_ms is None
    assert report.within_budget is None
    assert report.overrun_ms is None
    assert "no audio is produced" in report.describe()


def test_no_audio_on_an_audible_rung_has_no_verdict_either():
    """Distinct from the case above: the budget is known, but nothing was
    measured. A False here would report a failure that never happened."""
    report = assess(CapabilityLevel.VOICE_ONLY, None)
    assert report.budget_ms == 900
    assert report.within_budget is None
    assert "no audio was produced" in report.describe()


def test_an_unknown_rung_yields_no_verdict():
    report = assess("holodeck", 100)
    assert report.budget_ms is None
    assert report.within_budget is None


def test_the_report_converts_to_storable_telemetry():
    """Whatever `assess` produces must be writable — otherwise the measurement
    is taken and then dropped at the boundary."""
    for profile in LADDER:
        report = assess(profile.level, 500)
        metrics = report.as_metrics()
        assert sanitise_metrics(metrics) == metrics


def test_the_stored_report_contains_no_prose():
    """`describe()` is for humans and is NOT part of `as_metrics()` — a
    sentence is a string, and a string is what this whole module refuses."""
    report = assess(CapabilityLevel.VOICE_ONLY, 400)
    assert isinstance(report.describe(), str)
    assert all(isinstance(v, (int, float, bool)) for v in report.as_metrics().values())
