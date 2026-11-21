"""Copy every token version from PostgreSQL back to Redis, before rolling 096 back.

Migration 096 moved the per-account token version ("log out everywhere") from
Redis to `auth_token_versions`. Rolling it back puts Redis in charge again, and
Redis holds whatever it held before: for an account whose key was lost, 0. Every
token issued before that account's last "log out everywhere" would work again.
This script prevents that. Run it, then
`postgres/rollback/096_auth_token_versions_down.sql`, then revert the code.

── Usage ────────────────────────────────────────────────────────────────────

    # See what it would write, without writing
    python supracloud-jarvis/scripts/token_versions_to_redis.py --dry-run

    # Write
    python supracloud-jarvis/scripts/token_versions_to_redis.py

Each row is written as `ira:token_ver:<name>`, where the name is the subject the
account's tokens carry: `external_subject` in the private edition, and
`normalised_email` in the cloud. A key that already holds a higher value keeps
it: this never lowers a counter. Rows whose account has no name to key by are
listed and skipped, and the exit code is 1 if there were any.

Requires the application's database DSN and Redis settings, exactly as the
application does.

Each counter is read in a transaction bound to its own account
(`app.user_id`), because 096's policies admit nothing else: no IRA role
bypasses row-level security, so an unbound read of the table sees no rows at
all. Read that way, this script would copy nothing and report success, and the
rollback would lose every counter.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ira"))

# `users` carries no RLS; the counters do, one account per transaction.
_ACCOUNTS_SQL = "SELECT id, external_subject, normalised_email FROM users ORDER BY id"
_COUNTER_SQL = "SELECT version FROM auth_token_versions WHERE user_id = $1"


async def read_counters(conn) -> list[dict]:
    """Every account's counter, each read bound to its own account."""
    rows = []
    for account in await conn.fetch(_ACCOUNTS_SQL):
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id', $1, true)",
                               str(account["id"]))
            version = await conn.fetchval(_COUNTER_SQL, account["id"])
        if version is not None:
            rows.append({"user_id": account["id"], "version": int(version),
                         "external_subject": account["external_subject"],
                         "normalised_email": account["normalised_email"]})
    return rows


async def _run(dry_run: bool) -> int:
    import asyncpg

    from config import get_settings
    from core.editions import is_cloud
    from utils.redis_client import get_redis, init_redis

    cfg = get_settings()
    conn = await asyncpg.connect(cfg.database_dsn)
    try:
        rows = await read_counters(conn)
    finally:
        await conn.close()

    await init_redis()
    return await copy_to_redis(rows, get_redis(), cloud=is_cloud(), dry_run=dry_run)


async def copy_to_redis(rows, client, *, cloud: bool, dry_run: bool) -> int:
    """Write each counter to Redis unless Redis already holds as much."""
    from core.identity.token_versions import REDIS_PREFIX

    written = kept = 0
    unnamed: list[str] = []
    for row in rows:
        name = row["normalised_email"] if cloud else row["external_subject"]
        if not name:
            unnamed.append(str(row["user_id"]))
            continue
        key = f"{REDIS_PREFIX}{name}"
        existing = await client.get(key)
        try:
            existing_value = int(existing) if existing is not None else 0
        except (TypeError, ValueError):
            existing_value = 0
        if existing_value >= row["version"]:
            kept += 1
            continue
        if not dry_run:
            await client.set(key, int(row["version"]))
        written += 1
        print(f"{'would write' if dry_run else 'wrote'} {key} = {row['version']}"
              f" (was {existing_value})")

    print(f"{len(rows)} row(s): {written} {'to write' if dry_run else 'written'}, "
          f"{kept} already as high in Redis, {len(unnamed)} without a name")
    for user_id in unnamed:
        print(f"  skipped {user_id}: no subject to key it by", file=sys.stderr)
    return 1 if unnamed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, and write nothing")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
