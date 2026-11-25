"""IRA Care Heart: heart rate from a wrist PPG window (care/heart_estimate.py).

Every signal here is synthetic and generated in the test; no dataset file is read.
"""
from __future__ import annotations

import numpy as np
import pytest

from care.heart_estimate import (
    ACC_HZ,
    BVP_HZ,
    HeartTracker,
    estimate_heart_rate,
    naive_spectral_peak_bpm,
)

SECONDS = 8


def _t(fs):
    return np.arange(int(SECONDS * fs)) / fs


def pulse(bpm=72.0, seed=0):
    """A pulse-like wave: fundamental, a smaller second harmonic, a little noise."""
    rng = np.random.default_rng(seed)
    t = _t(BVP_HZ)
    f = bpm / 60.0
    return (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t + 0.5)
            + 0.05 * rng.standard_normal(len(t)))


def arm_swing(hz, amplitude_g=0.5, seed=1):
    """Wrist accelerometer (g) while the arm swings at `hz`, gravity on z."""
    rng = np.random.default_rng(seed)
    t = _t(ACC_HZ)
    return np.stack([amplitude_g * np.sin(2 * np.pi * hz * t),
                     0.4 * amplitude_g * np.sin(2 * np.pi * hz * t + 1.0),
                     np.ones_like(t)], axis=1) + 0.01 * rng.standard_normal((len(t), 3))


def motion_artefact(hz, amplitude=3.0):
    return amplitude * np.sin(2 * np.pi * hz * _t(BVP_HZ))


def test_a_clean_72_bpm_pulse_reads_about_72():
    r = estimate_heart_rate(pulse(72.0))
    assert r.reliable and r.reason is None
    assert abs(r.bpm - 72.0) <= 2.0
    assert r.confidence > 0.5


@pytest.mark.parametrize("bpm", [48.0, 95.0, 150.0])
def test_other_rates_are_found_too(bpm):
    r = estimate_heart_rate(pulse(bpm), arm_swing(1.0, amplitude_g=0.0))
    assert r.reliable and abs(r.bpm - bpm) <= 2.0


@pytest.mark.parametrize("swing_hz", [0.9, 1.6, 1.9, 2.6])
def test_motion_at_the_arm_swing_frequency_is_suppressed(swing_hz):
    bvp = pulse(72.0) + motion_artefact(swing_hz)  # the artefact is 3x the pulse
    r = estimate_heart_rate(bvp, arm_swing(swing_hz))
    assert r.motion_suppressed
    assert r.reliable and abs(r.bpm - 72.0) <= 3.0, r


@pytest.mark.parametrize("swing_hz", [0.9, 1.6, 1.9, 2.6])
def test_negative_control_naive_argmax_without_acc_follows_the_motion(swing_hz):
    """The same windows through the naive peak picker (no accelerometer) read the arm
    swing, not the heart: the suppression above is what makes the difference."""
    bvp = pulse(72.0) + motion_artefact(swing_hz)
    naive = naive_spectral_peak_bpm(bvp)
    assert abs(naive - 72.0) > 10.0
    assert abs(naive - swing_hz * 60.0) <= 2.0
    # and the estimator itself, denied the accelerometer, is fooled the same way
    no_acc = estimate_heart_rate(bvp, None, min_confidence=0.0)
    assert abs(no_acc.candidate_bpm - 72.0) > 10.0


def test_a_still_wrist_does_not_trigger_suppression():
    r = estimate_heart_rate(pulse(72.0), arm_swing(1.6, amplitude_g=0.0))
    assert not r.motion_suppressed and abs(r.bpm - 72.0) <= 2.0


def test_acc_in_metres_per_second_squared_is_scaled():
    acc = arm_swing(1.6) * 9.80665
    r = estimate_heart_rate(pulse(72.0) + motion_artefact(1.6), acc, acc_scale_g=1 / 9.80665)
    assert r.motion_suppressed and abs(r.bpm - 72.0) <= 3.0


def test_flat_input_gets_no_estimate():
    for flat in (np.zeros(512), np.full(512, 812.5)):
        r = estimate_heart_rate(flat)
        assert r.bpm is None and r.reason == "flat" and r.confidence == 0.0


def test_too_short_input_gets_no_estimate():
    r = estimate_heart_rate(pulse(72.0)[: int(5 * BVP_HZ)])
    assert r.bpm is None and r.reason == "too_short"
    assert estimate_heart_rate([]).reason == "too_short"


def test_nan_or_infinite_input_gets_no_estimate():
    bvp = pulse(72.0)
    bvp[100] = np.nan
    assert estimate_heart_rate(bvp).reason == "not_finite"
    bvp[100] = np.inf
    assert estimate_heart_rate(bvp).bpm is None
    acc = arm_swing(1.6)
    acc[5, 0] = np.nan
    r = estimate_heart_rate(pulse(72.0), acc)
    assert r.bpm is None and r.reason == "acc_not_finite"


def test_saturated_input_gets_no_estimate():
    r = estimate_heart_rate(np.clip(pulse(72.0), -0.5, 0.5))
    assert r.bpm is None and r.reason == "saturated"


def test_noise_alone_is_not_a_reliable_estimate():
    noise = np.random.default_rng(7).standard_normal(512)
    r = estimate_heart_rate(noise)
    assert r.bpm is None and r.reason == "low_confidence"
    assert r.candidate_bpm is not None  # the peak is still reported, for tracking


def test_a_short_accelerometer_window_is_refused():
    r = estimate_heart_rate(pulse(72.0), arm_swing(1.6)[:50])
    assert r.bpm is None and r.reason == "acc_too_short"


def test_the_tracker_follows_a_slow_rise_through_motion():
    # The swing stays at least 0.3 Hz from the pulse: closer than about the 8 s window's
    # resolution (0.125 Hz) the two cannot be told apart, which the model card states.
    tracker = HeartTracker()
    out = []
    for k, bpm in enumerate(np.linspace(70, 90, 11)):
        swing = 1.9 if k % 2 else 2.4
        r = tracker.update(pulse(bpm, seed=k) + motion_artefact(swing), arm_swing(swing, seed=k))
        out.append((bpm, r.bpm))
    assert all(est is not None and abs(est - bpm) <= 3.0 for bpm, est in out), out


def test_a_refused_window_does_not_disturb_the_tracker():
    tracker = HeartTracker()
    tracker.update(pulse(72.0))
    assert tracker.update(np.zeros(512)).reason == "flat"
    assert tracker.prev_bpm is not None and abs(tracker.prev_bpm - 72.0) <= 2.0
