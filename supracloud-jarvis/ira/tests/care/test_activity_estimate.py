"""IRA Care Activity: wrist activity from the watch accelerometer and gyroscope
(care/activity_estimate.py, weights in care/models/activity_model.json).

Every signal here is synthetic and generated in the test; no dataset file is read.
"""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from care.activity_estimate import (
    FEATURE_NAMES,
    FS_HZ,
    MODEL_PATH,
    UNKNOWN,
    ActivityModel,
    check_window,
    default_model,
    estimate_activity,
    window_features,
)

SECONDS = 10
N = int(SECONDS * FS_HZ)
CLASSES = ["walking", "stairs", "sitting", "standing", "eating_drinking", "other"]


def wrist(swing_hz=0.0, amplitude=0.0, seed=0, n=N, fs=FS_HZ):
    """Accelerometer (m/s^2, gravity on z) and gyroscope (rad/s) of a wrist that is still
    or swings at ``swing_hz``."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs
    acc = np.tile([0.0, 0.0, 9.81], (n, 1)) + 0.03 * rng.standard_normal((n, 3))
    gyro = 0.01 * rng.standard_normal((n, 3))
    if swing_hz:
        acc[:, 0] += amplitude * np.sin(2 * np.pi * swing_hz * t)
        acc[:, 1] += 0.4 * amplitude * np.sin(2 * np.pi * swing_hz * t + 1.0)
        gyro[:, 2] += 0.3 * amplitude * np.cos(2 * np.pi * swing_hz * t)
    return acc, gyro


def _spec():
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def reference_error(model: ActivityModel, spec: dict) -> float:
    ref = spec["reference"]
    got = model.predict_proba(np.array(ref["features"]))
    return float(np.abs(got - np.array(ref["probabilities"])).max())


# ── the shipped model file ───────────────────────────────────────────────────

def test_the_model_file_is_small_json_with_the_expected_classes():
    raw = MODEL_PATH.read_bytes()
    assert len(raw) < 1_000_000
    spec = json.loads(raw)
    assert spec["classes"] == CLASSES
    assert spec["unknown_label"] == UNKNOWN
    assert spec["feature_names"] == list(FEATURE_NAMES)
    assert spec["provenance"]["licence"] == "CC BY 4.0"
    assert "10.24432/C5HK59" in spec["provenance"]["citation"]


def test_the_json_model_reproduces_the_training_time_reference_predictions():
    spec = _spec()
    model = ActivityModel(spec)
    assert len(spec["reference"]["features"]) >= 10
    assert reference_error(model, spec) <= spec["reference"]["tolerance"]
    got = model.predict_proba(np.array(spec["reference"]["features"])).argmax(1)
    assert (got == np.array(spec["reference"]["probabilities"]).argmax(1)).all()


def test_negative_control_shuffled_weights_are_caught():
    """The reference check must fail for a deliberately wrong model."""
    spec = _spec()
    rng = np.random.default_rng(0)
    bad = copy.deepcopy(spec)
    W = np.array(bad["layers"][0]["W"])
    bad["layers"][0]["W"] = W[rng.permutation(W.shape[0])].tolist()  # rows shuffled
    assert reference_error(ActivityModel(bad), spec) > 1e-3

    swapped = copy.deepcopy(spec)  # output columns swapped: same numbers, wrong classes
    W = np.array(swapped["layers"][-1]["W"])
    b = np.array(swapped["layers"][-1]["b"])
    swapped["layers"][-1]["W"] = W[:, ::-1].tolist()
    swapped["layers"][-1]["b"] = b[::-1].tolist()
    assert reference_error(ActivityModel(swapped), spec) > 1e-3

    unscaled = copy.deepcopy(spec)  # standardisation dropped
    unscaled["standardise"]["mean"] = [0.0] * len(FEATURE_NAMES)
    unscaled["standardise"]["scale"] = [1.0] * len(FEATURE_NAMES)
    assert reference_error(ActivityModel(unscaled), spec) > 1e-3


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s.update(format="pickle"), "format"),
    (lambda s: s.update(feature_names=s["feature_names"][::-1]), "features"),
    (lambda s: s["layers"][0].update(b=s["layers"][0]["b"][:-1]), "shapes"),
    (lambda s: s.update(classes=s["classes"][:-1]), "classes"),
    (lambda s: s["standardise"].update(scale=[0.0] * len(FEATURE_NAMES)), "standardisation"),
])
def test_a_malformed_model_file_is_refused(mutate, message):
    spec = _spec()
    mutate(spec)
    with pytest.raises(ValueError, match=message):
        ActivityModel(spec)


def test_non_finite_weights_are_refused():
    spec = _spec()
    spec["layers"][1]["W"][0][0] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        ActivityModel(spec)


# ── refusal on bad input: no estimate, never a guess ─────────────────────────

def _refused(r, reason):
    assert r.activity is None and not r.reliable
    assert r.reason == reason
    assert r.probabilities == {} and r.confidence == 0.0


def test_too_short_is_refused():
    acc, gyro = wrist(n=int(7.5 * FS_HZ))
    _refused(estimate_activity(acc, gyro), "too_short")


def test_flat_accelerometer_is_refused():
    acc, gyro = wrist()
    acc[:] = [0.0, 0.0, 9.81]
    _refused(estimate_activity(acc, gyro), "flat_accelerometer")


def test_flat_gyroscope_is_refused():
    acc, gyro = wrist(1.5, 3.0)
    gyro[:] = 0.0
    _refused(estimate_activity(acc, gyro), "flat_gyroscope")


def test_saturated_accelerometer_is_refused():
    acc, gyro = wrist(2.0, 80.0)
    acc[:, 0] = np.clip(acc[:, 0], -19.6, 19.6)  # a +-2 g sensor pinned on most samples
    assert np.mean(acc[:, 0] == 19.6) > 0.1
    _refused(estimate_activity(acc, gyro), "saturated_accelerometer")


def test_saturated_gyroscope_is_refused():
    acc, gyro = wrist(1.5, 3.0)
    gyro[:, 2] = np.clip(30 * np.sin(np.linspace(0, 30, N)), -8.7, 8.7)
    _refused(estimate_activity(acc, gyro), "saturated_gyroscope")


def test_not_finite_is_refused():
    acc, gyro = wrist()
    acc[10, 1] = np.nan
    _refused(estimate_activity(acc, gyro), "not_finite")
    acc, gyro = wrist()
    gyro[3, 0] = np.inf
    _refused(estimate_activity(acc, gyro), "not_finite")


def test_accelerometer_in_g_instead_of_m_per_s2_is_refused():
    acc, gyro = wrist(1.0, 2.0)
    _refused(estimate_activity(acc / 9.81, gyro), "implausible_gravity")


@pytest.mark.parametrize("acc, gyro, reason", [
    (np.zeros((N, 2)), np.zeros((N, 3)), "bad_accelerometer_shape"),
    (np.zeros(N), np.zeros((N, 3)), "bad_accelerometer_shape"),
    (np.zeros((N, 3)), None, "bad_gyroscope_shape"),
    ("not a signal", np.zeros((N, 3)), "bad_accelerometer_shape"),
])
def test_wrong_shapes_are_refused(acc, gyro, reason):
    _refused(estimate_activity(acc, gyro), reason)


def test_a_bad_rate_is_refused():
    acc, gyro = wrist()
    _refused(estimate_activity(acc, gyro, fs_hz=0.0), "bad_rate")
    _refused(estimate_activity(acc, gyro, gyro_fs_hz=float("nan")), "bad_rate")


# ── answers ──────────────────────────────────────────────────────────────────

def test_a_good_window_gets_a_class_or_unknown_with_probabilities():
    acc, gyro = wrist(1.8, 4.0, seed=3)
    r = estimate_activity(acc, gyro)
    assert r.activity in CLASSES + [UNKNOWN]
    assert r.candidate in CLASSES
    assert set(r.probabilities) == set(CLASSES)
    assert abs(sum(r.probabilities.values()) - 1.0) < 1e-3
    assert r.confidence == pytest.approx(max(r.probabilities.values()), abs=1e-4)


def test_low_confidence_is_reported_as_unknown_not_as_a_class():
    acc, gyro = wrist(1.8, 4.0, seed=3)
    r = estimate_activity(acc, gyro, min_confidence=1.01)
    assert r.activity == UNKNOWN and not r.reliable
    assert r.reason == "low_confidence" and r.candidate in CLASSES
    r = estimate_activity(acc, gyro, min_confidence=0.0)
    assert r.activity == r.candidate and r.reliable and r.reason is None


def test_determinism_same_input_same_answer():
    acc, gyro = wrist(2.2, 5.0, seed=7)
    a = estimate_activity(acc, gyro)
    b = estimate_activity(acc.copy(), gyro.copy())
    assert a == b
    fa = window_features(*check_window(acc, gyro)[:2])
    fb = window_features(*check_window(acc.copy(), gyro.copy())[:2])
    assert np.array_equal(fa, fb)
    assert len(fa) == len(FEATURE_NAMES) and np.isfinite(fa).all()
    assert default_model() is default_model()


def test_a_longer_window_uses_the_last_ten_seconds():
    acc, gyro = wrist(1.6, 3.0, seed=2, n=3 * N)
    a, g, why = check_window(acc, gyro)
    assert why is None and len(a) == N and np.array_equal(a, acc[-N:])
    assert estimate_activity(acc, gyro) == estimate_activity(acc[-N:], gyro[-N:])


def test_another_sampling_rate_is_resampled_to_20_hz():
    acc50, gyro50 = wrist(1.5, 3.0, seed=4, n=int(SECONDS * 50), fs=50.0)
    a, g, why = check_window(acc50, gyro50, fs_hz=50.0)
    assert why is None and abs(len(a) - N) <= 1 and len(a) == len(g)
    r = estimate_activity(acc50, gyro50, fs_hz=50.0)
    assert r.activity is not None and set(r.probabilities) == set(CLASSES)


def test_a_still_wrist_and_a_swinging_wrist_are_told_apart():
    """Only a sanity check on synthetic signals, not an accuracy claim: a wrist that does
    not move is never read as walking or stairs, and the two windows differ."""
    still = estimate_activity(*wrist(seed=5))
    moving = estimate_activity(*wrist(1.8, 6.0, seed=5))
    assert still.candidate not in ("walking", "stairs")
    assert still.probabilities != moving.probabilities
