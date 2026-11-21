"""Suspected falls: the server's words and settings (care/falls.py)."""
from __future__ import annotations

from care import falls, monitoring


def test_the_watch_gets_the_carers_fall_settings_with_safe_defaults():
    assert falls.watch_settings({}) == {"fall_detection_enabled": True, "fall_countdown_s": 30, "fall_impact_g": 2.5,
                                        "fall_still_seconds": 20, "fall_still_threshold_g": 0.3}
    off = falls.watch_settings({"fall_detection_enabled": False, "fall_countdown_s": 45, "fall_impact_g": 3})
    assert off["fall_detection_enabled"] is False and off["fall_countdown_s"] == 45 and off["fall_impact_g"] == 3.0


def test_the_summary_says_what_was_observed_and_never_diagnoses():
    s = falls.summary(3.4, 20)
    assert "hard impact (3.4 g)" in s and "20 seconds without movement" in s and "asked if they are OK" in s
    assert monitoring.no_diagnosis_words(s)
    assert "impact" in falls.summary(None, None) and "(" not in falls.summary(None, None)
    assert "fall" not in falls.summary(3.4, 20).lower().replace("suspected", ""), (
        "the summary describes the signal; the alert KIND says 'suspected fall'")
