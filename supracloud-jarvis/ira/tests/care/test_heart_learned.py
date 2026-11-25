"""IRA Care Heart, learned path (care/heart_learned.py).

The shipped model file is plain JSON (`ira-gbt-v1`), checked before use; with no valid
model the tracker answers with the classical estimator. Signals are synthetic; the only
dataset-derived input is the fixture of feature rows and the trainer's probabilities for
them, written by scripts/training/heart_ppg_dalia.py when the model was exported.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from care import heart_learned as hl
from care.heart_estimate import HeartTracker, naive_spectral_peak_bpm
from tests.care.test_heart_estimate import arm_swing, motion_artefact, pulse

FIXTURE = Path(__file__).parent / "fixtures" / "heart_selector_rows.json"


@pytest.fixture(scope="module")
def model():
    return hl.load_model(hl.DEFAULT_MODEL_PATH)


@pytest.fixture(scope="module")
def model_dict():
    return json.loads(hl.DEFAULT_MODEL_PATH.read_text())


def _write(tmp_path, d, name="m.json"):
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


# ── the shipped file ─────────────────────────────────────────────────────────

def test_the_shipped_model_is_plain_json_in_the_checked_format(model, model_dict):
    assert model_dict["format"] == hl.FORMAT
    assert model_dict["features"] == hl.FEATURES
    assert 1 <= len(model.trees) <= model_dict["params"]["max_iter"]
    assert hl.DEFAULT_MODEL_PATH.stat().st_size <= hl.MAX_MODEL_BYTES
    assert not list(hl.DEFAULT_MODEL_PATH.parent.glob("*.pkl")), "no pickles next to the model"


def test_inference_matches_the_trainer_on_recorded_rows(model):
    fx = json.loads(FIXTURE.read_text())
    import hashlib
    assert hashlib.sha256(hl.DEFAULT_MODEL_PATH.read_bytes()).hexdigest() == fx["model_sha256"], \
        "fixture and model must come from the same export"
    X = np.array([[np.nan if v is None else v for v in row] for row in fx["rows"]], dtype=np.float64)
    assert np.isnan(X).any(), "the fixture must exercise missing-value routing"
    got = model.predict_proba(X)
    assert np.max(np.abs(got - np.array(fx["probability"]))) < 1e-9


def test_negative_control_a_perturbed_model_no_longer_matches(model_dict):
    d = copy.deepcopy(model_dict)
    d["baseline"] += 0.5
    fx = json.loads(FIXTURE.read_text())
    X = np.array([[np.nan if v is None else v for v in row] for row in fx["rows"]], dtype=np.float64)
    got = hl.TreeEnsemble.from_dict(d).predict_proba(X)
    assert np.max(np.abs(got - np.array(fx["probability"]))) > 1e-3


# ── the loader refuses anything else ────────────────────────────────────────

def _tiny():
    return {"format": hl.FORMAT, "features": list(hl.FEATURES), "baseline": 0.0,
            "trees": [{"feature": [0, -1, -1], "threshold": [80.0, 0.0, 0.0], "missing_left": [True, False, False],
                       "left": [1, -1, -1], "right": [2, -1, -1], "value": [0.0, -1.0, 1.0]}]}


def test_a_tiny_valid_model_loads_and_routes_missing_values(tmp_path):
    m = hl.load_model(_write(tmp_path, _tiny()))
    row = np.zeros((3, len(hl.FEATURES)))
    row[0, 0], row[1, 0], row[2, 0] = 70.0, 90.0, np.nan
    np.testing.assert_allclose(m.raw_score(row), [-1.0, 1.0, -1.0])


@pytest.mark.parametrize("breakage", [
    lambda d: d.update(format="pickle"),
    lambda d: d.update(features=list(reversed(hl.FEATURES))),
    lambda d: d.update(baseline="0"),
    lambda d: d.update(baseline=True),
    lambda d: d.update(trees=[]),
    lambda d: d["trees"][0].update(left=[0, -1, -1]),             # a loop: child is the node itself
    lambda d: d["trees"][0].update(right=[7, -1, -1]),            # child outside the tree
    lambda d: d["trees"][0].update(feature=[19, -1, -1]),         # feature index out of range
    lambda d: d["trees"][0].update(feature=[-2, -1, -1]),
    lambda d: d["trees"][0].update(threshold=[float("nan"), 0.0, 0.0]),
    lambda d: d["trees"][0].update(value=[0.0, float("inf"), 1.0]),
    lambda d: d["trees"][0].update(value=[0.0, 1.0]),             # arrays of different lengths
    lambda d: d["trees"][0].update(feature=[True, -1, -1]),       # a bool is not an index
    lambda d: d["trees"][0].update(missing_left=[1, 0, 0]),
    lambda d: d["trees"][0].update(left=[1, 2, -1]),              # a leaf with a child
    lambda d: d["trees"][0].pop("value"),
    lambda d: d.update(trees=[d["trees"][0]] * (hl.MAX_TREES + 1)),
])
def test_malformed_models_are_refused(tmp_path, breakage):
    d = _tiny()
    breakage(d)
    with pytest.raises(hl.ModelError):
        hl.load_model(_write(tmp_path, d))


def test_not_json_and_oversized_files_are_refused(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\x80\x04\x95 not json")          # a pickle header is not a model
    with pytest.raises(hl.ModelError):
        hl.load_model(bad)
    p = _write(tmp_path, _tiny())
    monkeypatch.setattr(hl, "MAX_MODEL_BYTES", p.stat().st_size - 1)
    with pytest.raises(hl.ModelError, match="over the"):
        hl.load_model(p)


def test_wrong_feature_width_is_an_error(model):
    with pytest.raises(ValueError):
        model.predict_proba(np.zeros((2, len(hl.FEATURES) - 1)))


# ── fallback to the classical estimator ──────────────────────────────────────

def _stream(n=6, bpm=72.0, swing_hz=1.6):
    for i in range(n):
        yield pulse(bpm, seed=i) + motion_artefact(swing_hz), arm_swing(swing_hz, seed=100 + i)


@pytest.mark.parametrize("make_path", [
    lambda tmp: tmp / "absent.json",
    lambda tmp: (tmp / "broken.json").write_text("{") and tmp / "broken.json",
])
def test_no_valid_model_means_the_classical_answer_exactly(tmp_path, make_path):
    lt = hl.LearnedHeartTracker(make_path(tmp_path))
    assert not lt.uses_learned_model
    ref = HeartTracker()
    for b, a in _stream():
        got, want = lt.update(b, a), ref.update(b, a)
        assert got.method == "classical"
        assert (got.bpm, got.confidence, got.reason) == (want.bpm, want.confidence, want.reason)


def test_without_an_accelerometer_the_classical_answer_is_used(model):
    lt = hl.LearnedHeartTracker(model)
    r = lt.update(pulse(72.0))
    assert r.method == "classical" and r.reliable and abs(r.bpm - 72.0) <= 2.0


def test_refused_input_is_refused_by_the_learned_path_too(model):
    lt = hl.LearnedHeartTracker(model)
    r = lt.update(np.zeros(512), arm_swing(1.0))
    assert r.bpm is None and r.reason == "flat" and r.method == "learned"


# ── behaviour on synthetic windows ───────────────────────────────────────────

@pytest.mark.parametrize("swing_hz", [0.9, 1.6, 2.6])
def test_the_learned_path_finds_the_pulse_under_arm_swing(model, swing_hz):
    lt = hl.LearnedHeartTracker(model)
    answers = [lt.update(b, a) for b, a in _stream(6, 72.0, swing_hz)]
    assert all(r.method == "learned" for r in answers)
    picked = [r.candidate_bpm for r in answers]
    assert all(abs(p - 72.0) <= 3.0 for p in picked), picked
    # negative control: the naive peak follows the swing, more than 10 bpm off
    b, _ = next(_stream(1, 72.0, swing_hz))
    assert abs(naive_spectral_peak_bpm(b) - 72.0) > 10.0


def test_a_low_probability_is_no_estimate_not_a_guess(model):
    lt = hl.LearnedHeartTracker(model, min_probability=1.01)
    r = lt.update(*next(_stream(1)))
    assert r.bpm is None and r.reason == "low_confidence" and r.candidate_bpm is not None


def test_training_and_inference_share_one_feature_function():
    script = (Path(__file__).resolve().parents[4] / "scripts" / "training" / "heart_ppg_dalia.py").read_text()
    assert "hl.candidate_features(" in script and "hl.FEATURES" in script
    assert "def _local_maxima" not in script, "the features must not be re-implemented in the script"


def test_nothing_raises_an_alert_on_the_learned_estimate():
    care = Path(hl.__file__).parent
    users = [p.name for p in care.rglob("*.py") if p.name != "heart_learned.py" and "heart_learned" in p.read_text()]
    assert users == [], f"wellbeing trend only; no alert path may consume it: {users}"
