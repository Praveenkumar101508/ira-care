#!/usr/bin/env python3
"""Scripted demo scenarios against the seeded demo organisation.

    python scripts/demo/scenarios.py [normal-day|night-escalation|fall-with-location|morning-report|all]

Each scenario drives the real care code with an explicit clock, so the
dashboard and the family app show the results afterwards: a normal day with a
conversation; a missed check-in at night with an abnormal heart rate and the
full escalation chain; a suspected fall with location; the next morning's
carer report. Everything is fake and log-only; nothing dials anyone.
Reads `.demo/demo.local.json` written by seed_demo.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
sys.path.insert(0, str(ROOT / "scripts"))

from demo._local_state import prepare_environment  # noqa: E402

prepare_environment(edition="cloud", model="fake")

UTC = dt.timezone.utc


def principal_for(d: "Demo"):
    """The demo manager, for the carer-side calls (ask now)."""
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal
    return Principal(user_id=uuid.UUID(d.info["manager_user_id"]), tenant_id=d.tenant, session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=d.ws, workspace_role="workspace_admin")


class Demo:
    def __init__(self, info: dict):
        from care import checkin_cycle as cycle
        from care.providers import LogOnlyProvider
        self.info = info
        self.tenant = uuid.UUID(info["tenant_id"]); self.ws = uuid.UUID(info["workspace_id"])
        self.jp = cycle.job_principal(self.tenant, self.ws)
        self.provider = LogOnlyProvider()
        self.lines: list[str] = []

    async def device(self, index: int = 0) -> dict:
        from care import repository as repo
        p = self.info["people"][index]
        found = await repo.authenticate_device(p["device_token"])
        if found is None:
            raise SystemExit("the demo device token is not accepted: run seed_demo.py again")
        _principal, device = found
        return device

    def log(self, when: dt.datetime, text: str) -> None:
        self.lines.append(f"{when:%a %H:%M:%S}  {text}")
        print(self.lines[-1])


async def normal_day(d: Demo, day: dt.date) -> None:
    """Three check-ins asked now and answered, then three things said to IRA.
    The real clock is used, so the dashboard shows it all as it happened."""
    from care import checkin_cycle as cycle
    from care import repository as repo, talk
    device = await d.device(0)
    pid = device["person_id"]
    manager = principal_for(d)
    answers = ["I'm fine, I had my tablet with breakfast", "Yes, soup and bread", "Feeling cheerful today, the sun is out"]
    for i, kind in enumerate(("medication", "meal", "mood")):
        now = dt.datetime.now(UTC)
        earlier = now - dt.timedelta(minutes=15 - i)   # ordinary readings, a little before each question
        await repo.record_report(d.jp, device, {"readings": [{"kind": "steps", "value": 40, "unit": "", "recorded_at": earlier},
                                                            {"kind": "heart_rate", "value": 74, "unit": "bpm", "recorded_at": earlier}],
                                                "battery_percent": 80, "on_wrist": True, "background_sensor_permission": True})
        await cycle.ask_now(manager, pid, kind=kind, now=now)
        due = await cycle.due_checkin(d.jp, device, now + dt.timedelta(seconds=1))
        if due is None:
            d.log(now, "no question came due (quiet hours?)")
            continue
        d.log(now, f"IRA asked ({due.get('question_source', 'template')}): {due['question_text']}")
        out = await cycle.record_answer(d.jp, device, due["id"], answers[i], now=now + dt.timedelta(seconds=20))
        d.log(now, f"summary for the family: {out['answer_summary']}")
    for words in ("what's on today?", "did I take my tablets?", "call my daughter"):
        spoken = []
        now = dt.datetime.now(UTC)
        async for item in talk.exchange(d.jp, pid, words, source="watch", now=now):
            if "sentence" in item:
                spoken.append(item["sentence"])
            elif item.get("action"):
                spoken.append(f"[action: {item['action']}]")
        d.log(now, f"Talk: '{words}' -> {' '.join(spoken)}")


async def night_escalation(d: Demo, day: dt.date) -> None:
    """An abnormal heart rate confirmed over two minutes, "are you OK?" unanswered,
    and the whole chain: tier 1, tier 2, tier 3 with "call 999 now" shown. The
    chain is advanced with an explicit clock a few minutes ahead of now."""
    from care import checkin_cycle as cycle
    from care import escalation, repository as repo
    device = await d.device(0)
    now = dt.datetime.now(UTC)
    readings = [{"kind": "heart_rate", "value": v, "unit": "bpm", "recorded_at": now - dt.timedelta(minutes=2 - i)} for i, v in enumerate((158, 161, 164))]
    out = await repo.record_report(d.jp, device, {"readings": readings, "battery_percent": 60, "on_wrist": True,
                                                  "background_sensor_permission": True, "now": now})
    d.log(now, f"heart rate 158, 161, 164 over two minutes: {out.get('safety_check', {}).get('kind', 'nothing')} -> asking first")
    due = await cycle.due_checkin(d.jp, device, now + dt.timedelta(seconds=2))
    if due:
        d.log(now, f"watch vibrates: {due['question_text']}")
    t = now + dt.timedelta(seconds=75)
    out = await escalation.run_workspace(d.jp, t, provider=d.provider)
    d.log(t, f"no answer within the wait: {out.promoted} question became an SOS; tier 1 told ({out.attempts} attempts, log-only)")
    for label, mins in (("tier 2", 5), ("tier 3", 10)):
        t2 = t + dt.timedelta(minutes=mins, seconds=2)
        out = await escalation.run_workspace(d.jp, t2, provider=d.provider)
        d.log(t2, f"{label}: {out.attempts} attempts" + (" ('call 999 now' shown to the family; nothing dials)" if out.repeated_tier3 else ""))
    texts = [x for ch, _to, x in d.provider.log if ch == "sms"]
    if texts:
        d.log(t, f"text sent (log-only): {texts[0][:220]}")


async def fall_with_location(d: Demo, day: dt.date) -> None:
    from care import escalation, falls, repository as repo
    device = await d.device(1)
    person = d.info["people"][1]
    t = dt.datetime.now(UTC)
    alert = await falls.start(d.jp, device, {"peak_g": 3.6, "still_seconds": 20, "now": t})
    d.log(t, f"watch: hard impact 3.6 g then 20 s still -> asking first (alert {alert['status'] if alert else 'already open'}), countdown on the wrist")
    body = {"via": "fall", "location": {"latitude": person["home_latitude"] + 0.018, "longitude": person["home_longitude"], "accuracy_m": 12}}
    a = await repo.record_help_request(d.jp, device, body)
    d.log(t, f"countdown ended: SOS {a['kind']} ({a['status']}), at home: {a['at_home']}")
    out = await escalation.run_workspace(d.jp, dt.datetime.now(UTC) + dt.timedelta(seconds=5), provider=d.provider)
    texts = [x for ch, _to, x in d.provider.log if ch == "sms"]
    d.log(t, f"tier 1 told ({out.attempts} attempts). {texts[-1][:260] if texts else ''}")


async def morning_report(d: Demo, day: dt.date) -> None:
    from care import reports
    from core.identity.db_context import workspace_connection
    pid = uuid.UUID(d.info["people"][0]["id"])
    async with workspace_connection(d.jp) as conn:
        person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", pid))
        await conn.execute("DELETE FROM care_reports WHERE person_id = $1 AND kind = 'daily_carer' AND period_start = $2", pid, day)
        report = await reports.generate(conn, person, reports.DAILY, day)
    when = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(7, 0), tzinfo=UTC)
    d.log(when, f"carer report: {report['title']}")
    for line in (report.get("body_text") or "").split("\n")[:12]:
        if line.strip():
            d.log(when, f"  {line.strip()[:160]}")


SCENARIOS = {"normal-day": normal_day, "night-escalation": night_escalation,
             "fall-with-location": fall_with_location, "morning-report": morning_report}


async def main(names: list[str]) -> int:
    import asyncpg
    import utils.db as db_module
    info_path = ROOT / ".demo" / "demo.local.json"
    if not info_path.exists():
        print("run scripts/demo/seed_demo.py first", file=sys.stderr)
        return 2
    dsn = os.getenv("IRA_TEST_DATABASE_DSN", "")
    if not dsn:
        print("set IRA_TEST_DATABASE_DSN", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    db_module._pool = pool
    try:
        d = Demo(json.loads(info_path.read_text()))
        day = dt.date.today()
        for name in names:
            print(f"=== {name} ===")
            await SCENARIOS[name](d, day)
        (ROOT / ".demo" / "scenarios.log").write_text("\n".join(d.lines) + "\n")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    wanted = sys.argv[1:] or ["all"]
    if wanted == ["all"]:
        wanted = list(SCENARIOS)
    unknown = [w for w in wanted if w not in SCENARIOS]
    if unknown:
        print("unknown:", unknown, "known:", list(SCENARIOS), file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(wanted)))
