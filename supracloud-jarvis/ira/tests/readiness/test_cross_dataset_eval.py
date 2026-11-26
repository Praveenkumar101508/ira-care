"""The pure parts of the two cross-dataset evaluations (requirements F4.f and F3).

scripts/eval/fall_cross_weda.py runs the shipped fall pipeline on WEDA-FALL;
scripts/eval/heart_cross_galaxyppg.py runs the heart research pipeline on GalaxyPPG. Their
numbers are only as good as the conversions in front of them, so those are checked here on
synthetic signals: units (a stream in the wrong unit is refused, not rescaled), timestamps
(out-of-order rows, misaligned clocks), resampling, the R-peak reference, and the Python port
of the watch's fall detectors against the cases in RecoveringFallDetectorTest.kt.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[4]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fall = _load("fall_cross_weda", "scripts/eval/fall_cross_weda.py")
heart = _load("heart_cross_galaxyppg", "scripts/eval/heart_cross_galaxyppg.py")
G = 9.80665


# ── fall: units, timestamps, rate ────────────────────────────────────────────

def _still_ms2(n=500, seed=0):
    rng = np.random.default_rng(seed)
    return np.c_[rng.normal(0, 0.2, n), rng.normal(0, 0.2, n), G + rng.normal(0, 0.2, n)]


def test_metres_per_second_squared_are_accepted_and_divided_by_g():
    assert fall.check_units_ms2(_still_ms2()) == pytest.approx(1.0, abs=0.02)
    acc = fall.resample_to_watch(np.tile([0.0, 0.0, G], (100, 1)), 50)
    assert np.allclose(np.linalg.norm(acc, axis=1), 1.0)


@pytest.mark.parametrize("scale", [1 / G, 1000 / G])     # the same signal already in g, or in mg
def test_a_recording_in_the_wrong_unit_is_refused_not_rescaled(scale):
    with pytest.raises(fall.RecordingError) as e:
        fall.check_units_ms2(_still_ms2() * scale)
    assert e.value.code == "unit_mismatch"


def test_vigorous_movement_in_metres_per_second_squared_is_not_mistaken_for_a_unit_error():
    t = np.arange(500) / 50
    jog = _still_ms2() + np.c_[np.zeros(500), np.zeros(500), 9 * np.abs(np.sin(2 * np.pi * 1.4 * t))]
    assert 1.2 < fall.check_units_ms2(jog) < 2.0


def _batched_times(n, hz, batch=5, seed=0):
    """Delivery times as WEDA-FALL has them: batches of samples share nearly one timestamp."""
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    return (i // batch) * batch / hz + rng.uniform(0, 0.01) + (i % batch) * 0.001


def test_a_block_delivered_out_of_order_is_put_back_in_time_order():
    t = _batched_times(400, 50)
    xyz = np.arange(400 * 3, dtype=float).reshape(400, 3)
    t2, x2 = t.copy(), xyz.copy()
    t2[200:219], t2[219:238] = t[219:238], t[200:219]          # two blocks swapped, as in 24 files
    x2[200:219], x2[219:238] = xyz[219:238], xyz[200:219]
    ts, xs, moved = fall.order_by_time(t2, x2)
    assert moved > 0
    assert np.array_equal(xs, xyz) and np.all(np.diff(ts) >= 0)


def test_timestamps_that_jump_back_by_seconds_are_refused():
    t = _batched_times(400, 50)
    t[300:] -= 5.0
    with pytest.raises(fall.RecordingError) as e:
        fall.order_by_time(t, np.zeros((400, 3)))
    assert e.value.code == "misaligned_timestamps"


@pytest.mark.parametrize("hz", [40, 50])
def test_the_native_rate_is_read_from_batched_delivery_times(hz):
    assert fall.native_rate(_batched_times(600, hz)) == hz


def test_a_recording_that_lost_samples_is_refused():
    t = _batched_times(600, 50)[::2]                          # every other sample missing: 25 Hz
    with pytest.raises(fall.RecordingError) as e:
        fall.native_rate(t)
    assert e.value.code == "rate_unknown"


def test_forty_hertz_is_interpolated_onto_the_watch_s_fifty_hertz_grid():
    n = 400                                                   # 10 s at 40 Hz
    src_t = np.arange(n) / 40
    xyz = np.c_[np.sin(2 * np.pi * 0.5 * src_t), np.zeros(n), np.ones(n)]      # in g
    out = fall.resample_to_watch(xyz * G, 40)
    assert len(out) == int((n - 1) / 40 * 50) + 1
    dst_t = np.arange(len(out)) / 50
    assert np.allclose(out[:, 0], np.sin(2 * np.pi * 0.5 * dst_t), atol=0.01)
    assert np.allclose(out[::5, 0], xyz[::4, 0][: len(out[::5])], atol=1e-9)   # shared instants unchanged


# ── fall: the port of the watch's detectors (RecoveringFallDetectorTest.kt) ──

SPEC = {"threshold": 0.95, "mean": [0, 0, 0, 0], "std": [1, 1, 1, 1], "windowSamples": 200, "rateHz": 50, "candidateG": 2.0}
HZ = 50


def _impact(peak_g, after):
    rng = np.random.default_rng(20261126)
    mags = list(1.0 + rng.uniform(-0.05, 0.05, 5 * HZ)) + [0.3] * (HZ // 4) + [peak_g * 0.8, peak_g, 1.6, 1.2]
    return mags + after


def _still(seconds):
    return list(1.0 + np.random.default_rng(1).uniform(-0.05, 0.05, seconds * HZ))


def _moving(seconds):
    return [1.6 if i % 10 < 5 else 0.5 for i in range(seconds * HZ)]


def _run(mags, infer, settings=None):
    d = fall.RecoveringFallDetector(settings or fall.FallSettings(), SPEC, infer)
    events = [d.feed(i * 1000 // HZ, 0.0, 0.0, m) for i, m in enumerate(mags)]
    return [e for e in events if e is not None], d


def _rules(mags):
    d = fall.FallDetector()
    return [e for e in (d.feed(i * 1000 // HZ, m) for i, m in enumerate(mags)) if e is not None and e[0] == "fall"]


def test_port_a_2_2_g_fall_the_rules_never_see_is_raised_by_the_model():
    mags = _impact(2.2, _still(25))
    assert _rules(mags) == []
    raised, d = _run(mags, lambda w: np.float32(0.99))
    assert len(raised) == 1 and raised[0][4] == "model" and raised[0][3] == 0
    assert raised[0][2] == pytest.approx(2.2)


def test_port_the_rules_stay_the_backstop_when_the_model_says_no():
    raised, _ = _run(_impact(3.5, _still(25)), lambda w: np.float32(0.01))
    assert [(e[4], e[3]) for e in raised] == [("rule", 20)]


def test_port_movement_after_the_impact_clears_the_rules_but_not_the_model():
    mags = _impact(3.5, _moving(25))
    assert _rules(mags) == []
    assert len(_run(mags, lambda w: np.float32(0.99))[0]) == 1
    assert _run(mags, lambda w: np.float32(0.5))[0] == []


def test_port_below_the_candidate_impact_the_model_is_never_asked():
    asked = []
    raised, _ = _run(_impact(1.8, _still(25)), lambda w: asked.append(1) or np.float32(0.99))
    assert raised == [] and asked == []


def test_port_the_model_sees_the_peak_in_the_middle_of_its_window():
    seen = []
    _run(_impact(2.6, _moving(10)), lambda w: seen.append(w) or np.float32(0.0))
    assert seen and seen[0].shape == (200, 4)
    assert 97 <= int(np.argmax(seen[0][:, 3])) <= 103


def test_port_a_failing_model_is_the_rules_alone():
    def broken(w):
        raise RuntimeError("no model")
    raised, d = _run(_impact(3.5, _still(25)), broken)
    assert [e[4] for e in raised] == ["rule"] and d.last_probability is None


def test_the_threshold_is_compared_as_the_watch_s_float():
    # 0.95 as a Float is 0.949999988; the watch raises at exactly that value.
    raised, _ = _run(_impact(2.2, _still(25)), lambda w: np.float32(0.95))
    assert len(raised) == 1


# ── fall: scoring ────────────────────────────────────────────────────────────

def test_a_fall_counts_only_near_its_annotation():
    assert fall.caught([5.4], 3.0, 4.5)                      # within the 1 s tolerance
    assert not fall.caught([5.6], 3.0, 4.5)
    assert not fall.caught([], 3.0, 4.5)


def test_a_second_detection_inside_the_countdown_is_ignored_as_on_the_watch():
    mags = _impact(2.2, _still(5)) + _impact(2.4, _still(5))[5 * HZ:]
    res = fall.stream(np.c_[np.zeros(len(mags)), np.zeros(len(mags)), mags], spec=SPEC,
                      infer=lambda w: np.float32(0.99), config="shipped")
    assert len(res["raised"]) == 1 and len(res["ignored_during_countdown"]) == 1
    assert fall.countdown_running_at(res, res["ignored_during_countdown"][0]["at_s"])


def test_an_open_rule_impact_counts_only_under_convention_b():
    mags = _impact(3.0, _still(5))                           # ends 5 s into the 20 s still window
    res = fall.stream(np.c_[np.zeros(len(mags)), np.zeros(len(mags)), mags], spec=SPEC,
                      infer=lambda w: np.float32(0.0), config="rule")
    assert fall.alarms(res, "a") == [] and len(fall.alarms(res, "b")) == 1


def test_the_model_only_configuration_never_uses_the_rules():
    mags = _impact(3.5, _still(25))
    res = fall.stream(np.c_[np.zeros(len(mags)), np.zeros(len(mags)), mags], spec=SPEC,
                      infer=lambda w: np.float32(0.0), config="model")
    assert res["raised"] == [] and res["open_rule_impact_s"] is None


# ── heart: units and clocks ──────────────────────────────────────────────────

def test_wrist_accelerometers_in_their_declared_units_are_accepted():
    galaxy = _still_ms2()
    e4 = np.round(_still_ms2() / G * 64)
    assert heart.check_acc_unit(galaxy, "m/s2") == pytest.approx(1.0, abs=0.02)
    assert heart.check_acc_unit(e4, "e4_raw") == pytest.approx(1.0, abs=0.03)


@pytest.mark.parametrize("data,declared", [("galaxy", "g"), ("e4", "g"), ("e4", "m/s2"), ("galaxy", "e4_raw")])
def test_an_accelerometer_declared_in_the_wrong_unit_is_refused(data, declared):
    samples = _still_ms2() if data == "galaxy" else np.round(_still_ms2() / G * 64)
    with pytest.raises(heart.AlignmentError) as e:
        heart.check_acc_unit(samples, declared)
    assert e.value.code == "unit_mismatch"


def test_polar_timestamps_are_moved_from_utc_plus_nine():
    assert heart.polar_seconds(np.array([1710844279359.0]))[0] == pytest.approx(1710844279.359 - 32400)


def _activity(t, seed):
    """|a| in g: rest, then bursts of movement at random times (shared by both devices)."""
    rng = np.random.default_rng(seed)
    a = np.ones_like(t)
    for start in rng.uniform(t[0] + 20, t[-1] - 40, 8):
        m = (t >= start) & (t < start + rng.uniform(8, 25))
        a[m] += 0.6 * np.abs(np.sin(2 * np.pi * 1.5 * t[m]))
    return a


def test_the_clock_lag_between_wrist_and_chest_is_recovered_from_movement():
    tw = np.arange(0, 600, 1 / 32)
    tc = np.arange(0, 600, 1 / 200)
    true_lag = 2.3                                             # chest time t is wrist time t + 2.3
    wrist = _activity(tw, 7)
    chest = np.interp(tc + true_lag, tw, wrist)
    env_w = heart.motion_envelope(tw, np.c_[wrist, 0 * wrist, 0 * wrist], 0, 600)
    env_c = heart.motion_envelope(tc, np.c_[chest, 0 * chest, 0 * chest], 0, 600)
    lag, corr = heart.estimate_lag(env_w, env_c)
    assert lag == pytest.approx(true_lag, abs=0.15) and corr > 0.9
    heart.check_alignment(lag, corr)


@pytest.mark.parametrize("lag,corr", [(7.0, 0.95), (-6.0, 0.9), (0.5, 0.2), (0.0, float("nan"))])
def test_misaligned_clocks_are_refused(lag, corr):
    with pytest.raises(heart.AlignmentError) as e:
        heart.check_alignment(lag, corr)
    assert e.value.code == "misaligned"


def test_two_unrelated_recordings_do_not_align():
    t = np.arange(0, 600, 1 / 32)
    env_a = heart.motion_envelope(t, np.c_[_activity(t, 1), 0 * t, 0 * t], 0, 600)
    env_b = heart.motion_envelope(t, np.c_[_activity(t, 2), 0 * t, 0 * t], 0, 600)
    with pytest.raises(heart.AlignmentError):
        heart.check_alignment(*heart.estimate_lag(env_a, env_b))


# ── heart: the ECG reference ─────────────────────────────────────────────────

def _ecg(bpm=72.0, seconds=60.0, fs=130.0, sign=1.0, seed=0, artefact_at=None):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    beats = np.arange(0.5, seconds - 0.5, 60.0 / bpm)
    beats = beats + rng.normal(0, 0.01, len(beats))
    x = 0.3 * np.sin(2 * np.pi * 0.2 * t) + 0.03 * rng.standard_normal(len(t))
    for b in beats:
        x += sign * 1.2 * np.exp(-0.5 * ((t - b) / 0.012) ** 2)            # R
        x += 0.25 * np.exp(-0.5 * ((t - b - 0.25) / 0.05) ** 2)            # T
    if artefact_at is not None:
        m = (t >= artefact_at) & (t < artefact_at + 1.0)
        x[m] += 40 * rng.standard_normal(m.sum())
    return t, x, beats


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_r_peaks_are_found_whatever_the_ecg_s_polarity(sign):
    t, x, beats = _ecg(sign=sign)
    found, gaps = heart.ecg_beats(t, x)
    assert gaps == [] and abs(len(found) - len(beats)) <= 1
    ref, why = heart.window_reference(found, gaps, 10.0)
    assert why is None and ref == pytest.approx(72.0, abs=1.5)


def test_one_large_artefact_does_not_blind_the_detector_for_the_rest_of_the_recording():
    # The first (Pan-Tompkins) implementation found 2 beats in an hour after an artefact. Here the
    # artefact raises the threshold only within its centred 30 s averaging span (about 15 s either side).
    t, x, beats = _ecg(seconds=120.0, artefact_at=30.0)
    found, _ = heart.ecg_beats(t, x)
    after = beats[beats > 47]
    assert np.sum(found > 47) >= len(after) - 1
    assert np.sum(found < 14) >= np.sum(beats < 14) - 1


def test_a_window_with_a_missed_beat_has_no_reference():
    t, x, beats = _ecg()
    found, gaps = heart.ecg_beats(t, x)
    drop = found[(found > 12) & (found < 14)][:1]
    assert heart.window_reference(np.setdiff1d(found, drop), gaps, 10.0) == (None, "irregular_rr")


def test_a_window_over_an_ecg_gap_has_no_reference():
    t, x, _ = _ecg()
    keep = (t < 20) | (t > 22)
    found, gaps = heart.ecg_beats(t[keep], x[keep])
    assert len(gaps) == 1
    assert heart.window_reference(found, gaps, 16.0) == (None, "ecg_gap")
    assert heart.window_reference(found, gaps, 30.0)[1] is None


def test_ecg_timestamps_out_of_order_are_refused():
    t, x, _ = _ecg()
    t[100], t[101] = t[101], t[100]
    with pytest.raises(heart.AlignmentError) as e:
        heart.ecg_beats(t, x)
    assert e.value.code == "not_monotonic"


def test_activities_label_only_windows_wholly_inside_a_session():
    ev = [("walking", 100.0, 220.0), ("rest-4", 225.0, 345.0)]
    assert heart.activity_at(ev, 100.0, 108.0) == "walking"
    assert heart.activity_at(ev, 215.0, 223.0) == "transition"
    assert heart.activity_at([], 0.0, 8.0) == "unlabelled"
    assert heart.group_activity("rest-4") == "rest" and heart.group_activity("tsst-speech") == "tsst-speech"


def test_the_research_pipeline_accepts_a_25_hz_ppg_stream():
    # The Galaxy Watch's 25 Hz PPG is inside the data contract (20-512 Hz): it is evaluated, not refused.
    from care import heart_research as hr
    rec = hr.simulate(70.0, 30.0, ppg_hz=25.0, acc_hz=25.0)
    r = hr.validate_recording(rec)
    assert r.ppg.fs_hz == 25.0 and math.isfinite(hr.run(r, use_learned=False).windows[0].quality["motion_rms_g"])
