#!/usr/bin/env python3
"""Drive a whole "person does not answer and stops moving" scenario, end to end.

    IRA_TEST_DATABASE_DSN=... IRA_TEST_ADMIN_DSN=... python scripts/simulate_patient.py

Against a real PostgreSQL (the test roles, never production), with a simulated
clock, the script:

  1. creates a family (tenant, workspace, administrator, a family member, a
     carer and a care organisation), a person, their safety settings and a
     paired watch;
  2. runs two normal check-ins: the watch reports movement and a heart rate,
     the person answers;
  3. then the person stops answering and stops moving: the check-in is asked,
     repeated, missed; the sensor check finds no movement; an SOS opens;
  4. the escalation chain runs: tier 1 (family and carer: push, text, call,
     all through the log-only provider), nobody acknowledges, tier 2 (the care
     organisation), nobody acknowledges, tier 3 (no monitoring centre: the
     "call 999 now" flag is set and tiers 1 and 2 repeat);
  5. the family member acknowledges ("I'm on my way"); the chain stops;
  6. everything is printed as a timeline, and the fixture family is removed.

Nothing here sends anything: the provider is log-only. No real phone number
is used (`+44000000000x`). The same functions are what
tests/tenancy/test_care_escalation_db.py asserts on, so a change that breaks
the chain fails CI rather than the next demo.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "supracloud-jarvis" / "ira"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo._local_state import prepare_environment  # noqa: E402

prepare_environment()


UTC = dt.timezone.utc
_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


@dataclass
class Timeline:
    events: list[tuple[dt.datetime, str]] = field(default_factory=list)

    def add(self, when: dt.datetime, text: str) -> None:
        self.events.append((when, text))

    def kinds(self) -> list[str]:
        return [t.split(":", 1)[0] for _, t in self.events]

    def render(self) -> str:
        return "\n".join(f"{when:%H:%M:%S}  {text}" for when, text in self.events)


@dataclass
class Family:
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    admin_user: uuid.UUID
    member_user: uuid.UUID
    person_id: uuid.UUID | None = None
    device: dict | None = None
    member_id: uuid.UUID | None = None


async def create_family(admin_dsn: str, pool, label: str = "sim") -> Family:
    """Tenant, default workspace and two users, created as the privileged role."""
    import asyncpg
    tenant_id, admin_user, member_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ws = uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant_id}")
    slug = f"{label}-{tenant_id.hex[:8]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute("INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')", tenant_id, slug, slug)
    finally:
        await admin.close()
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(admin_user))
        org = await conn.fetchval("INSERT INTO organisations (tenant_id,name) VALUES ($1,$2) RETURNING id", tenant_id, slug)
        await conn.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default,status) "
            "VALUES ($1,$2,$3,'Default','default',TRUE,'active')", ws, tenant_id, org)
        for uid in (admin_user, member_user):
            await conn.execute("INSERT INTO users (id,email,display_name,status) VALUES ($1,$2,'simulation','active')",
                               uid, f"{slug}-{uid.hex[:8]}@example.invalid")
        for k in ("app.tenant_id", "app.user_id", "app.workspace_id"):
            await conn.execute(f"SELECT set_config('{k}','',false)")
    return Family(tenant_id, ws, admin_user, member_user)


async def remove_family(admin_dsn: str, fam: Family) -> None:
    import asyncpg
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute("DELETE FROM care_access_log WHERE tenant_id = $1", fam.tenant_id)
        await admin.execute("DELETE FROM notifications WHERE tenant_id = $1", fam.tenant_id)
        await admin.execute("DELETE FROM tenants WHERE id = $1", fam.tenant_id)
        await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", [fam.admin_user, fam.member_user])
    finally:
        await admin.close()


def admin_principal(fam: Family):
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal
    return Principal(user_id=fam.admin_user, tenant_id=fam.tenant_id, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=fam.workspace_id, workspace_role="workspace_admin")


def member_principal(fam: Family):
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal
    return Principal(user_id=fam.member_user, tenant_id=fam.tenant_id, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=fam.workspace_id, workspace_role="member")


async def setup_person(fam: Family, *, heart_rate_range=(45, 130)) -> None:
    """A person, a circle of three tiers of fake contacts, a range, a paired watch."""
    from care import repository as repo
    p = admin_principal(fam)
    person = await repo.create_person(p, {"display_name": "Simulated Person", "preferred_name": "Sim",
                                          "timezone": "UTC", "home_address": "1 Example Street, Testtown, TE5 7ST", "home_latitude": 51.5, "home_longitude": -0.12, "emergency_summary": "Hard of hearing: speak clearly and face them.", "access_notes": "Key safe left of the front door. Small friendly dog called Biscuit."})
    fam.person_id = person["id"]
    member = await repo.add_member(p, person["id"], {
        "display_name": "Family Member (simulated)", "role": "family", "tier": 1, "priority": 1,
        "phone": "+440000000001", "user_id": fam.member_user})
    fam.member_id = member["id"]
    await repo.add_member(p, person["id"], {"display_name": "Carer (simulated)", "role": "carer", "tier": 1,
                                            "priority": 2, "phone": "+440000000002"})
    await repo.add_member(p, person["id"], {"display_name": "Care organisation (simulated)",
                                            "role": "care_organisation", "tier": 2, "phone": "+440000000003"})
    settings = await repo.get_settings(p, person["id"])
    settings = {k: settings[k] for k in settings if k not in ("person_id", "tenant_id", "workspace_id", "set_by", "updated_at")}
    settings["heart_rate_min"], settings["heart_rate_max"] = heart_rate_range
    await repo.put_settings(p, person["id"], settings)
    created = await repo.create_device(p, person["id"], {"platform": "wearos", "model": "Simulator"})
    paired = await repo.pair_device(created["pairing_code"], app_version="simulator")
    paired["person_id"] = person["id"]
    fam.device = paired


def _device_principal(fam: Family):
    from care.checkin_cycle import job_principal
    return job_principal(fam.tenant_id, fam.workspace_id)


async def watch_reports(fam: Family, now: dt.datetime, *, steps: float, heart_rate: float, on_wrist=True) -> None:
    from care import repository as repo
    await repo.record_report(_device_principal(fam), fam.device, {
        "readings": [
            {"kind": "steps", "value": steps, "unit": "", "recorded_at": now},
            {"kind": "heart_rate", "value": heart_rate, "unit": "bpm", "recorded_at": now},
            {"kind": "on_wrist", "value": 1.0 if on_wrist else 0.0, "unit": "", "recorded_at": now},
        ],
        "battery_percent": 70, "on_wrist": on_wrist, "background_sensor_permission": True})


async def run_scenario(fam: Family, timeline: Timeline, *, start: dt.datetime | None = None,
                       provider=None) -> dict:
    """The scenario. Returns the final alert (with attempts and acknowledgements)."""
    from care import checkin_cycle as cycle
    from care import escalation, repository as repo
    from care.providers import LogOnlyProvider

    provider = provider or LogOnlyProvider()
    rng = random.Random(7)
    jp = _device_principal(fam)
    now = start or dt.datetime(2026, 11, 21, 9, 0, tzinfo=UTC)

    async def tick(when: dt.datetime) -> None:
        await cycle.run_workspace(jp, when, rng=rng)
        await escalation.run_workspace(jp, when, provider=provider)

    # ── Two normal check-ins ────────────────────────────────────────────────
    await tick(now)
    for i in range(2):
        now += dt.timedelta(minutes=21)
        await watch_reports(fam, now - dt.timedelta(minutes=1), steps=40, heart_rate=72)
        asked = await cycle.due_checkin(jp, fam.device, now, rng=rng)
        assert asked is not None, "a check-in should be due by now"
        timeline.add(now, f"asked: {asked['question_text']}")
        now += dt.timedelta(seconds=20)
        answered = await cycle.record_answer(jp, fam.device, asked["id"], "I'm fine, thank you", now=now)
        timeline.add(now, f"answered: {answered['answer_summary']}")
        await tick(now)

    # ── The person stops answering and stops moving ─────────────────────────
    now += dt.timedelta(minutes=21)
    await watch_reports(fam, now - dt.timedelta(minutes=1), steps=0, heart_rate=70)
    asked = await cycle.due_checkin(jp, fam.device, now, rng=rng)
    timeline.add(now, f"asked: {asked['question_text']}")
    now += dt.timedelta(seconds=60)
    await cycle.mark_repeated(jp, fam.device, asked["id"], now)
    timeline.add(now, "repeated: louder, with vibration")
    now += dt.timedelta(seconds=120)
    await watch_reports(fam, now, steps=0, heart_rate=70)
    missed = await cycle.mark_missed(jp, fam.device, asked["id"], now)
    timeline.add(now, f"missed: {missed['status']}")
    out = await escalation.run_workspace(jp, now, provider=provider)
    for a in out.opened:
        timeline.add(now, f"alert opened: {a['kind']} ({a['severity']})")
    alert_id = out.opened[0]["id"] if out.opened else None
    assert alert_id is not None, "the sensor check should have opened an SOS"
    timeline.add(now, f"tier 1 told: {out.attempts} attempts (push, text, call; log-only provider)")

    # ── Nobody acknowledges: tier 2, then tier 3 ─────────────────────────────
    for label in ("tier 2", "tier 3"):
        now += dt.timedelta(minutes=5, seconds=1)
        out = await escalation.run_workspace(jp, now, provider=provider)
        timeline.add(now, f"{label} told: {out.attempts} attempts" +
                     (" (no monitoring centre: 'call 999 now' shown, tiers 1 and 2 repeated)"
                      if out.repeated_tier3 else ""))

    # ── The family member acknowledges ───────────────────────────────────────
    now += dt.timedelta(minutes=2)
    acked = await repo.acknowledge_alert(member_principal(fam), alert_id, {"kind": "on_my_way", "note": "10 minutes away"})
    timeline.add(now, f"acknowledged: {acked['status']} by the family member")
    now += dt.timedelta(minutes=6)
    out = await escalation.run_workspace(jp, now, provider=provider)
    timeline.add(now, f"after acknowledgement: {out.tiers_run} further tiers run")
    final = await repo.get_alert(admin_principal(fam), alert_id)
    timeline.add(now, f"final: status {final['status']}, tier {final['current_tier']}, "
                      f"{len(final['attempts'])} attempts, {len(final['acknowledgements'])} acknowledgement(s)")

    # ── The next morning: the carer's daily report records the day ──────────
    from care import reports
    from core.identity.db_context import workspace_connection
    async with workspace_connection(jp) as conn:
        person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", fam.person_id))
        report = await reports.generate(conn, person, reports.DAILY, now.date())
    if report is not None:
        timeline.add(now + dt.timedelta(hours=21), f"daily report: {report['title']}")
        final["daily_report"] = report
    return final


# ── Round 2: continuous monitoring scenarios ─────────────────────────────────

async def _hr_run(fam: Family, start: dt.datetime, values: list[float]) -> dict:
    """Heart-rate readings one minute apart, as the watch would send them."""
    from care import repository as repo
    readings = [{"kind": "heart_rate", "value": v, "unit": "bpm", "recorded_at": start + dt.timedelta(minutes=i)}
                for i, v in enumerate(values)]
    return await repo.record_report(_device_principal(fam), fam.device, {
        "readings": readings, "battery_percent": 70, "on_wrist": True, "background_sensor_permission": True,
        "now": readings[-1]["recorded_at"]})


async def scenario_night_heart_rate_no_answer(fam: Family, timeline: Timeline, *, provider=None) -> dict:
    """02:00: heart rate outside the range, confirmed over two minutes. IRA asks
    "are you OK?"; nobody answers within the wait; the alert becomes an SOS."""
    from care import checkin_cycle as cycle
    from care import escalation, repository as repo
    from care.providers import LogOnlyProvider
    provider = provider or LogOnlyProvider()
    jp = _device_principal(fam)
    now = dt.datetime(2026, 11, 22, 2, 0, tzinfo=UTC)
    out = await _hr_run(fam, now, [72, 158])
    timeline.add(now + dt.timedelta(minutes=1), "heart rate 158 once: nothing (a single reading is noise)")
    assert "safety_check" not in out
    out = await _hr_run(fam, now + dt.timedelta(minutes=2), [161, 164, 166])
    now += dt.timedelta(minutes=4)
    assert "safety_check" in out, "three readings over two minutes should ask first"
    timeline.add(now, f"heart rate 161, 164, 166 over two minutes: asking first ({out['safety_check']['kind']})")
    now += dt.timedelta(seconds=5)
    due = await cycle.due_checkin(jp, fam.device, now)
    assert due and due["safety"], "the watch collects the safety question at any hour"
    timeline.add(now, f"watch vibrates and asks: {due['question_text']}")
    now += dt.timedelta(seconds=20)
    await cycle.mark_repeated(jp, fam.device, due["id"], now)
    timeline.add(now, "repeated, louder")
    now += dt.timedelta(seconds=40)
    out = await escalation.run_workspace(jp, now, provider=provider)
    timeline.add(now, f"no answer within the wait: {out.promoted} question became an SOS, tier 1 told ({out.attempts} attempts)")
    assert out.promoted == 1 and out.tiers_run == 1
    alerts = await repo.list_alerts(admin_principal(fam), person_id=None, open_only=True, limit=10)
    final = next(a for a in alerts if a["kind"] == "heart_rate_outside_range")
    details = final["details"] if isinstance(final["details"], dict) else json.loads(final["details"] or "{}")
    timeline.add(now, f"final: {final['kind']} {final['status']} (outcome {details.get('outcome')})")
    return final


async def scenario_heart_rate_answered_ok(fam: Family, timeline: Timeline, *, provider=None) -> dict:
    """Heart rate outside the range in the afternoon; the person taps I'm OK."""
    from care import checkin_cycle as cycle
    from care import repository as repo
    jp = _device_principal(fam)
    now = dt.datetime(2026, 11, 22, 15, 0, tzinfo=UTC)
    out = await _hr_run(fam, now, [38, 37, 39])
    now += dt.timedelta(minutes=2)
    assert "safety_check" in out
    timeline.add(now, "heart rate 38, 37, 39 over two minutes: asking first")
    now += dt.timedelta(seconds=5)
    due = await cycle.due_checkin(jp, fam.device, now)
    timeline.add(now, f"watch asks: {due['question_text']}")
    now += dt.timedelta(seconds=12)
    answered = await cycle.record_answer(jp, fam.device, due["id"], "", via="button", now=now)
    timeline.add(now, f"answered: {answered['answer_summary']}")
    alerts = await repo.list_alerts(admin_principal(fam), person_id=None, open_only=False, limit=10)
    final = next(a for a in alerts if a["kind"] == "heart_rate_outside_range")
    timeline.add(now, f"final: {final['status']} ({final['resolution']})")
    assert final["status"] == "resolved"
    return final


async def scenario_no_movement_by_wake_time(fam: Family, timeline: Timeline, *, provider=None) -> dict:
    """Nothing moved by the usual wake time plus the grace: a morning check-in
    is asked; no answer follows the normal missed-check-in path to an SOS."""
    from care import checkin_cycle as cycle
    from care import escalation
    from care.providers import LogOnlyProvider
    provider = provider or LogOnlyProvider()
    jp = _device_principal(fam)
    rng = random.Random(3)
    now = dt.datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    await watch_reports(fam, now - dt.timedelta(minutes=10), steps=0, heart_rate=66)
    timeline.add(now - dt.timedelta(minutes=10), "watch reports: on the wrist, no steps since waking")
    out = await cycle.run_workspace(jp, now, rng=rng)
    timeline.add(now, f"08:00, nothing moved since 07:30: morning checks {out.morning_checks} (still within the grace)")
    now = now.replace(hour=8, minute=31)
    out = await cycle.run_workspace(jp, now, rng=rng)
    assert out.morning_checks == 1
    timeline.add(now, "08:31, still nothing: morning check asked now")
    now += dt.timedelta(seconds=5)
    due = await cycle.due_checkin(jp, fam.device, now, rng=rng)
    timeline.add(now, f"watch asks: {due['question_text']}")
    now += dt.timedelta(seconds=60)
    await cycle.mark_repeated(jp, fam.device, due["id"], now)
    now += dt.timedelta(seconds=120)
    await watch_reports(fam, now, steps=0, heart_rate=68)
    missed = await cycle.mark_missed(jp, fam.device, due["id"], now)
    timeline.add(now, f"missed: {missed['status']}")
    out = await escalation.run_workspace(jp, now, provider=provider)
    for a in out.opened:
        timeline.add(now, f"alert opened: {a['kind']} ({a['severity']})")
    assert out.opened, "a missed morning check with no movement opens an SOS"
    return {"opened": out.opened, "attempts": out.attempts}


async def scenario_help_away_from_home(fam: Family, timeline: Timeline, *, provider=None) -> dict:
    """The Help button pressed 2 km from home: the watch sends its location
    once, the SOS says AWAY FROM HOME with the address and a map link, and the
    family member on the way can open the access notes (logged)."""
    from care import escalation, repository as repo
    from care.providers import LogOnlyProvider
    provider = provider or LogOnlyProvider()
    jp = _device_principal(fam)
    now = dt.datetime(2026, 11, 24, 11, 0, tzinfo=UTC)
    alert = await repo.record_help_request(jp, fam.device, {"via": "button", "location": {
        "latitude": 51.518, "longitude": -0.12, "accuracy_m": 12}})
    timeline.add(now, f"help pressed; watch sent one location fix; at_home={alert['at_home']}")
    out = await escalation.run_workspace(jp, now, provider=provider)
    texts = [t for ch, _to, t in provider.log if ch == "sms"]
    timeline.add(now, f"tier 1 told ({out.attempts} attempts). Text: {texts[0] if texts else '(no text: no phone)'}")
    notes = await repo.open_access_notes(member_principal(fam), alert["id"])
    timeline.add(now + dt.timedelta(minutes=1), f"family member opened the access notes (allowed={notes['allowed']}): {notes.get('access_notes', '')}")
    return alert


SCENARIOS = {
    "stops-answering": None,  # the original scenario, run_scenario
    "night-heart-rate-no-answer": scenario_night_heart_rate_no_answer,
    "heart-rate-answered-ok": scenario_heart_rate_answered_ok,
    "no-movement-by-wake-time": scenario_no_movement_by_wake_time,
    "help-away-from-home": scenario_help_away_from_home,
}


async def main() -> int:
    import asyncpg
    dsn, admin_dsn = os.getenv("IRA_TEST_DATABASE_DSN", ""), os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not dsn or not admin_dsn:
        print("Set IRA_TEST_DATABASE_DSN (the ira_app role) and IRA_TEST_ADMIN_DSN (a privileged role) "
              "to a TEST database with the migration chain applied.", file=sys.stderr)
        return 2
    import utils.db as db_module
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    db_module._pool = pool
    wanted = sys.argv[1:] or list(SCENARIOS)
    unknown = [w for w in wanted if w not in SCENARIOS]
    if unknown:
        print("unknown scenario(s):", ", ".join(unknown), "\nknown:", ", ".join(SCENARIOS), file=sys.stderr)
        return 2
    rc = 0
    try:
        for name in wanted:
            fam = await create_family(admin_dsn, pool, label=f"sim-{name}")
            timeline = Timeline()
            try:
                await setup_person(fam)
                print(f"=== {name} ===")
                if name == "stops-answering":
                    final = await run_scenario(fam, timeline)
                    ok = final["status"] == "acknowledged" and final["current_tier"] == 3
                    verdict = "the chain ran to tier 3 and stopped at the acknowledgement"
                else:
                    await SCENARIOS[name](fam, timeline)
                    ok, verdict = True, "the scenario ran as described"
                print(timeline.render())
                print("RESULT:", verdict if ok else "UNEXPECTED", "\n")
                rc = rc or (0 if ok else 1)
            finally:
                await remove_family(admin_dsn, fam)
    finally:
        await pool.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
