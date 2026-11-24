#!/usr/bin/env python3
"""Seed the IRA CARE demo organisation with one command.

    IRA_TEST_DATABASE_DSN=... IRA_TEST_ADMIN_DSN=... python scripts/demo/seed_demo.py [--reset]

What it creates follows docs/BUILD_RULES.md "Demonstration data": the care
organisation "Lowbridge Care"; Praveen Kamineti as the care coordinator and
Sravani Perni as family (the two people who build IRA CARE); two made-up people
being looked after, Ada Hartley and Bert Lowe, with example.com addresses,
07700 900xxx telephone numbers (the UK range reserved for fiction) and
addresses in a town that does not exist; routines, medication, a week of
readings and check-ins, one resolved alert and one report. Four accounts can
sign in: Praveen (dashboard and app, the carer's experience), Sravani (the
family experience), Ada (the wearer's own phone) and a read-only viewer. Their passwords and the device tokens are
generated afresh on every run, printed to the terminal and written for the
local scripts to `.demo/demo.local.json` (mode 0600, ignored by git). Nothing
in the repository can sign in to any seeded demo.

Idempotent: `--reset` (or a second run) removes the previous demo tenant first.
Nothing here is real health data, and no message leaves the machine.
"""
from __future__ import annotations

import argparse
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

from demo._local_state import fresh_password, prepare_environment, write_private

# A fresh sealing key on every seed: the notes it seals are reseeded with it.
prepare_environment(renew_key=True, edition="cloud", model="fake")

UTC = dt.UTC
SLUG = "demo-care-organisation"
DISPLAY = "Lowbridge Care"
ACCOUNTS = [
    # key, email, display name, tenant role, workspace role. Passwords are generated
    # on every run (fresh_password) and never written into the repository.
    ("coordinator", "praveen@example.com", "Praveen Kamineti", "owner", "workspace_admin"),
    ("family", "sravani@example.com", "Sravani Perni", "member", "member"),
    ("wearer", "ada@example.com", "Ada Hartley", "member", "member"),
    ("viewer", "viewer@example.com", "Care viewer", "member", "viewer"),
]
PEOPLE = [
    {"display_name": "Ada Hartley", "preferred_name": "Ada", "timezone": "Europe/London", "language": "en-GB",
     "home_address": "14 Orchard Close, Lowbridge, LB1 2AB", "home_latitude": 51.5, "home_longitude": -0.12,
     "emergency_summary": "Hard of hearing: speak clearly and face them. Uses a walking stick.",
     "access_notes": "Key safe left of the front door, code on the circle's record. Small friendly dog called Biscuit.",
     "routines": [("breakfast", "meal", "08:00"), ("lunch", "meal", "12:30"), ("afternoon walk", "activity", "15:00"), ("tea", "meal", "18:00")],
     "medications": [("the morning tablets", "one", ["08:00"]), ("the evening tablet", "one", ["20:00"])],
     # name, role, tier, phone, relationship, linked account (a key in ACCOUNTS or None)
     "circle": [("Ada Hartley", "family", 3, "+447700900000", "self", "wearer"),
                ("Sravani Perni", "family", 1, "+447700900001", "daughter", "family"),
                ("Praveen Kamineti", "carer", 1, "+447700900002", "care coordinator", "coordinator"),
                ("Lowbridge Care desk", "care_organisation", 2, "+447700900003", "", None)]},
    {"display_name": "Bert Lowe", "preferred_name": "Bert", "timezone": "Europe/London", "language": "en-GB",
     "home_address": "Flat 3, Mill House, Lowbridge, LB1 4QT", "home_latitude": 51.52, "home_longitude": -0.1,
     "emergency_summary": "", "access_notes": "Door code with the warden. No pets.",
     "routines": [("breakfast", "meal", "09:00"), ("lunch", "meal", "13:00")],
     "medications": [("the blue tablet", "one", ["09:00", "21:00"])],
     "circle": [("Sravani Perni", "family", 1, "+447700900001", "niece", "family"),
                ("Praveen Kamineti", "carer", 1, "+447700900002", "care coordinator", "coordinator"),
                ("Lowbridge Care desk", "care_organisation", 2, "+447700900003", "", None)]},
]


def principal_for(user_id, tenant_id, workspace_id, role):
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal
    return Principal(user_id=user_id, tenant_id=tenant_id, session_id=uuid.uuid4(), edition=EDITION_CLOUD,
                     workspace_id=workspace_id, workspace_role=role)


async def remove_previous(admin) -> None:
    rows = await admin.fetch("SELECT id FROM tenants WHERE slug = $1", SLUG)
    for r in rows:
        # The care access log keeps RESTRICT on the tenant: remove the demo's rows first (schema owner).
        await admin.execute("DELETE FROM care_access_log WHERE tenant_id = $1", r["id"])
        for _key, email, *_ in ACCOUNTS:
            # Accounts are retired, not deleted: token and audit rows keep their foreign keys.
            await admin.execute(
                "UPDATE users SET email = 'retired+' || id::text || '@example.com', email_verified_at = NULL, "
                "deleted_at = NOW(), status = 'deleted' WHERE email = $1 AND deleted_at IS NULL", email)
        await admin.execute("DELETE FROM tenants WHERE id = $1", r["id"])
        print(f"removed the previous demo tenant {r['id']}")


async def seed(dsn: str, admin_dsn: str, out: Path) -> dict:
    import asyncpg
    import utils.db as db_module
    from api.middleware.auth import hash_password
    from care import checkin_cycle as cycle
    from care import repository as repo
    from core.identity.db_context import workspace_connection

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    db_module._pool = pool
    admin = await asyncpg.connect(admin_dsn)
    try:
        await remove_previous(admin)
        tenant_id, org_id, ws_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await admin.execute(
            "INSERT INTO tenants (id, name, slug, display_name, edition, status) VALUES ($1, $2, $2, $3, 'cloud', 'active')",
            tenant_id, SLUG, DISPLAY)
        users, passwords = {}, {}
        for key, email, name, trole, wrole in ACCOUNTS:
            uid = uuid.uuid4()
            password = passwords[email] = fresh_password()
            await admin.execute(
                "INSERT INTO users (id, email, email_verified_at, display_name, auth_provider, status) "
                "VALUES ($1, $2, NOW(), $3, 'password', 'active')", uid, email, name)
            await admin.execute("INSERT INTO user_credentials (user_id, password_hash) VALUES ($1, $2)", uid, hash_password(password))
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at) VALUES ($1, $2, $3, 'active', NOW())",
                tenant_id, uid, trole)
            users[key] = (uid, wrole)
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenant_id))
            await conn.execute("SELECT set_config('app.user_id', $1, false)", str(users["coordinator"][0]))
            await conn.execute("INSERT INTO organisations (id, tenant_id, name, slug) VALUES ($1, $2, $3, $4)", org_id, tenant_id, DISPLAY, f"org-{SLUG}")
            await conn.execute(
                "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default, status) "
                "VALUES ($1, $2, $3, 'Lowbridge care team', 'lowbridge-care-team', TRUE, 'active')", ws_id, tenant_id, org_id)
            for uid, wrole in users.values():
                await conn.execute(
                    "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) VALUES ($1, $2, $3, $4, 'active')",
                    tenant_id, ws_id, uid, wrole)
            for k in ("app.tenant_id", "app.user_id"):
                await conn.execute(f"SELECT set_config('{k}', '', false)")

        manager = principal_for(users["coordinator"][0], tenant_id, ws_id, "workspace_admin")
        jp = cycle.job_principal(tenant_id, ws_id)
        rng = random.Random(42)
        now = dt.datetime.now(UTC)
        out_people = []
        for spec in PEOPLE:
            person = await repo.create_person(manager, {k: spec[k] for k in (
                "display_name", "preferred_name", "timezone", "language", "home_address", "home_latitude",
                "home_longitude", "emergency_summary", "access_notes")})
            pid = person["id"]
            for name, role, tier, phone, rel, link in spec["circle"]:
                await repo.add_member(manager, pid, {"display_name": name, "role": role, "tier": tier, "phone": phone,
                                                     "relationship": rel, "user_id": users[link][0] if link else None,
                                                     "sms_enabled": True, "call_enabled": True, "push_enabled": True})
            for name, kind, at in spec["routines"]:
                await repo.add_routine(manager, pid, {"name": name, "kind": kind, "time_of_day": dt.time.fromisoformat(at), "days_of_week": [1, 2, 3, 4, 5, 6, 7], "active": True})
            for name, dose, times in spec["medications"]:
                await repo.add_medication(manager, pid, {"name": name, "dose": dose, "times": [dt.time.fromisoformat(t) for t in times], "notes": "", "active": True})
            settings = await repo.get_settings(manager, pid)
            body = {k: settings[k] for k in repo._SETTINGS_FIELDS}
            body.update(heart_rate_min=45, heart_rate_max=130)
            await repo.put_settings(manager, pid, body)
            created = await repo.create_device(manager, pid, {"platform": "wearos"})
            paired = await repo.pair_device(created["pairing_code"], app_version="demo-seed")
            device = dict(paired); device["person_id"] = pid; device["id"] = paired["id"]
            # A week of ordinary readings and answered check-ins, obviously synthetic.
            await history(conn_factory=workspace_connection, jp=jp, device=device, pid=pid, now=now, rng=rng, repo=repo)
            out_people.append({"id": str(pid), "display_name": spec["display_name"], "device_token": paired["device_token"],
                               "home_latitude": spec["home_latitude"], "home_longitude": spec["home_longitude"]})
        result = {"tenant_id": str(tenant_id), "workspace_id": str(ws_id), "organisation": DISPLAY,
                  "manager_user_id": str(users["coordinator"][0]),
                  "logins": [{"key": k, "name": n, "email": e, "password": passwords[e], "role": w} for k, e, n, _t, w in ACCOUNTS], "people": out_people}
        write_private(out, json.dumps(result, indent=2) + "\n")
        return result
    finally:
        await admin.close()
        await pool.close()


async def history(*, conn_factory, jp, device, pid, now, rng, repo) -> None:
    """Seven days: readings every 20 minutes in the day, check-ins answered, one
    missed check-in that became a resolved alert, and yesterday's report."""
    from care import reports
    readings = []
    for day in range(7, 0, -1):
        base = (now - dt.timedelta(days=day)).replace(hour=7, minute=0, second=0, microsecond=0)
        for step in range(15 * 3):   # 07:00 to 22:00, every 20 minutes
            t = base + dt.timedelta(minutes=20 * step)
            readings.append({"kind": "heart_rate", "value": round(rng.uniform(62, 88), 1), "unit": "bpm", "recorded_at": t})
            readings.append({"kind": "steps", "value": float(rng.randint(0, 60)), "unit": "", "recorded_at": t})
    await repo.record_report(jp, device, {"readings": readings[:2000], "battery_percent": 82, "on_wrist": True, "background_sensor_permission": True})
    if len(readings) > 2000:
        await repo.record_report(jp, device, {"readings": readings[2000:], "battery_percent": 82, "on_wrist": True, "background_sensor_permission": True})
    summaries = ["Said they were doing well.", "Said they had eaten.", "Said they had taken the morning tablets.",
                 "Answered about their plans.", "Said they slept well.", "Answered about how they feel. (sounded low)"]
    kinds = ["wellbeing", "meal", "medication", "plans", "mood", "mood"]
    async with conn_factory(jp) as conn:
        for day in range(7, 0, -1):
            base = (now - dt.timedelta(days=day)).replace(hour=8, minute=30, second=0, microsecond=0)
            for i in range(5):
                asked = base + dt.timedelta(hours=2 * i + rng.randint(0, 40) / 60)
                k = rng.randrange(len(kinds))
                status = "missed" if (day == 3 and i == 2) else "answered"
                await conn.execute(
                    "INSERT INTO care_checkins (person_id, scheduled_at, asked_at, status, question_kind, question_text, "
                    "answered_at, answer_summary, answered_via) VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8)",
                    pid, asked, status, kinds[k], f"Hello. A demo question about {kinds[k]}?",
                    asked + dt.timedelta(seconds=25) if status == "answered" else None,
                    summaries[k] if status == "answered" else "No answer.", "voice" if status == "answered" else "none")
        opened = (now - dt.timedelta(days=3)).replace(hour=12, minute=45, second=0, microsecond=0)
        await conn.execute(
            "INSERT INTO care_alerts (person_id, kind, severity, status, current_tier, opened_at, acknowledged_at, resolved_at, "
            "resolution, details) VALUES ($1, 'no_answer_no_movement', 'sos', 'resolved', 1, $2, $3, $4, $5, $6::jsonb)",
            pid, opened, opened + dt.timedelta(minutes=4), opened + dt.timedelta(minutes=30),
            "Checked in person; had fallen asleep in the chair. All well.",
            json.dumps({"summary": "No answer to the check-in and no movement for 3 minutes.", "demo": True}))
        person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", pid))
        await reports.generate(conn, person, reports.DAILY, (now - dt.timedelta(days=1)).date())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reset", action="store_true", help="remove the previous demo tenant first (also the default behaviour)")
    ap.add_argument("--out", default=str(ROOT / ".demo" / "demo.local.json"))
    args = ap.parse_args()
    dsn, admin_dsn = os.getenv("IRA_TEST_DATABASE_DSN", ""), os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not dsn or not admin_dsn:
        print("Set IRA_TEST_DATABASE_DSN (the ira_app role) and IRA_TEST_ADMIN_DSN (a privileged role) to a database "
              "with the migration chain applied. The demo seeds a tenant of its own and never touches other tenants.", file=sys.stderr)
        return 2
    result = asyncio.run(seed(dsn, admin_dsn, Path(args.out)))
    print(json.dumps({k: v for k, v in result.items() if k not in ("people", "logins")}, indent=2))
    print("\nLogins (generated for this run only):")
    for login in result["logins"]:
        print(f"  {login['name']:18} {login['role']:16} {login['email']}  password {login['password']}")
    for p in result["people"]:
        print(f"{p['display_name']}: device token {p['device_token']}")
    print(f"\nWritten for the local scripts to {args.out} (mode 0600, inside the ignored .demo/ directory). All of it is fake.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
