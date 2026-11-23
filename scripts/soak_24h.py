#!/usr/bin/env python3
"""An accelerated day for 200 people: job timings and no missed escalations.

    IRA_TEST_DATABASE_DSN=... IRA_TEST_ADMIN_DSN=... python scripts/soak_24h.py [--people 200] [--step-minutes 5]

Seeds one workspace with N people and paired watches, then advances a
simulated clock through 24 hours in steps, running the check-in pass and the
monitoring pass at every step exactly as the worker would. Watches answer
most questions; a chosen set of people stop answering and stop moving at
known times, and the run asserts that every one of them reached an SOS and
tier 1 within the configured window. Records per-pass timings. All data is
synthetic and removed at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
sys.path.insert(0, str(ROOT / "scripts"))

from demo._local_state import prepare_environment  # noqa: E402

prepare_environment()

UTC = dt.timezone.utc


async def main(people_n: int, step_min: int) -> int:
    import asyncpg
    import utils.db as db_module
    from simulate_patient import create_family, remove_family
    from care import checkin_cycle as cycle
    from care import escalation, repository as repo
    from care.providers import LogOnlyProvider
    from core.identity.db_context import workspace_connection

    dsn, admin_dsn = os.environ["IRA_TEST_DATABASE_DSN"], os.environ["IRA_TEST_ADMIN_DSN"]
    pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=8)
    db_module._pool = pool
    fam = await create_family(admin_dsn, pool, label="soak")
    jp = cycle.job_principal(fam.tenant_id, fam.workspace_id)
    rng = random.Random(2026)
    timings: dict[str, list[float]] = {"checkins": [], "monitoring": []}
    try:
        # ── Seed: N people with a paired watch and a circle member ──────────
        t0 = time.perf_counter()
        devices: list[dict] = []
        async with workspace_connection(jp) as conn:
            ids = [uuid.uuid4() for _ in range(people_n)]
            await conn.executemany(
                "INSERT INTO care_people (id, display_name, preferred_name, timezone) VALUES ($1, $2, 'P', 'UTC')",
                [(i, f"Soak Person {n:03d}") for n, i in enumerate(ids)])
            await conn.executemany("INSERT INTO care_safety_settings (person_id, heart_rate_min, heart_rate_max) VALUES ($1, 45, 130)",
                                   [(i,) for i in ids])
            await conn.executemany(
                "INSERT INTO care_circle_members (person_id, display_name, role, tier, phone) VALUES ($1, 'Soak Family', 'family', 1, '+440000000001')",
                [(i,) for i in ids])
            import hashlib
            # The soak drives the device records directly; the hash only has to look like one.
            rows = [(i, hashlib.sha256(uuid.uuid4().bytes).hexdigest()) for i in ids]
            await conn.executemany(
                "INSERT INTO care_devices (person_id, platform, status, token_hash, paired_at, last_sync_at, on_wrist, battery_percent) "
                "VALUES ($1, 'wearos', 'paired', $2, NOW(), NOW(), TRUE, 80)", rows)
            devices = [dict(r) for r in await conn.fetch("SELECT * FROM care_devices WHERE person_id = ANY($1::uuid[])", ids)]
        print(f"seeded {people_n} people in {time.perf_counter() - t0:.1f} s")

        # ── The day ────────────────────────────────────────────────────────
        start = dt.datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
        silent: dict[uuid.UUID, dt.datetime] = {}       # person -> when they stopped answering
        silent_people = set(rng.sample([d["person_id"] for d in devices], 20))
        silent_from = {p: start + dt.timedelta(hours=9 + (i % 10), minutes=7 * i % 60) for i, p in enumerate(sorted(silent_people, key=str))}
        answered = missed = 0
        now = start
        steps = 24 * 60 // step_min
        for s in range(steps + 1):
            now = start + dt.timedelta(minutes=step_min * s)
            # Watches report every step: steps and a heart rate, except the silent ones after their hour.
            readings: list[tuple] = []
            for d in devices:
                p = d["person_id"]
                moving = not (p in silent_from and now >= silent_from[p])
                readings.append((p, d["id"], "steps", float(rng.randint(5, 40)) if moving else 0.0, now))
                readings.append((p, d["id"], "heart_rate", float(rng.randint(60, 90)), now))
            async with workspace_connection(jp) as conn:
                await conn.executemany(
                    "INSERT INTO care_vitals (person_id, device_id, kind, value, unit, recorded_at, source) VALUES ($1, $2, $3, $4, '', $5, 'watch') "
                    "ON CONFLICT DO NOTHING", readings)
                await conn.execute("UPDATE care_devices SET last_sync_at = $1 WHERE person_id = ANY($2::uuid[])", now, [d["person_id"] for d in devices])
            t1 = time.perf_counter()
            await cycle.run_workspace(jp, now, rng=rng)
            timings["checkins"].append(time.perf_counter() - t1)
            # The watches collect and answer what is due (the silent ones do not).
            for d in devices:
                p = d["person_id"]
                due = await cycle.due_checkin(jp, d, now, rng=rng)
                if due is None:
                    continue
                if p in silent_from and now >= silent_from[p]:
                    silent.setdefault(p, now)
                    await cycle.mark_repeated(jp, d, due["id"], now + dt.timedelta(seconds=60))
                    # The real watch syncs a sensor snapshot right after a miss: no steps, a normal heart rate.
                    await repo.record_report(jp, d, {"readings": [
                        {"kind": "steps", "value": 0.0, "unit": "", "recorded_at": now + dt.timedelta(seconds=170)},
                        {"kind": "heart_rate", "value": 72.0, "unit": "bpm", "recorded_at": now + dt.timedelta(seconds=170)}],
                        "battery_percent": 70, "on_wrist": True, "background_sensor_permission": True})
                    await cycle.mark_missed(jp, d, due["id"], now + dt.timedelta(seconds=180))
                    missed += 1
                else:
                    await cycle.record_answer(jp, d, due["id"], rng.choice(["yes thanks", "I'm fine", "had my lunch"]), now=now + dt.timedelta(seconds=20))
                    answered += 1
            t2 = time.perf_counter()
            await escalation.run_workspace(jp, now + dt.timedelta(minutes=step_min - 1), provider=LogOnlyProvider())
            timings["monitoring"].append(time.perf_counter() - t2)
            if s % (60 // step_min) == 0:
                print(f"{now:%H:%M}  check-in pass {timings['checkins'][-1]:.2f}s  monitoring pass {timings['monitoring'][-1]:.2f}s  answered {answered} missed {missed}")

        # ── Did every silent person escalate? ──────────────────────────────
        async with workspace_connection(jp) as conn:
            alerts = await conn.fetch(
                "SELECT person_id, kind, status, current_tier, opened_at FROM care_alerts WHERE severity IN ('sos', 'urgent') AND person_id = ANY($1::uuid[])",
                list(silent_people))
        by_person = {}
        for a in alerts:
            by_person.setdefault(a["person_id"], []).append(a)
        missing = [p for p in silent_people if p not in by_person]
        late = []
        for p, rows in by_person.items():
            first = min(r["opened_at"] for r in rows)
            if first - silent[p] > dt.timedelta(minutes=step_min + 5):
                late.append((p, first - silent[p]))
        tiers_reached = [max(r["current_tier"] for r in rows) for rows in by_person.values()]
        report = {
            "people": people_n, "step_minutes": step_min, "steps": steps + 1,
            "checkins_answered": answered, "checkins_missed": missed,
            "silent_people": len(silent_people), "escalated": len(by_person), "missing_escalations": len(missing), "late_escalations": len(late),
            "tier_reached_min": min(tiers_reached) if tiers_reached else None,
            "checkin_pass_s": {"mean": round(statistics.mean(timings["checkins"]), 3), "p95": round(sorted(timings["checkins"])[int(0.95 * len(timings["checkins"]))], 3), "max": round(max(timings["checkins"]), 3)},
            "monitoring_pass_s": {"mean": round(statistics.mean(timings["monitoring"]), 3), "p95": round(sorted(timings["monitoring"])[int(0.95 * len(timings["monitoring"]))], 3), "max": round(max(timings["monitoring"]), 3)},
        }
        print(json.dumps(report, indent=2))
        (ROOT / ".demo").mkdir(exist_ok=True)
        (ROOT / ".demo" / "soak_report.json").write_text(json.dumps(report, indent=2) + "\n")
        ok = not missing and not late and all(t >= 1 for t in tiers_reached)
        print("RESULT:", "no missed escalations" if ok else "MISSED OR LATE ESCALATIONS")
        return 0 if ok else 1
    finally:
        await remove_family(admin_dsn, fam)
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=200)
    ap.add_argument("--step-minutes", type=int, default=5)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.people, a.step_minutes)))
