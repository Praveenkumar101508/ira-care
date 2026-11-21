"""P17-03 — the benchmark ratchet, and which of its four controls are real.

A number recorded once stops being true quietly, so P17-02's run is gated. The
gate has four controls and they are NOT equally strong; these tests pin that
distinction, because a reader who treats the latency ceiling as an SLO would be
quoting a backstop as a guarantee.

  1. the inspected-path floor    deterministic — the real gate
  2. the measurement floor       deterministic — the real gate
  3. the conditions guard        deterministic — the real gate
  4. the latency ceiling         generous, order-of-magnitude only
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[4]
GATE = REPO / "scripts" / "check_benchmark_ratchet.py"
BASELINE = REPO / "benchmarks" / ".benchmark-baseline.json"

if not GATE.is_file():
    pytest.skip(f"{GATE} not found", allow_module_level=True)


def _module():
    sys.path.insert(0, str(REPO / "scripts"))
    return importlib.import_module("check_benchmark_ratchet")


@pytest.fixture()
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.fixture()
def run(baseline) -> dict:
    """A synthetic run that the gate ACCEPTS, built from the real baseline.

    Every test below mutates one thing about it. Built from the baseline rather
    than hand-written so that a baseline change cannot leave these tests
    asserting against a shape the gate no longer sees.
    """
    return {
        "paths_driven": baseline["min_paths_driven"],
        "throttled": baseline["throttled_paths"],
        "conditions": {
            **baseline["conditions"],
            "host": "test", "platform": "test", "python": "3.12",
            "cpu_count": 8, "ci": True,
        },
        "results": [
            {"path": p, "latency_ms_p95": 1.0,
             "measures": ("the rate limiter refusing, NOT the path"
                          if p in baseline["throttled_paths"]
                          else "the path doing its work")}
            for p in baseline["max_p95_ms"]
        ],
    }


def test_the_baseline_run_is_accepted(run, baseline):
    """First, because every test below is a rejection.

    A gate that only ever rejects is a gate nobody has watched accept anything,
    and its rejections cannot be attributed to the property they name.
    """
    assert _module().check(run, baseline) == []


# ── 1. The inspected-path floor: the control that matters most ─────────────


def test_a_run_that_drove_fewer_paths_fails_however_fast_it_was(run, baseline):
    """Recurring failure class 34, in a unit of milliseconds.

    A benchmark that got faster by measuring less is the single most likely way
    this phase's work stops meaning anything — and it looks like an improvement
    in every table.
    """
    run["paths_driven"] -= 1
    problems = _module().check(run, baseline)
    assert any("below the floor" in p for p in problems), problems
    assert any("FEWER PATHS" in p for p in problems)


def test_driving_more_paths_also_fails_and_demands_the_floor_be_raised(run, baseline):
    """The ratchet's other direction.

    A ratchet that is not tightened is a threshold, and a threshold drifts back.
    Same rule the mypy and ESLint ratchets carry.
    """
    run["paths_driven"] += 1
    problems = _module().check(run, baseline)
    assert any("ABOVE the floor" in p for p in problems), problems
    assert any("Raise min_paths_driven" in p for p in problems)


# ── 2. The measurement floor ───────────────────────────────────────────────


def test_a_path_that_did_not_do_its_work_fails(run, baseline):
    target = next(r for r in run["results"]
                  if r["path"] not in baseline["throttled_paths"])
    target["measures"] = "the rate limiter refusing, NOT the path"
    problems = _module().check(run, baseline)
    assert any("not a measurement of the path" in p for p in problems), problems


def test_a_declared_throttled_path_is_exempt_and_others_are_not(run, baseline):
    """The exemption exists and is NARROW.

    Both halves asserted: a declared-throttled path may report a non-measurement
    without failing the run, and nothing else may.
    """
    assert baseline["throttled_paths"], (
        "no path is declared throttled, so the exemption below is untested")
    assert _module().check(run, baseline) == []


def test_a_path_with_no_verdict_fails(run, baseline):
    run["results"][0].pop("measures")
    assert any("does not say what it measured" in p
               for p in _module().check(run, baseline))


# ── 3. The conditions guard ────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["host", "platform", "python", "cpu_count"])
def test_an_unrecorded_condition_fails(run, baseline, field):
    run["conditions"][field] = None
    assert any(f"conditions.{field} is unrecorded" in p
               for p in _module().check(run, baseline))


def test_a_run_at_a_different_concurrency_is_not_a_comparison(run, baseline):
    """Two numbers taken under different conditions are not a comparison.

    They look exactly like one, which is the whole reason this field is pinned.
    """
    run["conditions"]["concurrency"] = baseline["conditions"]["concurrency"] * 2
    problems = _module().check(run, baseline)
    assert any("two different measurements" in p for p in problems), problems


# ── 4. The latency ceiling, and its stated limits ──────────────────────────


def test_a_p95_above_its_ceiling_fails(run, baseline):
    path = next(p for p in baseline["max_p95_ms"]
                if p not in baseline["throttled_paths"])
    target = next(r for r in run["results"] if r["path"] == path)
    target["latency_ms_p95"] = baseline["max_p95_ms"][path] + 1
    assert any("above the ceiling" in p
               for p in _module().check(run, baseline))


def test_a_path_with_a_ceiling_that_was_not_driven_fails(run, baseline):
    """A ceiling over a path nobody ran is a green tick with nothing behind it."""
    dropped = next(r for r in reversed(run["results"])
                   if r["path"] not in baseline["throttled_paths"])
    run["results"].remove(dropped)
    run["paths_driven"] -= 1
    problems = _module().check(run, baseline)
    assert any(f"{dropped['path']} has a ceiling but was not driven" in p
               for p in problems), problems


def test_the_ceilings_are_generous_and_the_file_says_why(baseline):
    """The honesty this control depends on.

    A percent-level ratchet over CI-runner latency would flap, and a gate that
    flaps gets switched off — FINDING AB's shape, met twice by this programme.
    So the ceiling is a backstop and the baseline must say so in as many words,
    where somebody reading the number will meet it.
    """
    why = (baseline.get("why_the_ceilings_are_generous", "")).lower()
    assert "not a performance slo" in why, (
        "the baseline does not warn that its ceilings are not an SLO, so the "
        "next person to quote one will quote it as a guarantee")
    assert "order-of-magnitude" in why
    assert min(baseline["max_p95_ms"].values()) >= 30_000, (
        "a ceiling tighter than the floor would flap on a shared runner")


# ── The gate's own failure semantics ───────────────────────────────────────


def test_a_missing_results_file_is_refused_rather_than_passed(tmp_path):
    """A gate with no input inspected NOTHING, which is UNKNOWN, never clean.

    Phase 13's rule, item S, and the single most likely way this gate stops
    meaning anything: the benchmark step changes its output path, this reads an
    absent file, and a green tick appears over no measurement at all.
    """
    import subprocess
    done = subprocess.run(
        [sys.executable, str(GATE), str(tmp_path / "absent.json")],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert done.returncode == 2
    assert "inspected NOTHING" in done.stderr
    assert "never clean" in done.stderr


def test_the_seeder_refuses_a_run_the_gate_would_reject(tmp_path, run, baseline):
    """A baseline taken from a bad run is a baseline that accepts bad runs.

    Not hypothetical. The first run this gate was pointed at had 80 transport
    timeouts on /readyz under contention, and the first seeder INFERRED
    "throttled" from "was not a measurement" — so it would have written /readyz
    into the exemption list permanently, retiring the one path whose degradation
    this gate most needs to see.
    """
    import subprocess
    target = next(r for r in run["results"]
                  if r["path"] not in baseline["throttled_paths"])
    target["measures"] = "the server not answering at all"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(run), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(GATE), "--update", str(bad),
         "--baseline", str(tmp_path / "would-be.json")],
        capture_output=True, text=True, check=False, cwd=REPO)
    assert done.returncode == 2
    assert "REFUSED to seed" in done.stderr
    assert not (tmp_path / "would-be.json").exists(), (
        "the seeder wrote a baseline from a run it refused")


def test_a_throttled_paths_latency_is_not_gated(run, baseline):
    """Exempt from the ceiling as well as the measurement floor, same reason.

    Its latency figure is not a measurement of the path by construction. CI
    recorded /auth/token at a p95 of 30 033 ms — the client timeout, waiting on
    a limiter doing its job — against a 30 000 ms ceiling. Gating that would
    have made the gate flap on a quantity nobody claims means anything, and a
    gate that flaps gets switched off.
    """
    throttled = baseline["throttled_paths"]
    assert throttled, "nothing is declared throttled, so this asserts nothing"
    target = next(r for r in run["results"] if r["path"] in throttled)
    target["latency_ms_p95"] = 999_999.0
    assert _module().check(run, baseline) == []
