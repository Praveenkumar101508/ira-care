#!/usr/bin/env python3
"""Phase 4 batch 06 — expiring an append-only log, deliberately and on the record.

    python -m scripts.phase4.expire_auth_events status --dsn ... --days 365
    python -m scripts.phase4.expire_auth_events expire --dsn ... --days 365 \
        --export /secure/path/auth-events-2026.jsonl --confirm

`061` made `auth_events` append-only for EVERY role — no DELETE policy, DELETE
revoked from `ira_app`, and a trigger that raises whoever asks. That is right,
and `core/privacy/retention.py` therefore counts what is past retention and
refuses to touch it.

Refusing forever is not a retention policy either. A log that can only grow is
an unbounded personal-data store, and the way that ends is somebody running
`TRUNCATE` at 2am with no record of what went. This script is the alternative:
the same deletion, performed the same way twice, leaving evidence.

── Why it is a script and not a function in the application ────────────────

Anything callable by `ira_app` is callable by whoever holds `ira_app`, and the
first thing an intruder does is remove the entry that shows them arriving. `064`
adds no DELETE policy, no grant, and no SECURITY DEFINER wrapper for exactly
that reason. Deletion requires the table OWNER, which means an operator, a
separate credential, and an intent.

── The order, and why each step is where it is ────────────────────────────

  1. Count the window. Nothing else happens if it is empty.
  2. Export the rows to a file and FSYNC it.
  3. Read the file back and count its lines. A count that disagrees with the
     database aborts before anything is deleted — an export nobody verified is
     not a backup, and this is the same read-back-before-destroy rule the
     voiceprint worker follows.
  4. Disable the trigger, DELETE the window, re-enable the trigger — all inside
     ONE transaction, so a failure at any point rolls the table back to
     append-only rather than leaving it open.
  5. Write a `log_expired` row recording how many went and where the export
     lives, with `created_at = NOW()` so it sits outside the window it
     describes and cannot be removed by the pass that wrote it.
  6. Assert the trigger is enabled again, by reading `pg_trigger`. Not by
     assuming the ALTER ran — step 4 is the step whose failure matters most,
     and the check for it must not be the code that performed it.

── What the export contains ───────────────────────────────────────────────

Every column. `subject_hash` and `ip_hash` are already HMACs under the
application secret (`061` stores no address in the clear), and `detail` is
capped at 200 characters and carries no credential material by construction. No
password, token, recovery code or TOTP code has ever been written to this table.

The export is operator output. It belongs wherever the deployment keeps audit
archives — not in the repository, and not in an evidence directory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

TRIGGER = "auth_events_append_only_trg"


def _dsn(explicit: Optional[str]) -> str:
    dsn = explicit or os.getenv("IRA_ADMIN_DSN") or os.getenv("DATABASE_URL", "")
    if not dsn:
        raise SystemExit(
            "no DSN: pass --dsn, or set IRA_ADMIN_DSN. This must be a role that "
            "OWNS auth_events — ira_app cannot and must not be able to do this.")
    return dsn


async def _count_window(conn, days: int) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM auth_events "
        " WHERE created_at < NOW() - ($1 || ' days')::interval",
        str(days)) or 0)


async def trigger_state(conn) -> Optional[str]:
    """`pg_trigger.tgenabled` as a one-character string, or None if absent.

    `tgenabled` is PostgreSQL's `"char"` type and asyncpg hands it back as
    BYTES — `b'D'`, not `'D'`. Comparing the raw value to `"D"` is always false,
    so a check written the obvious way passes whether the trigger is enabled or
    disabled and reports an open audit log as a healthy one. This function is
    the single place that conversion happens.
    """
    raw = await conn.fetchval(
        "SELECT tgenabled FROM pg_trigger "
        " WHERE tgrelid = 'auth_events'::regclass AND tgname = $1", TRIGGER)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


async def _assert_trigger_enabled(conn) -> None:
    """Read the catalogue. The ALTER that re-enabled it is not evidence of
    itself — this is the assertion that the log is append-only again."""
    state = await trigger_state(conn)
    if state is None:
        raise SystemExit(
            f"FATAL: {TRIGGER} does not exist. auth_events is no longer "
            f"append-only. Restore it from 061 before using this system.")
    if state == "D":
        raise SystemExit(
            f"FATAL: {TRIGGER} is DISABLED. auth_events is writable and "
            f"deletable by anyone with the owner role. Re-enable it now:\n"
            f"  ALTER TABLE auth_events ENABLE TRIGGER {TRIGGER};")


async def cmd_status(args) -> int:
    import asyncpg

    conn = await asyncpg.connect(_dsn(args.dsn))
    try:
        await _assert_trigger_enabled(conn)
        total = int(await conn.fetchval("SELECT count(*) FROM auth_events") or 0)
        window = await _count_window(conn, args.days)
        oldest = await conn.fetchval("SELECT min(created_at) FROM auth_events")
        expiries = int(await conn.fetchval(
            "SELECT count(*) FROM auth_events WHERE event = 'log_expired'") or 0)
    finally:
        await conn.close()

    print(f"auth_events rows:            {total}")
    print(f"oldest:                      {oldest}")
    print(f"past {args.days} days:       {window}")
    print(f"previous expiry passes:      {expiries}")
    if window:
        print("\nnothing has been deleted. To expire, re-run with:")
        print("  expire --days N --export <path> --confirm")
    return 0


async def cmd_expire(args) -> int:
    import asyncpg

    if not args.confirm:
        raise SystemExit("refusing to delete audit history without --confirm")
    export = Path(args.export).expanduser()
    if export.exists():
        raise SystemExit(
            f"{export} already exists. Refusing to overwrite an audit export — "
            f"choose a new path rather than losing a previous one.")

    conn = await asyncpg.connect(_dsn(args.dsn))
    try:
        await _assert_trigger_enabled(conn)

        rows = await conn.fetch(
            "SELECT * FROM auth_events "
            " WHERE created_at < NOW() - ($1 || ' days')::interval "
            " ORDER BY created_at", str(args.days))
        if not rows:
            print(f"nothing is past {args.days} days. Nothing to do.")
            return 0

        # Step 2 and 3: write, fsync, read back, count. An export nobody
        # verified is not a backup, and this is the last moment at which the
        # rows still exist.
        export.parent.mkdir(parents=True, exist_ok=True)
        with export.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(
                    {k: (v.isoformat() if hasattr(v, "isoformat") else
                         str(v) if v is not None else None)
                     for k, v in dict(row).items()}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        written = sum(1 for _ in export.open(encoding="utf-8"))
        if written != len(rows):
            raise SystemExit(
                f"export holds {written} line(s) but {len(rows)} row(s) were "
                f"selected. NOTHING has been deleted. Investigate {export}.")
        print(f"exported {written} row(s) to {export}")

        # Step 4 and 5, in one transaction. If anything below raises, the
        # ALTER that disabled the trigger rolls back with everything else and
        # the table is append-only again without anybody intervening.
        async with conn.transaction():
            await conn.execute(f"ALTER TABLE auth_events DISABLE TRIGGER {TRIGGER}")
            result = await conn.execute(
                "DELETE FROM auth_events "
                " WHERE created_at < NOW() - ($1 || ' days')::interval",
                str(args.days))
            deleted = int(str(result).rsplit(" ", 1)[-1])
            if deleted != len(rows):
                raise SystemExit(
                    f"deleted {deleted} but exported {len(rows)}. Rolling back; "
                    f"the log is unchanged.")
            await conn.execute(f"ALTER TABLE auth_events ENABLE TRIGGER {TRIGGER}")
            # Written after the DELETE and with NOW(), so it falls outside the
            # window it describes. `detail` is capped at 200 by 061.
            await conn.execute(
                "INSERT INTO auth_events (event, detail, created_at) "
                "VALUES ('log_expired', $1, NOW())",
                f"expired {deleted} event(s) older than {args.days}d to "
                f"{export.name}"[:200])

        await _assert_trigger_enabled(conn)
        print(f"deleted {deleted} row(s); the log records the pass as 'log_expired'")
        print(f"the append-only trigger is enabled ({TRIGGER})")
        return 0
    finally:
        await conn.close()


def build_parser() -> argparse.ArgumentParser:
    """Separate from `main` so tests can reach the commands without
    `asyncio.run`, which cannot be called from inside a running loop."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="what is past retention")
    status.add_argument("--dsn")
    status.add_argument("--days", type=int, required=True)
    status.set_defaults(func=cmd_status)

    expire = sub.add_parser("expire", help="export then delete the window")
    expire.add_argument("--dsn")
    expire.add_argument("--days", type=int, required=True)
    expire.add_argument("--export", required=True,
                        help="where the exported rows go. Must not exist.")
    expire.add_argument("--confirm", action="store_true")
    expire.set_defaults(func=cmd_expire)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
