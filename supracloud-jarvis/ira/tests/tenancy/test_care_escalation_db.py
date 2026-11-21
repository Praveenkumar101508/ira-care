"""IRA CARE (phase 4): the rules and the SOS chain against a real PostgreSQL.

The whole "person does not answer and stops moving" scenario comes from
`scripts/simulate_patient.py`, so the demo and the test are the same code.
Around it: an acknowledgement stops the chain; a notice is told once and does
not escalate; a kind already open is not opened twice; the log-only provider
sent nothing; no emergency number was ever dialled.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import random
import sys
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from tests.tenancy._care_fixtures import pool  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("simulate_patient", ROOT / "scripts" / "simulate_patient.py")
sim = importlib.util.module_from_spec(_spec)
sys.modules["simulate_patient"] = sim
_spec.loader.exec_module(sim)

UTC = dt.timezone.utc


@pytest.fixture
async def family(pool):
    fam = await sim.create_family(ADMIN_DSN, pool, label="esc")
    try:
        await sim.setup_person(fam)
        yield fam
    finally:
        await sim.remove_family(ADMIN_DSN, fam)


async def test_the_full_scenario_runs_to_tier_three_and_stops_at_the_acknowledgement(family):
    from care import providers
    from care.escalation import CALL_999_FLAG
    provider = providers.LogOnlyProvider()
    timeline = sim.Timeline()
    final = await sim.run_scenario(family, timeline, provider=provider)
    kinds = timeline.kinds()
    assert kinds.count("answered") == 2 and "missed" in kinds
    assert any(k.startswith("alert opened") for k in kinds)
    assert final["kind"] == "no_answer_no_movement" and final["severity"] == "sos"
    assert final["status"] == "acknowledged" and final["current_tier"] == 3
    assert final["details"][CALL_999_FLAG] is True
    tiers = [a["tier"] for a in final["attempts"]]
    assert 1 in tiers and 2 in tiers and 3 in tiers
    channels = {a["channel"] for a in final["attempts"]}
    assert {"push", "sms", "call", "dashboard", "monitoring_centre"} <= channels
    centre = [a for a in final["attempts"] if a["channel"] == "monitoring_centre"]
    assert centre and centre[0]["outcome"] == "skipped" and "999" in centre[0]["detail"]
    # Nothing was sent, and no emergency number was dialled.
    assert all(a["outcome"] in ("logged", "failed", "skipped") for a in final["attempts"])
    numbers = {to for _kind, to, _text in provider.log}
    assert numbers == {"+440000000001", "+440000000002", "+440000000003"}
    assert final["acknowledgements"][0]["kind"] == "on_my_way"
    assert final["acknowledgements"][0]["member_id"] == family.member_id
    # And the next morning's report records the day: the answers, the miss,
    # the alert and who went, in summaries, without the person's words.
    report = final["daily_report"]
    text = report["body_text"]
    assert "2 answered, 1 missed, of 3 asked" in text
    assert "no answer and no movement (sos, reached tier 3, now acknowledged acknowledged by Family Member (simulated))" in text
    assert "fine, thank you" not in text


async def test_a_notice_is_told_once_and_does_not_escalate(family, pool):
    from care import escalation, providers, repository as repo
    jp = sim._device_principal(family)
    now = dt.datetime.now(UTC)  # the report below syncs at the real clock
    await repo.record_report(jp, family.device, {"readings": [], "battery_percent": 9, "on_wrist": True,
                                                  "background_sensor_permission": True})
    provider = providers.LogOnlyProvider()
    out = await escalation.run_workspace(jp, now, provider=provider)
    assert [o["kind"] for o in out.opened] == ["low_battery"] and out.notices == 1
    alerts = await repo.list_alerts(sim.admin_principal(family), person_id=family.person_id, open_only=True, limit=10)
    assert len(alerts) == 1 and alerts[0]["next_action_at"] is None and alerts[0]["current_tier"] == 1
    later = await escalation.run_workspace(jp, now + dt.timedelta(minutes=30), provider=provider)
    assert later.tiers_run == 0 and later.alerts_opened == 0, "a notice neither escalates nor re-opens"
    assert all(kind == "sms" for kind, _to, _text in provider.log), "a notice is not a phone call"


async def test_a_kind_already_open_is_not_opened_twice_and_help_escalates_at_once(family):
    from care import escalation, providers, repository as repo
    jp = sim._device_principal(family)
    first = await repo.record_help_request(jp, family.device, {"via": "button"})
    second = await repo.record_help_request(jp, family.device, {"via": "voice"})
    now = dt.datetime.now(UTC) + dt.timedelta(seconds=1)  # help is due the moment it is pressed
    assert first["id"] != second["id"], "a second press is a second row (the person pressed twice)"
    provider = providers.LogOnlyProvider()
    out = await escalation.run_workspace(jp, now, provider=provider)
    assert out.tiers_run == 2 and out.attempts > 0
    alerts = await repo.list_alerts(sim.admin_principal(family), person_id=family.person_id, open_only=True, limit=10)
    helps = [a for a in alerts if a["kind"] == "help_requested"]
    assert len(helps) == 2 and all(a["status"] == "escalating" and a["current_tier"] == 1 for a in helps)
    # The rules engine does not open another help alert on top.
    out2 = await escalation.run_workspace(jp, now + dt.timedelta(seconds=30), provider=provider)
    assert out2.alerts_opened == 0


async def test_normal_readings_log_the_miss_and_escalate_on_the_third(family):
    from care import checkin_cycle as cycle
    from care import escalation, providers
    jp = sim._device_principal(family)
    provider = providers.LogOnlyProvider()
    rng = random.Random(3)
    now = dt.datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    await cycle.run_workspace(jp, now, rng=rng)
    kinds = []
    for i in range(3):
        now += dt.timedelta(minutes=21)
        await sim.watch_reports(family, now - dt.timedelta(seconds=30), steps=25, heart_rate=70)
        asked = await cycle.due_checkin(jp, family.device, now, rng=rng)
        assert asked is not None
        now += dt.timedelta(minutes=4, seconds=30)  # past repeat + sensor check + grace
        await sim.watch_reports(family, now - dt.timedelta(seconds=10), steps=25, heart_rate=70)
        await cycle.run_workspace(jp, now, rng=rng)  # marks the miss, schedules the next
        out = await escalation.run_workspace(jp, now, provider=provider)
        kinds.extend(o["kind"] for o in out.opened)
        if i < 2:
            assert kinds == [], f"a miss with normal readings and movement must not escalate (miss {i + 1})"
    assert kinds == ["missed_checkins"]
