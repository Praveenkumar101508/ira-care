"""The demo API's memory tracing is a debugging aid that is off unless asked for
and refuses to run in production.

scripts/demo/memtrace.py starts tracemalloc and writes periodic snapshots when
IRA_DEBUG_MEMTRACE names a file. tracemalloc slows the process and costs
memory, so nothing may turn it on by default, and a production environment
must refuse it. Negative control: with the production check removed from
`wanted()`, `test_production_refuses_it` fails (the path is returned).
"""
from __future__ import annotations

import importlib.util
import json
import tracemalloc
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("demo_memtrace", ROOT / "scripts" / "demo" / "memtrace.py")
memtrace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memtrace)


def test_off_unless_asked():
    assert memtrace.wanted({}) is None
    assert memtrace.wanted({"IRA_DEBUG_MEMTRACE": "  "}) is None


@pytest.mark.parametrize("env", [{"IRA_ENV": "production"}, {"ENVIRONMENT": "Production"}, {"IRA_ENVIRONMENT": "prod"}])
def test_production_refuses_it(env):
    with pytest.raises(RuntimeError, match="must not be set in production"):
        memtrace.wanted({"IRA_DEBUG_MEMTRACE": "/tmp/x.jsonl", **env})


def test_development_gets_the_path():
    assert memtrace.wanted({"IRA_DEBUG_MEMTRACE": "/tmp/x.jsonl", "IRA_ENV": "development"}) == "/tmp/x.jsonl"


def test_the_demo_api_starts_it_only_through_the_guard():
    """api.py calls start_if_asked() (which goes through wanted()) and nothing else."""
    src = (ROOT / "scripts" / "demo" / "api.py").read_text()
    assert "start_if_asked()" in src
    assert "tracemalloc" not in src


def test_snapshot_records_heap_and_growth(tmp_path):
    out = tmp_path / "m.jsonl"
    mt = memtrace.MemTrace(str(out), interval=1, baseline_after=0)
    tracemalloc.start()
    try:
        mt.take("baseline")
        keep = [bytearray(1000) for _ in range(200)]   # noqa: F841 - held so it shows as growth
        mt.take("interval")
    finally:
        tracemalloc.stop()
    base, later = [json.loads(line) for line in out.read_text().splitlines()]
    assert base["baseline"] is True and "top_growth" not in base
    assert later["traced_current"] > 0 and later["gc_objects"] > 0
    assert any(d["size_diff"] >= 150_000 for d in later["top_growth"])
