"""The wall-clock soak harness judges a run honestly.

scripts/soak_wallclock.py drives a real running stack for --hours of real time
and writes every sample to an append-only JSONL checkpoint. Its verdict comes
from `summarise()`, which reads that file alone. These tests feed it made-up
checkpoints: a clean run passes, and each way a run can go wrong (a lost or
duplicate alert, a restart, a memory leak, a gap in the watching, an
interruption, server errors, a dead worker) changes the verdict. Nothing here
starts a server or touches a database.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("soak_wallclock", ROOT / "scripts" / "soak_wallclock.py")
soak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soak)

T0 = 1_800_000_000.0
MB = 2**20


def _run(hours: float = 1.0, interval: float = 30.0, *, rss_slope_mb_per_h: float = 0.0) -> list[dict]:
    """A clean, completed one-hour run sampled every 30 s."""
    recs = [{"type": "start", "run_id": "r1", "wall": T0, "hours": hours, "sample_interval_s": interval,
             "mode": "demo", "heartbeat_file": "/tmp/hb", "criteria": dict(soak.CRITERIA)}]
    n = int(hours * 3600 / interval)
    for i in range(1, n + 1):
        t = T0 + i * interval
        recs.append({"type": "sample", "seq": i - 1, "wall": t,
                     "health": {"ok": 6, "total": 6},
                     "load": {"requests": 20, "server_errors": 0, "client_errors": 0, "connection_errors": 0,
                              "latencies_ms": [10.0 + (i % 7)] * 20, "counts": {"report:200": 12, "due:200": 8}},
                     "processes": {"api": {"alive": True, "pid": 100, "create_time": 5.0,
                                           "rss_bytes": int((120 + rss_slope_mb_per_h * (t - T0) / 3600) * MB),
                                           "cpu_percent": 2.0}},
                     "worker_heartbeat_age_s": 30.0,
                     "db": {"size_bytes": 20 * MB + i * 1000, "rows": {"care_alerts": i}, "outbox_pending": 0,
                            "dispatch_retries": 0}})
        if i % 10 == 0:
            recs.append({"type": "event", "event": "help", "wall": t, "key": f"k{i}", "alert_id": f"a{i}",
                         "replay_id": f"a{i}"})
    end = T0 + n * interval
    recs.append({"type": "load_stopped", "wall": end + 0.5, "reason": "completed"})
    ids = [r["alert_id"] for r in recs if r.get("event") == "help"]
    recs.append({"type": "final_check", "wall": end + 60, "help_alert_ids": ids, "stuck_outbox_rows": 0,
                 "open_alerts_at_end": 0})
    recs.append({"type": "end", "wall": end + 61, "reason": "completed"})
    return recs


def _summary(recs):
    return soak.summarise(copy.deepcopy(recs))


def test_clean_run_passes():
    s = _summary(_run())
    assert s["verdict"] == "PASS", s["checks"]
    assert s["alerts"]["issued"] == 12 and s["alerts"]["lost"] == 0 and s["alerts"]["duplicates"] == 0
    assert s["wall_clock"]["observed_coverage"] == 1.0
    assert s["availability"]["ratio"] == 1.0


def test_lost_alert_fails():
    recs = _run()
    final = next(r for r in recs if r["type"] == "final_check")
    final["help_alert_ids"] = final["help_alert_ids"][1:]
    s = _summary(recs)
    assert s["alerts"]["lost"] == 1
    assert s["checks"]["lost_alerts"] is False and s["verdict"] == "FAIL"


def test_unexpected_alert_in_the_database_is_a_duplicate():
    recs = _run()
    next(r for r in recs if r["type"] == "final_check")["help_alert_ids"].append("ghost")
    s = _summary(recs)
    assert s["alerts"]["duplicates"] == 1 and s["alerts"]["unexpected_ids"] == ["ghost"]
    assert s["verdict"] == "FAIL"


def test_replay_answered_with_another_alert_is_a_duplicate():
    recs = _run()
    next(r for r in recs if r.get("event") == "help")["replay_id"] = "different"
    s = _summary(recs)
    assert s["alerts"]["replay_mismatches"] == 1 and s["verdict"] == "FAIL"


def test_same_alert_row_listed_twice_is_a_duplicate():
    recs = _run()
    final = next(r for r in recs if r["type"] == "final_check")
    final["help_alert_ids"].append(final["help_alert_ids"][0])
    assert _summary(recs)["checks"]["duplicate_alerts"] is False


def test_process_restart_fails():
    recs = _run()
    samples = [r for r in recs if r["type"] == "sample"]
    for r in samples[60:]:
        r["processes"]["api"]["pid"] = 200
    s = _summary(recs)
    assert s["restarts"] == {"api": 1} and s["verdict"] == "FAIL"


def test_process_gone_then_back_counts_once():
    recs = _run()
    samples = [r for r in recs if r["type"] == "sample"]
    samples[30]["processes"]["api"] = {"alive": False}
    assert _summary(recs)["restarts"] == {"api": 1}


def test_memory_growth_fails_and_flat_memory_passes():
    assert _summary(_run(rss_slope_mb_per_h=0.0))["checks"]["rss_growth"] is True
    leaking = _summary(_run(rss_slope_mb_per_h=12.0))   # 10 % of 120 MB per hour
    assert leaking["processes"]["api"]["rss_growth_pct_per_hour"] > 5.0
    assert leaking["checks"]["rss_growth"] is False and leaking["verdict"] == "FAIL"


def test_gap_in_watching_makes_the_run_incomplete_not_pass():
    recs = _run()
    # Drop ten minutes of samples: the harness was not watching.
    recs = [r for r in recs if not (r["type"] == "sample" and T0 + 1200 < r["wall"] <= T0 + 1800)]
    s = _summary(recs)
    assert s["wall_clock"]["unobserved_s"] > 500
    assert s["wall_clock"]["observed_coverage"] < 0.99
    assert s["verdict"] == "INCOMPLETE"


def test_interrupted_run_is_incomplete_even_if_every_check_held():
    recs = [r for r in _run() if r["type"] != "end"]
    s = _summary(recs)
    assert s["wall_clock"]["completed"] is False
    assert s["verdict"] == "INCOMPLETE"


def test_interrupted_by_signal_is_incomplete():
    recs = _run()
    recs[-1]["reason"] = "interrupted:SIGTERM"
    assert _summary(recs)["verdict"] == "INCOMPLETE"


def test_server_errors_fail():
    recs = _run()
    next(r for r in recs if r["type"] == "sample")["load"]["server_errors"] = 1
    s = _summary(recs)
    assert s["checks"]["server_errors"] is False and s["verdict"] == "FAIL"


def test_client_errors_fail_but_a_refreshed_token_does_not():
    recs = _run()
    first = next(r for r in recs if r["type"] == "sample")
    first["load"]["counts"]["auth_refresh"] = 3
    assert _summary(recs)["verdict"] == "PASS"
    first["load"]["client_errors"] = 2
    s = _summary(recs)
    assert s["checks"]["client_errors"] is False and s["verdict"] == "FAIL"


def test_alert_left_open_at_the_end_fails():
    recs = _run()
    next(r for r in recs if r["type"] == "final_check")["open_alerts_at_end"] = 2
    s = _summary(recs)
    assert s["checks"]["alerts_closed"] is False and s["verdict"] == "FAIL"


def test_availability_below_threshold_fails():
    recs = _run()
    # 720 health checks; 99.9 % allows none to be missed in practice.
    next(r for r in recs if r["type"] == "sample")["health"]["ok"] = 4
    s = _summary(recs)
    assert s["availability"]["ratio"] < 0.999 and s["verdict"] == "FAIL"


def test_stale_worker_heartbeat_fails():
    recs = _run()
    [r for r in recs if r["type"] == "sample"][50]["worker_heartbeat_age_s"] = 600.0
    assert _summary(recs)["checks"]["worker_heartbeat"] is False


def test_stuck_outbox_fails():
    recs = _run()
    next(r for r in recs if r["type"] == "final_check")["stuck_outbox_rows"] = 2
    assert _summary(recs)["checks"]["outbox_drained"] is False


def test_slow_requests_fail():
    recs = _run()
    for r in recs:
        if r["type"] == "sample":
            r["load"]["latencies_ms"] = [1500.0] * 20
    assert _summary(recs)["checks"]["p95_latency"] is False


def test_missing_final_check_cannot_pass():
    recs = [r for r in _run() if r["type"] != "final_check"]
    s = _summary(recs)
    assert s["alerts"] is None and s["verdict"] == "FAIL"


def test_torn_last_line_is_skipped(tmp_path):
    p = tmp_path / "c.jsonl"
    lines = [json.dumps(r) for r in _run()]
    p.write_text("\n".join(lines) + "\n" + '{"type": "sample", "wall": 1')
    assert len(soak.read_checkpoint(p)) == len(lines)


def test_percentile_and_slope():
    assert soak.percentile([], 95) is None
    assert soak.percentile([5, 1, 3, 2, 4], 50) == 3
    assert soak.percentile(range(1, 101), 95) == 95
    with pytest.raises(ValueError):
        soak.percentile([1], 0)
    assert soak.slope_per_hour([(0, 0), (3600, 10)]) == pytest.approx(10)
    assert soak.slope_per_hour([(5, 1)]) is None


def test_gap_detection_counts_only_the_extra_time():
    assert soak.observed_gaps([0, 30, 60, 90], 30) == []
    gaps = soak.observed_gaps([0, 30, 330, 360], 30)
    assert gaps == [{"from": 30, "to": 330, "unobserved_s": 270}]


def test_real_providers_refused():
    assert soak.refuse_real_providers({}) == []
    assert soak.refuse_real_providers({"IRA_CARE_TWILIO_ACCOUNT_SID": "  "}) == []
    assert soak.refuse_real_providers({"IRA_CARE_TWILIO_ACCOUNT_SID": "AC1", "IRA_CARE_ARC_URL": "https://x"}) == [
        "IRA_CARE_TWILIO_ACCOUNT_SID", "IRA_CARE_ARC_URL"]


def test_criteria_are_stated_in_the_module_docstring():
    doc = soak.__doc__
    for phrase in ("99 %", "99.9 %", "5 % per hour", "1000 ms", "180 s", "300 s", "NOT scripts/soak_24h.py"):
        assert phrase in doc, phrase
    assert soak.CRITERIA["min_availability"] == 0.999
    assert soak.CRITERIA["max_rss_growth_pct_per_hour"] == 5.0
