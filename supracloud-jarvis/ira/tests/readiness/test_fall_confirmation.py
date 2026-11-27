"""The fall confirmation stage: the Python mirror (scripts/eval/fall_confirmation.py) and the watch's
FallConfirmation.kt agree on every constant; the decisions it makes on the synthetic scenarios are
the ones the investigation records; and it never suppresses a still wrist.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
EVAL = ROOT / "scripts" / "eval"
KOTLIN = ROOT / "watch" / "app" / "src" / "main" / "java" / "care" / "ira" / "watch" / "sensors" / "FallConfirmation.kt"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EVAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # dataclasses look the module up by name
    if str(EVAL) not in sys.path:
        sys.path.insert(0, str(EVAL))  # fall_synthetic_eval imports its siblings by name
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fc():
    return _load("fall_confirmation")


@pytest.fixture(scope="module")
def scenarios():
    return _load("fall_synthetic_scenarios")


def _kotlin_const(name: str) -> float:
    m = re.search(rf"const val {name} = ([0-9_.]+)L?", KOTLIN.read_text())
    assert m, f"{name} not in FallConfirmation.kt"
    return float(m.group(1).replace("_", ""))


def test_the_watch_and_the_mirror_use_the_same_constants(fc):
    assert _kotlin_const("PRE_FROM_MS") == fc.PRE_FROM_S * 1000
    assert _kotlin_const("PRE_TO_MS") == fc.PRE_TO_S * 1000
    assert _kotlin_const("POST_FROM_MS") == fc.POST_FROM_S * 1000
    assert _kotlin_const("POST_TO_MS") == fc.POST_TO_S * 1000
    assert _kotlin_const("STILL_SD_G") == fc.STILL_SD_G
    assert _kotlin_const("BUSY_SD_G") == fc.BUSY_SD_G
    assert _kotlin_const("SAME_TURN_DEG") == fc.SAME_TURN_DEG
    assert _kotlin_const("REPEAT_TURN_DEG") == fc.REPEAT_TURN_DEG
    assert _kotlin_const("REPEAT_MIN") == fc.REPEAT_MIN
    assert _kotlin_const("REPEAT_WINDOW_MS") == fc.REPEAT_WINDOW_S * 1000
    assert _kotlin_const("REPEAT_GAP_MS") == fc.REPEAT_GAP_S * 1000
    assert _kotlin_const("IMPACT_G") == fc.IMPACT_G
    assert _kotlin_const("CLIP_G") == fc.CLIP_G


def test_the_stage_is_off_by_default_on_the_watch():
    dto = (ROOT / "watch" / "app" / "src" / "main" / "java" / "care" / "ira" / "watch" / "api" / "CareApi.kt").read_text()
    assert re.search(r'@SerialName\("fall_confirm_before_asking"\) val confirmBeforeAsking: Boolean = false', dto)
    service = (ROOT / "watch" / "app" / "src" / "main" / "java" / "care" / "ira" / "watch" / "service" / "CareForegroundService.kt").read_text()
    assert "if (deps.fallSettings.confirmBeforeAsking) pending = pending ?: event else onSuspectedFall(event)" in service


def _recording(rng, before, after, peak=4.0, pre_s=3.0, post_s=6.0, post_amp=0.0, post_noise=0.02, hz=50):
    """`pre_s` of a wrist reading gravity `before`, an impact of `peak`, then `post_s` of gravity `after`
    with a sinusoid of `post_amp` g along x."""
    n_pre, n_post = int(pre_s * hz), int(post_s * hz)
    pre = np.asarray(before)[None, :] + rng.normal(0, 0.02, (n_pre, 3))
    hit = np.vstack([before, np.asarray(after) + np.asarray(after) * peak, np.asarray(after) * (1 + 0.55 * peak), after])
    t = np.arange(n_post) / hz
    post = np.asarray(after)[None, :] + rng.normal(0, post_noise, (n_post, 3))
    # the hand's movement along the gravity axis, which is what changes |a| (a swing across it barely does)
    post += (np.asarray(after)[None, :] * (post_amp * np.sin(2 * np.pi * t / 0.7))[:, None])
    xyz = np.vstack([pre, hit, post])
    return np.arange(len(xyz)) / hz, xyz, (n_pre + 1) / hz


def test_a_fall_that_turns_the_wrist_and_lies_still_is_confirmed(fc):
    rng = np.random.default_rng(1)
    t, xyz, impact = _recording(rng, [0, 0, 1.0], [np.sin(np.radians(100)), 0, np.cos(np.radians(100))])   # 100 degrees from z
    d = fc.decide(t, xyz, impact)
    assert d.confirm and d.reason == "still afterwards" and d.orientation_change_deg > 60


def test_a_still_wrist_is_confirmed_whatever_the_orientation_did(fc):
    rng = np.random.default_rng(2)
    t, xyz, impact = _recording(rng, [0, 0, 1.0], [0, 0, 1.0])
    d = fc.decide(t, xyz, impact)
    assert d.confirm and d.reason == "still afterwards" and d.orientation_change_deg < fc.SAME_TURN_DEG


def test_a_slap_with_the_hand_carrying_on_is_suppressed_but_a_gentle_hand_is_asked(fc):
    rng = np.random.default_rng(3)
    t, xyz, impact = _recording(rng, [0, 0, 1.0], [0, 0, 1.0], post_amp=0.8)
    assert fc.decide(t, xyz, impact).reason == "same orientation, hand carried on"
    t, xyz, impact = _recording(rng, [0, 0, 1.0], [0, 0, 1.0], post_amp=0.35)
    assert fc.decide(t, xyz, impact).confirm, "between still and busy the stage asks"


def test_repeated_impacts_are_counted_and_a_burst_with_the_same_orientation_is_suppressed(fc):
    hz = 50
    rng = np.random.default_rng(4)
    xyz = np.asarray([0, 0, 1.0])[None, :] + rng.normal(0, 0.02, (12 * hz, 3))
    for k in range(6):                      # a clap every 0.4 s from t = 3 s
        i = int((3.0 + 0.4 * k) * hz)
        xyz[i] += [0, 3.0, 0]
        xyz[i + 1] += [0, 1.6, 0]
    t = np.arange(len(xyz)) / hz
    xyz[int(5.5 * hz):, 2] += 0.6 * np.sin(2 * np.pi * t[int(5.5 * hz):] / 0.7)   # along gravity: |a| changes
    d = fc.decide(t, xyz, 3.8)
    assert d.impacts_in_window >= fc.REPEAT_MIN and not d.confirm
    assert d.reason == "repeated impacts, same orientation, moving"


def test_a_saturated_axis_is_flagged_only(fc):
    rng = np.random.default_rng(5)
    t, xyz, impact = _recording(rng, [0, 0, 1.0], [0, 0, 1.0], peak=16.0)
    d = fc.decide(t, xyz, impact)
    assert d.clipped and d.confirm


def test_the_synthetic_scenarios_are_deterministic_and_labelled(scenarios):
    a = scenarios.generate(7, 2)
    b = scenarios.generate(7, 2)
    assert [s["id"] for s in a] == [s["id"] for s in b]
    assert all(np.array_equal(x["xyz"], y["xyz"]) for x, y in zip(a, b))
    assert len(a) == 2 * (len(scenarios.FALL_KINDS) + len(scenarios.NOT_FALL_KINDS))
    assert {s["label"] for s in a} == {"fall", "not_fall"}
    assert all(abs(float(np.linalg.norm(s["xyz"][:50], axis=1).mean()) - 1.0) < 0.1 for s in a), "every recording starts with a still, worn wrist"


def test_on_the_synthetic_set_no_caught_fall_is_lost_and_alarms_fall():
    """The shipped pipeline through LiteRT, with and without the stage. Needs the runtime."""
    pytest.importorskip("ai_edge_litert")
    ev = _load("fall_synthetic_eval")
    spec, infer, _ = ev.load_shipped_litert()
    scen = _load("fall_synthetic_scenarios").generate(7, 4)
    caught_before = caught_after = alarms_before = alarms_after = 0
    for s in scen:
        raised = ev.run_pipeline(s["xyz"], spec, infer)
        judged = ev.confirm_all(s["xyz"], raised)
        kept = [j for j in judged if j["confirmed"]]
        if s["label"] == "fall":
            caught_before += bool(raised)
            caught_after += bool(kept)
        else:
            alarms_before += len(raised)
            alarms_after += len(kept)
    assert caught_after == caught_before, "the stage lost a caught fall"
    assert alarms_after < alarms_before, "the stage removed no false alarm"
