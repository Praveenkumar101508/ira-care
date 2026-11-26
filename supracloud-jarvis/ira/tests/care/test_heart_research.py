"""IRA Care Heart, research mode (care/heart_research.py, D-086).

The data contract, signal-quality rejection, inference with the classical fallback, provenance,
and the rule that a research result is never a person's reading and never reaches an alert.
Signals are simulated here, except the in-repository PPG-DaLiA excerpt (CC BY 4.0), which is
checked against the estimators called directly and against the held-out evaluation's numbers.
Each rule has a negative control: the same input without the fault, or a checker shown to
fail on a deliberately broken case.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from care import heart_estimate as he
from care import heart_learned as hl
from care import heart_research as hr

CARE = Path(hr.__file__).resolve().parent
ROOT = CARE.parents[2]
EVAL = ROOT / "docs" / "ai" / "heart" / "research_pipeline_eval.json"
EXCERPT = hr.SAMPLES_DIR / "ppg_dalia_s1_excerpt.json"
CARD_SHA = "3b0863a9a8cf81f906f5475fe54cc3878a38fbe7f7b1cc88b3c851d85f1c35dc"


def sim(**kw):
    kw.setdefault("seconds", 30.0)
    return hr.simulate(kw.pop("bpm", 72.0), **kw)


def run(d, **kw):
    return hr.run(hr.validate_recording(d), **kw)


def contract_error(d, **kw) -> str:
    with pytest.raises(hr.ContractError) as e:
        hr.validate_recording(d, **kw)
    return e.value.code


# ── the data contract ────────────────────────────────────────────────────────

def test_a_simulated_recording_meets_the_contract_and_is_windowed_8_s_every_2_s():
    rec = hr.validate_recording(sim(seconds=30.0))
    assert rec.source == "simulated" and rec.acc_scale_g == 1.0
    assert list(hr.window_starts(rec)) == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0]


@pytest.mark.parametrize("source,code", [("device", "device_source"), ("watch", "bad_source"), (None, "bad_source")])
def test_only_research_sources_are_accepted(source, code):
    d = sim()
    d["source"] = source
    assert contract_error(d) == code
    d["source"] = "research_recording"                       # negative control
    assert hr.validate_recording(d).source == "research_recording"


@pytest.mark.parametrize("stream,fs", [("ppg", 10.0), ("ppg", 2000.0), ("acc", 5.0), ("ppg", float("nan")), ("ppg", "x")])
def test_sampling_rates_out_of_range_are_refused(stream, fs):
    d = sim()
    d[stream]["fs_hz"] = fs
    assert contract_error(d) == "bad_rate"


def _with_timestamps(d, stream="ppg"):
    s = d[stream]
    s["timestamps_s"] = [i / s["fs_hz"] for i in range(len(s["samples"]))]
    return d


def test_timestamps_must_be_strictly_increasing():
    d = _with_timestamps(sim())
    hr.validate_recording(d)                                  # negative control: as generated, accepted
    ts = d["ppg"]["timestamps_s"]
    ts[100], ts[101] = ts[101], ts[100]
    assert contract_error(d) == "not_monotonic"
    d = _with_timestamps(sim())
    d["ppg"]["timestamps_s"][50] = d["ppg"]["timestamps_s"][49]
    assert contract_error(d) == "not_monotonic"


def test_timestamps_must_agree_with_the_declared_rate_and_count():
    d = _with_timestamps(sim(), "acc")
    d["acc"]["timestamps_s"] = [i / 25.0 for i in range(len(d["acc"]["samples"]))]   # 25 Hz, declared 32
    assert contract_error(d) == "rate_mismatch"
    d = _with_timestamps(sim(), "acc")
    d["acc"]["timestamps_s"] = d["acc"]["timestamps_s"][:-1]
    assert contract_error(d) == "timestamp_count"


def test_ppg_and_accelerometer_must_share_a_timebase():
    d = sim()
    d["acc"]["t0_s"] = 5.0
    assert contract_error(d) == "misaligned"
    d["acc"]["t0_s"] = 0.5                                    # negative control: half a second is one timebase
    rec = hr.validate_recording(d)
    assert hr.window_starts(rec)[0] == 0.5, "windows start where both streams have data"


def test_too_short_too_long_and_non_overlapping_recordings_are_refused():
    assert contract_error(sim(seconds=7.0)) == "too_short"
    d = sim(seconds=20.0)
    d["acc"]["samples"] = d["acc"]["samples"][:100]          # 3 s of accelerometer
    assert contract_error(d) in {"too_short", "no_overlap"}
    long = sim(seconds=hr.MAX_SECONDS + 10)
    assert contract_error(long, max_samples=None) == "too_long"
    assert hr.validate_recording(long, max_samples=None, max_seconds=None)   # an offline evaluation may lift it


def test_accelerometer_shape_and_unit_are_checked():
    d = sim()
    d["acc"]["unit"] = "furlongs"
    assert contract_error(d) == "bad_unit"
    d = sim()
    d["acc"]["samples"] = [row[:2] for row in d["acc"]["samples"]]
    assert contract_error(d) == "bad_samples"
    d = sim()
    d["ppg"]["samples"][3] = float("inf")
    assert contract_error(d) == "bad_samples"


def test_raw_e4_counts_give_exactly_the_answer_of_the_same_signal_in_g():
    d = sim(bpm=80.0, swing_hz=1.6, seconds=20.0)
    g = run(copy.deepcopy(d))
    for row in d["acc"]["samples"]:
        row[:] = [v * 64.0 for v in row]
    d["acc"]["unit"] = "e4_raw"
    raw = run(d)
    assert [(w.bpm, w.confidence) for w in raw.windows] == [(w.bpm, w.confidence) for w in g.windows]
    d["acc"]["unit"] = "g"                                    # negative control: the counts read as g are 64x the motion
    wrong = run(d)
    assert [w.quality["motion_rms_g"] for w in wrong.windows] != [w.quality["motion_rms_g"] for w in raw.windows]


# ── windows: gaps, missing samples, signal quality ───────────────────────────

def test_a_gap_makes_only_the_windows_over_it_unavailable():
    d = _with_timestamps(sim(seconds=30.0))
    clean = run(copy.deepcopy(d))
    assert all(w.status == "estimate" for w in clean.windows)        # negative control
    cut = slice(int(14 * 64), int(15 * 64))                         # one second missing at 14 s
    del d["ppg"]["samples"][cut], d["ppg"]["timestamps_s"][cut]
    res = run(d)
    gap = [w.t_start_s for w in res.windows if w.reason == "gap"]
    assert gap == [8.0, 10.0, 12.0, 14.0]
    assert all(w.bpm is None for w in res.windows if w.reason == "gap")
    assert all(w.status == "estimate" for w in res.windows if w.t_start_s not in gap)


def test_missing_samples_make_a_window_unavailable_not_a_guess():
    d = sim(seconds=20.0)
    d["ppg"]["samples"][5 * 64] = None
    res = run(d)
    assert [w.reason for w in res.windows[:3]] == ["missing_samples"] * 3
    assert res.windows[3].status == "estimate"


@pytest.mark.parametrize("fault,reason", [("flat", "flat"), ("clip", "saturated")])
def test_flat_and_clipped_signals_are_rejected(fault, reason):
    d = sim(seconds=8.0)
    x = d["ppg"]["samples"]
    d["ppg"]["samples"] = [0.25] * len(x) if fault == "flat" else [max(-0.3, min(0.3, v)) for v in x]
    w = run(d).windows[0]
    assert (w.status, w.reason, w.bpm, w.quality["usable"]) == ("unavailable", reason, None, False)
    assert w.to_dict()["reason_text"]


def test_vigorous_motion_is_rejected_by_accelerometer_energy():
    hard = run(sim(bpm=72.0, swing_hz=1.2, swing_g=1.2, seconds=8.0)).windows[0]
    assert hard.quality["motion_rms_g"] > hr.MOTION_REJECT_G
    assert (hard.status, hard.reason, hard.bpm) == ("unavailable", "motion_artefact", None)
    assert hard.classical is not None, "the estimates are still shown, marked unusable"
    walk = run(sim(bpm=72.0, swing_hz=1.6, swing_g=0.4, seconds=8.0)).windows[0]   # negative control
    # (the first walking window is usable but the learned selector is unsure of it, so it is
    # "learned_low_confidence" since D-094; what matters here is that motion did not reject it)
    assert walk.quality["motion_rms_g"] < hr.MOTION_REJECT_G and walk.quality["usable"] and walk.reason != "motion_artefact"


def test_the_fault_sample_shows_every_reason_and_never_a_number_with_one():
    res = hr.run(hr.load_sample("simulated_faults"))
    reasons = {w.reason for w in res.windows if w.status == "unavailable"}
    assert {"flat", "saturated", "missing_samples", "gap", "motion_artefact"} <= reasons
    for w in res.to_dict()["windows"]:
        assert (w["status"] == "estimate") == (w["bpm"] is not None)


def test_a_window_result_cannot_carry_a_number_and_a_reason():
    with pytest.raises(ValueError):
        hr.WindowResult(0, 0.0, "unavailable", 72.0, None, None, None, "flat", None, {}, None, None, None, 0.0)
    with pytest.raises(ValueError):
        hr.WindowResult(0, 0.0, "unavailable", None, None, None, None, None, None, {}, None, None, None, 0.0)


# ── inference, fallback and confidence ───────────────────────────────────────

def test_the_learned_answer_is_reported_with_its_probability():
    res = run(sim(bpm=75.0, swing_hz=1.6, seconds=20.0))
    assert all(w.method == "learned" and w.confidence_kind == "probability_within_5_bpm" for w in res.windows)
    assert all(abs(w.bpm - 75.0) <= 3.0 for w in res.windows)
    assert all(w.confidence >= hl.MIN_PROBABILITY for w in res.windows)


def test_without_a_model_the_classical_estimator_answers_and_says_why(tmp_path):
    res = run(sim(bpm=75.0, swing_hz=1.6, seconds=20.0), model_path=str(tmp_path / "absent.json"))
    assert res.provenance["learned_model"]["available"] is False
    assert all((w.method, w.fallback, w.confidence_kind) == ("classical", "no_model", "spectral_concentration")
               for w in res.windows if w.status == "estimate")
    assert all(w.learned is None for w in res.windows)


def test_without_an_accelerometer_the_classical_estimator_answers():
    d = sim(bpm=70.0, seconds=20.0)
    d.pop("acc")
    res = run(d)
    assert {(w.method, w.fallback) for w in res.windows} == {("classical", "no_accelerometer")}


def _low_probability_model(tmp_path) -> str:
    d = {"format": hl.FORMAT, "features": list(hl.FEATURES), "baseline": -6.0,
         "trees": [{"feature": [-1], "threshold": [0.0], "missing_left": [False], "left": [-1], "right": [-1],
                    "value": [0.0]}]}
    p = tmp_path / "low.json"
    p.write_text(json.dumps(d))
    return str(p)


def test_a_low_probability_learned_answer_is_unavailable_not_the_classical_one(tmp_path):
    """D-094: when the learned selector is unsure, the window says so; it is not answered by the
    classical estimator, even a reliable one (16 to 39 bpm out on held-out and independent data)."""
    res = run(sim(bpm=68.0, seconds=20.0), model_path=_low_probability_model(tmp_path))
    assert res.windows
    for w in res.windows:
        assert w.learned["reason"] == "low_confidence" and w.learned["bpm"] is None
        # the classical estimate was reliable and close: the old rule would have reported it
        assert w.classical["bpm"] is not None and abs(w.classical["bpm"] - 68.0) <= 2.0
        assert (w.status, w.bpm, w.method, w.fallback, w.confidence) == ("unavailable", None, None, None, None)
        assert w.reason == "learned_low_confidence"
        assert "not reported" in w.to_dict()["reason_text"]
    s = res.summary()
    assert (s["estimates"], s["classical_fallbacks"]) == (0, 0)
    assert s["unavailable_by_reason"] == {"learned_low_confidence": len(res.windows)}
    # negative control: with the shipped model the learned answer stands
    assert {w.method for w in run(sim(bpm=68.0, seconds=20.0)).windows} == {"learned"}


def test_a_learned_selector_with_no_candidate_is_unavailable_not_the_classical_one(monkeypatch):
    monkeypatch.setattr(hl, "candidate_features", lambda *a, **k: (np.zeros(0), np.zeros((0, len(hl.FEATURES)))))
    res = run(sim(bpm=70.0, seconds=12.0))
    assert res.windows
    for w in res.windows:
        assert w.classical["bpm"] is not None
        assert (w.status, w.bpm, w.method, w.reason) == ("unavailable", None, None, "learned_no_candidates")


def test_the_classical_answer_remains_only_where_the_learned_selector_cannot_run(tmp_path):
    """The classical estimate, labelled as a fallback with why, is kept where there is no model or no
    accelerometer; wherever the learned selector ran, no window is answered by the classical one."""
    no_model = run(sim(bpm=72.0, seconds=16.0), model_path=str(tmp_path / "absent.json"))
    d = sim(bpm=72.0, seconds=16.0)
    d.pop("acc")
    no_acc = run(d)
    for res, why in ((no_model, "no_model"), (no_acc, "no_accelerometer")):
        assert res.windows and all(w.status == "estimate" for w in res.windows)
        assert {(w.method, w.fallback, w.confidence_kind) for w in res.windows} == {
            ("classical", why, "spectral_concentration")}
    low = run(sim(bpm=72.0, seconds=16.0), model_path=_low_probability_model(tmp_path))
    assert not any(w.method == "classical" for w in low.windows)


def test_when_neither_is_reliable_the_window_is_unavailable(tmp_path):
    rng = np.random.default_rng(5)
    d = sim(seconds=8.0)
    d["ppg"]["samples"] = [float(v) for v in rng.standard_normal(len(d["ppg"]["samples"]))]
    w = run(d, model_path=_low_probability_model(tmp_path)).windows[0]
    assert (w.status, w.bpm, w.reason) == ("unavailable", None, "learned_low_confidence")
    # without a model the classical estimator decides alone, and its own reason is kept
    w = run(d, model_path=str(tmp_path / "absent.json")).windows[0]
    assert (w.status, w.bpm, w.reason) == ("unavailable", None, "low_confidence")


def test_the_pipeline_is_the_trackers_on_the_dataset_windowing_not_a_copy_of_them():
    d = sim(bpm=90.0, swing_hz=2.6, seconds=24.0)
    res = run(d)
    ppg = np.array(d["ppg"]["samples"])
    acc = np.array(d["acc"]["samples"])
    lt = hl.LearnedHeartTracker(hl.load_model())
    for k, w in enumerate(res.windows):
        r = lt.update(ppg[128 * k:128 * k + 512], acc[64 * k:64 * k + 256])
        assert (w.learned["candidate_bpm"], w.learned["probability"]) == (r.candidate_bpm, round(r.confidence, 4))
        assert w.classical["candidate_bpm"] == r.classical.candidate_bpm


# ── provenance and the research-only rule ────────────────────────────────────

def test_every_result_carries_its_provenance():
    d = hr.run(hr.load_sample("simulated_rest")).to_dict()
    m = d["provenance"]["learned_model"]
    assert m["sha256"] == hashlib.sha256(hl.DEFAULT_MODEL_PATH.read_bytes()).hexdigest() == CARD_SHA
    assert m["version"] == f"heart_selector@{CARD_SHA[:12]}" and m["format"] == hl.FORMAT
    assert m["dataset"] == "PPG-DaLiA" and m["dataset_licence"] == "CC BY 4.0" and "15 subjects" in m["trained_on"]
    assert d["provenance"]["pipeline"] == hr.PIPELINE_VERSION
    assert d["provenance"]["classical"]["min_confidence"] == he.MIN_CONFIDENCE
    assert d["provenance"]["recording"] == {"source": "simulated", "sample": "simulated_rest",
                                            "reference_kind": "the simulated pulse rate"}
    assert (d["source"], d["is_measurement"], d["may_alert"], d["stored"]) == ("simulated", False, False, False)
    assert d["notice"] == hr.NOTICE and "not a measurement" in d["notice"]
    assert all(w["processing_ms"] >= 0 for w in d["windows"]) and d["resources"]["per_window_ms_mean"] > 0


def test_a_research_result_cannot_be_labelled_a_device_reading_or_an_alerting_one():
    good = hr.run(hr.load_sample("simulated_rest"))
    for change in ({"source": "device"}, {"source": "watch"}, {"is_measurement": True}, {"may_alert": True}):
        with pytest.raises(ValueError):
            hr.ResearchResult(**{**good.__dict__, **change})
    assert "device" in hr.SOURCES and "device" not in hr.RESEARCH_SOURCES
    rec = hr.validate_recording(sim())
    forged = hr.Recording("device", rec.ppg, rec.acc, rec.acc_scale_g)
    with pytest.raises(hr.ContractError):
        hr.run(forged)


def test_a_research_result_is_not_shaped_like_a_reading():
    """The readings the watch sends are {kind, value, unit, recorded_at}; nothing in a research
    result has that shape, so it cannot be passed off as one by accident."""
    from care import schemas
    d = hr.run(hr.load_sample("simulated_rest")).to_dict()
    for w in d["windows"]:
        assert "kind" not in w and "value" not in w and "recorded_at" not in w
        with pytest.raises(Exception):
            schemas.Reading(**w)
    assert d["kind"] == "heart_research_result" and d["kind"] not in schemas.VITAL_KINDS


# ── never reaches an alert ───────────────────────────────────────────────────

ALERT_PATH = ("continuous", "monitoring", "askfirst", "escalation", "sos", "repository", "worker", "falls",
              "delivery", "push", "checkin_cycle", "checkins", "followup", "reports", "overview", "history")


def care_imports(care_dir: Path) -> dict[str, set[str]]:
    """Module -> the care modules it imports (relative or `care.` absolute imports)."""
    graph: dict[str, set[str]] = {}
    for p in care_dir.glob("*.py"):
        deps: set[str] = set()
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level == 1 and not mod:
                    deps.update(a.name for a in node.names)
                elif node.level == 1:
                    deps.add(mod.split(".")[0])
                elif mod == "care":
                    deps.update(a.name for a in node.names)
                elif mod.startswith("care."):
                    deps.add(mod.split(".")[1])
            elif isinstance(node, ast.Import):
                deps.update(a.name.split(".")[1] for a in node.names if a.name.startswith("care."))
        graph[p.stem] = deps
    return graph


def research_reachable_from(graph: dict[str, set[str]], roots) -> list[str]:
    bad = []
    for root in roots:
        seen, todo = set(), [root]
        while todo:
            m = todo.pop()
            if m in seen:
                continue
            seen.add(m)
            todo.extend(graph.get(m, ()))
        if "heart_research" in seen:
            bad.append(root)
    return bad


def test_no_alert_path_module_can_reach_the_research_pipeline():
    graph = care_imports(CARE)
    assert set(ALERT_PATH) <= set(graph), "the alert path modules are all present"
    assert research_reachable_from(graph, ALERT_PATH) == []
    users = sorted(m for m, deps in graph.items() if "heart_research" in deps)
    assert users == ["routes"], f"only the research route may use the research pipeline: {users}"


def test_negative_control_the_reachability_check_catches_an_indirect_import(tmp_path):
    (tmp_path / "continuous.py").write_text("from . import helper\n")
    (tmp_path / "helper.py").write_text("from care import heart_research\n")
    (tmp_path / "heart_research.py").write_text("")
    assert research_reachable_from(care_imports(tmp_path), ["continuous"]) == ["continuous"]


def test_the_research_pipeline_touches_no_database_and_no_person():
    tree = ast.parse((CARE / "heart_research.py").read_text(encoding="utf-8"))
    tree.body = tree.body[1:]                     # the code, without the module docstring that explains the rule
    src = ast.unparse(tree)
    imported = care_imports(CARE)["heart_research"]
    assert imported == {"heart_estimate", "heart_learned"}
    for word in ("workspace_connection", "asyncpg", "INSERT", "care_vitals", "person_id", "askfirst", "escalat"):
        assert word not in src, word


def test_the_research_route_opens_no_connection_and_takes_no_person():
    from care import routes
    tree = ast.parse((CARE / "routes.py").read_text(encoding="utf-8"))
    fns = {n.name: ast.unparse(n) for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    for name in ("research_heart_run", "research_heart_samples"):
        body = fns[name]
        assert "ensure_can_manage(p)" in body
        for word in ("workspace_connection", "repo.", "audit.", "person_id", "askfirst", "sos."):
            assert word not in body, (name, word)
    paths = [r.path for r in routes.router.routes if "research" in getattr(r, "path", "")]
    assert paths and all("{" not in p for p in paths), paths


# ── the PPG-DaLiA excerpt ────────────────────────────────────────────────────

def test_the_excerpt_is_small_attributed_and_labelled_as_research_data():
    raw = EXCERPT.read_bytes()
    assert len(raw) < 1024 * 1024
    d = json.loads(raw)
    assert d["format"] == hr.SAMPLE_FORMAT and d["source"] == "research_recording"
    assert d["licence"] == "CC BY 4.0" and "Reiss" in d["attribution"] and "creativecommons.org/licenses/by/4.0" in d["attribution"]
    assert d["changes"] and d["excerpt"]["subject"] == "S1"
    names = {s["name"]: s for s in hr.list_samples()}
    assert names["ppg_dalia_s1_excerpt"]["source"] == "research_recording"
    assert {n for n, s in names.items() if s["source"] == "simulated"} == set(hr.SIMULATED)


def test_the_excerpt_is_run_exactly_as_the_estimators_run_it_directly():
    rec = hr.load_sample("ppg_dalia_s1_excerpt")
    assert rec.acc.unit == "e4_raw" and rec.acc_scale_g == 1 / 64
    res = hr.run(rec)
    assert len(res.windows) == 147 == len(rec.reference_bpm)
    lt = hl.LearnedHeartTracker(hl.load_model(), acc_scale_g=1 / 64)
    ppg, acc = rec.ppg.samples, rec.acc.samples
    for k, w in enumerate(res.windows):
        r = lt.update(ppg[128 * k:128 * k + 512], acc[64 * k:64 * k + 256])
        assert w.classical["candidate_bpm"] == r.classical.candidate_bpm
        assert w.learned["candidate_bpm"] == r.candidate_bpm


def test_the_excerpt_reproduces_the_held_out_evaluation():
    """The numbers the evaluation script recorded for the excerpt with the shipped model."""
    want = json.loads(EVAL.read_text())["excerpt"]["runs"]["shipped_model_trained_on_all_15"]
    got = hr.run(hr.load_sample("ppg_dalia_s1_excerpt")).summary()["against_reference"]
    assert got["classical_all_windows"] == want["classical_all_windows"]
    assert got["learned_all_windows"] == want["learned_all_windows"]
    assert got["reported"] == want["reported"]
    assert got["reported"] == want["reported_learned"]       # D-094: only learned answers are reported
    assert want["reported_classical_fallback"]["windows"] == 0


def test_the_excerpt_reports_no_classical_answer_where_the_learned_selector_was_unsure():
    """On the real excerpt, the windows the old rule answered with the classical estimate are now
    unavailable (learned_low_confidence); their number is the evaluation's recorded count."""
    want = json.loads(EVAL.read_text())["excerpt"]["runs"]["shipped_model_trained_on_all_15"]
    res = hr.run(hr.load_sample("ppg_dalia_s1_excerpt"))
    assert not any(w.method == "classical" for w in res.windows)
    held_back = [w for w in res.windows if w.reason == "learned_low_confidence" and w.classical["bpm"] is not None]
    assert len(held_back) == want["previous_rule_classical_fallback"]["windows"] > 0
    assert all(w.bpm is None and w.status == "unavailable" for w in held_back)


def test_negative_control_without_the_accelerometer_the_excerpt_gives_different_answers():
    """Withholding the accelerometer must switch the learned path off and change the classical
    answers; if it did not, the excerpt tests above would not be exercising the motion path.
    (On this excerpt the classical estimator without the accelerometer happens to score 10.55
    against 12.65 bpm with it: a 5-minute excerpt is not the 15-subject evaluation.)"""
    rec = hr.load_sample("ppg_dalia_s1_excerpt")
    with_acc = hr.run(rec).summary()["against_reference"]
    no_acc = hr.run(hr.Recording(rec.source, rec.ppg, None, 1.0, name=rec.name, reference_bpm=rec.reference_bpm,
                                 info=rec.info)).summary()["against_reference"]
    assert with_acc["learned_all_windows"]["windows"] == 147 and no_acc["learned_all_windows"]["windows"] == 0
    assert no_acc["classical_all_windows"]["mae_bpm"] != with_acc["classical_all_windows"]["mae_bpm"]
