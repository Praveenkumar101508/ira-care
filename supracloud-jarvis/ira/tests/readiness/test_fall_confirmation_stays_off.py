"""The fall confirmation stage stays off while its recorded-data gate fails (F4.i, D-110).

docs/ai/fall/recorded_confirmation.json is the run of the stage on BITS-2 and UCI HAR
(scripts/eval/fall_recorded_confirmation.py). Its gate: no fall the shipped pipeline catches may
be lost. While that gate has not passed, the watch's default must be "ask straight away" and no
server code may switch the stage on. A later run that passes the gate is what allows turning it on,
by an explicit decision, never by this test going quiet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RECORD = ROOT / "docs" / "ai" / "fall" / "recorded_confirmation.json"
CARE_API = ROOT / "watch" / "app" / "src" / "main" / "java" / "care" / "ira" / "watch" / "api" / "CareApi.kt"
SERVER = ROOT / "supracloud-jarvis" / "ira"


def _gate_passed() -> bool:
    return bool(json.loads(RECORD.read_text())["summary"]["gate"]["passed"])


def test_the_recorded_run_exists_and_names_what_it_lost():
    s = json.loads(RECORD.read_text())["summary"]
    assert s["falls"]["recordings"] > 0 and s["falls"]["caught_before"] > 0
    assert s["gate"]["passed"] == (s["falls"]["lost_to_confirmation"] == 0)
    assert len(s["falls"]["lost_ids"]) == s["falls"]["lost_to_confirmation"]


def test_the_watch_asks_straight_away_by_default_while_the_gate_fails():
    if _gate_passed():
        return
    text = CARE_API.read_text(encoding="utf-8")
    m = re.search(r'@SerialName\("fall_confirm_before_asking"\)\s*val\s+\w+:\s*Boolean\s*=\s*(\w+)', text)
    assert m is not None, "the setting's declaration moved; update this test"
    assert m.group(1) == "false"


def test_no_server_code_switches_the_stage_on_while_the_gate_fails():
    if _gate_passed():
        return
    on = re.compile(r"""["']fall_confirm_before_asking["']\s*[:=]\s*(True|true|1)""")
    for p in SERVER.rglob("*.py"):
        if "tests" in p.parts:
            continue
        assert not on.search(p.read_text(encoding="utf-8", errors="ignore")), p
