#!/usr/bin/env python3
"""The migration command. **The only thing that applies migrations.**

    python -m scripts.migrate status --dsn <migrator-dsn>
    python -m scripts.migrate apply  --dsn <migrator-dsn> [--i-am-authorised-to-migrate-production]
    python -m scripts.migrate verify --dsn <any-dsn>

Why this exists
---------------

The application used to apply migrations on every boot, through its ordinary
connection pool. That single line forced the runtime role to own every table —
DDL requires ownership — so the role serving requests could also
`ALTER TABLE … NO FORCE ROW LEVEL SECURITY`, drop a policy, or hand itself
ownership of anything.

`FORCE ROW LEVEL SECURITY` constrained the application's *queries* and not the
application's *role*. Every isolation guarantee on this branch rested on that
distinction without stating it. D-18 and R-50 record it; this command is half of
the fix, and `042_role_ownership_separation.sql` is the other half.

Credential handling
-------------------

The migrator DSN must **not** live in ordinary runtime application
configuration. It is a deployment-time credential: a CI secret, a deploy-job
environment variable, or a short-lived token from a secrets manager. If the API
container can read it, the separation is decorative.

No DSN is ever printed unredacted, and no password appears in any output or
exception message raised by this module.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys


def redact_dsn(dsn: str) -> str:
    """Strip credentials from a DSN so it is safe to print."""
    return re.sub(
        r"(?P<scheme>[a-z+]+://)(?P<user>[^:@/]*):[^@/]*@",
        r"\g<scheme>\g<user>:***@",
        dsn,
    )


async def _connect(dsn: str):
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - deployment environment
        sys.exit("asyncpg is required: pip install asyncpg")
    return await asyncpg.connect(dsn)


async def cmd_status(conn, args) -> int:
    from utils.migrations import migration_status

    st = await migration_status(conn)
    print(f"applied {st['applied']}/{st['total']}")
    if st["pending"]:
        print("pending:")
        for name in st["pending"]:
            print(f"  {name}")
    # Non-zero while anything is pending, so a deploy pipeline can gate on it.
    return 0 if st["up_to_date"] else 1


async def cmd_apply(conn, args) -> int:
    from utils.migrations import MigrationRefused, apply_migrations

    try:
        result = await apply_migrations(
            conn, authorised_for_production=args.i_am_authorised_to_migrate_production
        )
    except MigrationRefused as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2

    print(f"role={result['role']} database={result['database']} "
          f"environment={result['environment']}")
    if result["applied"]:
        for name in result["applied"]:
            print(f"  applied {name}")
    else:
        print("  nothing to apply")
    return 0


async def cmd_verify(conn, args) -> int:
    """Post-migration schema verification. Safe from any role.

    Checks the properties a migration could plausibly leave broken, rather than
    re-stating what the migration said it did:

      * every pending migration is applied;
      * no table was left with `FORCE ROW LEVEL SECURITY` off after a backfill
        window (`035` section 0) — the failure mode that would silently let the
        schema owner bypass a policy;
      * the runtime roles own nothing.
    """
    from utils.migrations import migration_status

    rc = 0
    st = await migration_status(conn)
    print(f"migrations: applied {st['applied']}/{st['total']}")
    if st["pending"]:
        print(f"  FAIL: {len(st['pending'])} pending", file=sys.stderr)
        rc = 1

    unforced = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "   AND c.relrowsecurity AND NOT c.relforcerowsecurity "
        "   AND c.relname <> 'legacy_identity_map'"
    )
    if unforced:
        print(f"  FAIL: RLS enabled but not forced on "
              f"{[r['relname'] for r in unforced]} — a backfill window was not "
              f"closed, and the schema owner can bypass those policies",
              file=sys.stderr)
        rc = 1
    else:
        print("force rls: restored everywhere (legacy_identity_map excepted by design)")

    owned = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','S','p') "
        "   AND pg_get_userbyid(c.relowner) IN ('ira_app','ira_worker')"
    )
    if owned:
        print(f"  FAIL: runtime roles own {[r['relname'] for r in owned]} — D-18 reopened",
              file=sys.stderr)
        rc = 1
    else:
        print("ownership: no application object is owned by a runtime role")

    print("\nSCHEMA VERIFIED" if rc == 0 else "\nSCHEMA NOT VERIFIED", file=sys.stderr)
    return rc


COMMANDS = {"status": cmd_status, "apply": cmd_apply, "verify": cmd_verify}


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--dsn", required=True, help="PostgreSQL DSN. Never logged unredacted.")
    p.add_argument(
        "--i-am-authorised-to-migrate-production",
        action="store_true",
        dest="i_am_authorised_to_migrate_production",
        help="required when IRA_ENVIRONMENT is staging or production",
    )
    args = p.parse_args()

    print(f"# database: {redact_dsn(args.dsn)}", file=sys.stderr)

    async def run() -> int:
        conn = await _connect(args.dsn)
        try:
            return await COMMANDS[args.command](conn, args)
        finally:
            await conn.close()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
