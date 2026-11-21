#!/usr/bin/env python3
"""Phase 3B Batch 1 — migration status, quarantine and contract verification.

SQL cannot encrypt. `036` and `037` add the destination columns and set
`migration_status = 'PLAINTEXT'`, which is the honest starting state. This tool
reports on that state, names the rows that cannot proceed, and gates the
contract stage.

    python migrate_secrets.py status   --dsn ...
    python migrate_secrets.py dry-run  --dsn ...
    python migrate_secrets.py verify   --dsn ...
    python migrate_secrets.py quarantine --dsn ... [--json out.json]

── There is deliberately no `encrypt` command ───────────────────────────────

An earlier draft of this file had one. It was removed rather than fixed,
because two things were wrong with it and only one was a bug.

The bug: it called `keyring.encrypt_for_migration`, which does not exist. The
call sat inside a broad `except Exception` that marks the row `FAILED`, so
running it would have marked **every** row FAILED — quietly, with no traceback,
because the handler deliberately does not log the exception (it can contain the
plaintext). A tool that reports "0 encrypted, N failed" for a reason it refuses
to print is worse than no tool.

The design gap, which is the real reason: `core/connectors/keyring.py` mints
data-encryption keys **per tenant**. A TOTP secret and a voiceprint are
USER_PRIVATE (`036`, `037`) — they belong to a person, and a person may belong
to several tenants or leave the one they enrolled in. Sealing them under a
tenant DEK would mean deleting a tenant cryptographically destroys its former
members' second factors, and it would put a key a tenant administrator's
erasure can reach around data that administrator must never be able to read.

Choosing between a per-user key purpose and a per-tenant one is a key-management
decision with a rotation, escrow and recovery story attached. It is recorded as
an open decision, and the encryption worker is written after it is made — not
inside the batch whose migrations state in their own headers that they do not
encrypt anything.

Until then `migration_status` stays `PLAINTEXT`, `contract_ready` stays false,
and no part of this system claims these secrets are encrypted at rest.

── Safety properties, each deliberate ───────────────────────────────────────

**No secret is ever printed.** Not in output, not in the quarantine report, not
in an exception message. Every command reports counts and identifiers only.
`--verbose` does not change that.

**Read-only.** No command here writes to any table. `status`, `dry-run`,
`verify` and `quarantine` all report; nothing mutates a secret row. An earlier
draft carried a `--i-am-authorised-to-migrate-production` flag; with the writing
command gone it guarded nothing, and a flag that guards nothing teaches people
that the flags here are ceremony.

**Refuses a role that cannot see everything.** Every number printed is a count,
and a count taken through row-level security is a count of what the connected
role happens to see. `verify` gates the migration that drops the plaintext
columns, so a filtered read would report the schema ready to drop plaintext that
is merely invisible. See `refuse_if_the_role_cannot_see_everything`.

**Non-zero exit on unresolved rows.** `verify` fails the build while any secret
row lacks an owner or remains plaintext, which is what makes it usable as a
contract gate.

**No credential path.** This module does not import `core/connectors/secrets`
or `core/connectors/keyring`, and `tests/architecture/test_credential_boundary.py`
enforces that. Reporting on how many rows are still plaintext requires no key
material, so it takes none.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

TABLES = {
    "totp": {
        "table": "totp_secrets",
        "plaintext": "secret",
        "ciphertext": "secret_ciphertext",
        "view": "totp_migration_status",
        "label": "TOTP secret",
    },
    "voice": {
        "table": "voice_profiles",
        "plaintext": "embedding",
        "ciphertext": "template_ciphertext",
        "view": "voice_migration_status",
        "label": "voiceprint template",
    },
}


def redact_dsn(dsn: str) -> str:
    """Strip credentials from a DSN so it is safe to print."""
    import re
    return re.sub(r"(?P<scheme>[a-z+]+://)(?P<user>[^:@/]*):[^@/]*@", r"\g<scheme>\g<user>:***@", dsn)


async def connect(dsn: str):
    try:
        import asyncpg
    except ImportError:
        sys.exit("asyncpg is required: pip install asyncpg")
    return await asyncpg.connect(dsn)


#: Tables this tool must be able to read in full for its counts to mean anything.
MUST_SEE = ("totp_secrets", "voice_profiles", "legacy_identity_map")


async def refuse_if_the_role_cannot_see_everything(conn) -> None:
    """Refuse to report at all unless the connected role reads unfiltered rows.

    Every number this tool prints is a count, and a count taken through a
    row-level security policy is silently a count of what the connected role can
    see. That failure mode is specific and severe here:

      * `legacy_identity_map` is DENY-ALL (034) — RLS on, no policy — so the
        application role reads **zero** rows and `quarantine` would report no
        unresolved usernames at all;
      * `totp_secrets` and `voice_profiles` are USER_PRIVATE (036, 037), keyed
        on `app.user_id`, which this tool never sets — so both would report
        zero rows, zero plaintext, and `contract_ready = true`.

    That last one is the whole point. `verify` exists to gate the contract stage
    — the migration that drops the plaintext columns. Run under the application
    role it would report the schema ready to have its plaintext dropped while
    every secret was still plaintext and merely invisible.

    **An earlier version of this check tested the wrong property.** It required
    the connected role to OWN the tables, on the reasoning that an owner is
    exempt from its own policies. That is only true while `FORCE ROW LEVEL
    SECURITY` is off, and all three of these tables force it — precisely so the
    owner cannot bypass them. So the check passed for a role that could not see
    a single row.

    The property that actually matters is "not subject to row-level security at
    all": superuser, or `BYPASSRLS`. Ownership is neither necessary nor
    sufficient.
    """
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])",
        list(MUST_SEE),
    )
    found = {r["relname"] for r in rows}
    missing = [t for t in MUST_SEE if t not in found]
    if missing:
        sys.exit(f"REFUSING: {', '.join(missing)} not found — is the migration "
                 f"chain applied to this database?")

    role = await conn.fetchrow(
        "SELECT current_user AS me, rolsuper, rolbypassrls "
        "  FROM pg_roles WHERE rolname = current_user")
    if not (role["rolsuper"] or role["rolbypassrls"]):
        sys.exit(
            f"REFUSING: connected as {role['me']!r}, which is subject to row-level "
            f"security. Every count this tool prints would be a count of what that "
            f"role happens to see, and `verify` would report contract_ready on "
            f"secrets it simply cannot read. Owning the tables is NOT enough: "
            f"totp_secrets and voice_profiles set FORCE ROW LEVEL SECURITY, which "
            f"binds the owner too. Connect as a superuser or a role with BYPASSRLS."
        )


# ── Commands ─────────────────────────────────────────────────────────────────

async def cmd_status(conn, args) -> int:
    for key, spec in TABLES.items():
        row = await conn.fetchrow(f"SELECT * FROM {spec['view']}")
        print(f"\n{spec['label']} ({spec['table']}):")
        for k, v in dict(row).items():
            print(f"  {k:22} {v}")
    return 0


async def cmd_dry_run(conn, args) -> int:
    """What the encryption worker would face, once there is one. Changes nothing."""
    rc = 0
    for key, spec in TABLES.items():
        t = spec["table"]
        total = await conn.fetchval(f"SELECT count(*) FROM {t}")
        unresolved = await conn.fetchval(f"SELECT count(*) FROM {t} WHERE user_id IS NULL")
        pending = await conn.fetchval(
            f"SELECT count(*) FROM {t} WHERE migration_status = 'PLAINTEXT' AND user_id IS NOT NULL")
        blocked = await conn.fetchval(
            f"SELECT count(*) FROM {t} WHERE migration_status = 'PLAINTEXT' AND user_id IS NULL")
        print(f"\n{spec['label']} ({t}):")
        print(f"  rows total                 {total}")
        print(f"  would encrypt              {pending}")
        print(f"  BLOCKED (no resolved owner) {blocked}")
        print(f"  unresolved owners          {unresolved}")
        if blocked:
            print("  -> these rows are NOT encrypted and NOT guessed. See `quarantine`.")
            rc = 1
    return rc


async def cmd_quarantine(conn, args) -> int:
    """Rows that cannot be migrated safely, by reason. No secret values."""
    report: dict = {"generated_for": redact_dsn(args.dsn), "reasons": {}, "tables": {}}

    rows = await conn.fetch(
        "SELECT resolution, count(*) AS n FROM legacy_identity_map "
        "WHERE resolution <> 'MAPPED' GROUP BY resolution ORDER BY resolution")
    for r in rows:
        report["reasons"][r["resolution"]] = r["n"]

    for key, spec in TABLES.items():
        t = spec["table"]
        unresolved = await conn.fetch(
            f"SELECT id, migration_status FROM {t} WHERE user_id IS NULL ORDER BY id")
        report["tables"][t] = {
            "unresolved_owner": len(unresolved),
            # Row IDs only — never the owner string, never the secret.
            "row_ids": [str(r["id"]) for r in unresolved[:200]],
            "encryption_failed": await conn.fetchval(
                f"SELECT count(*) FROM {t} WHERE migration_status = 'FAILED'"),
        }

    text = json.dumps(report, indent=1, sort_keys=True)
    if args.json:
        open(args.json, "w").write(text + "\n")
        print(f"quarantine report written to {args.json}")
    else:
        print(text)

    blocking = sum(v["unresolved_owner"] + v["encryption_failed"] for v in report["tables"].values())
    if blocking:
        print(f"\n{blocking} row(s) block the contract stage.", file=sys.stderr)
        return 1
    return 0


async def cmd_verify(conn, args) -> int:
    """Contract-readiness. Non-zero while anything is unresolved or plaintext."""
    rc = 0
    vacuous = []
    for key, spec in TABLES.items():
        row = await conn.fetchrow(f"SELECT * FROM {spec['view']}")
        ready = row["contract_ready"]
        # `contract_ready` is a pair of `count(...) = 0` tests, so it is TRUE on
        # an empty table — there is nothing unresolved and nothing plaintext
        # because there is nothing. That is a correct answer to "does anything
        # block the contract", and a dangerously misleading answer to the
        # question a reader actually asks, which is "did the encryption run".
        # Saying which one it is costs a word and removes the ambiguity.
        empty = row["total_rows"] == 0
        if empty:
            vacuous.append(spec["table"])
        print(f"{spec['table']:16} contract_ready={ready}"
              f"{' (VACUOUS — no rows)' if empty else ''} "
              f"total={row['total_rows']} "
              f"unresolved={row['unresolved_owner']} plaintext={row['still_plaintext']} "
              f"failed={row['encryption_failed']}")
        if not ready:
            rc = 1

    orphans = await conn.fetchval(
        "SELECT count(*) FROM legacy_identity_map WHERE resolution <> 'MAPPED'")
    print(f"{'identity map':16} unresolved_usernames={orphans}")

    if rc != 0:
        print("\nNOT CONTRACT READY", file=sys.stderr)
    elif vacuous:
        print(f"\nCONTRACT READY — but {', '.join(vacuous)} hold no rows, so this "
              f"says nothing about whether any secret was ever encrypted.",
              file=sys.stderr)
    else:
        print("\nCONTRACT READY", file=sys.stderr)
    return rc


COMMANDS = {
    "status": cmd_status, "dry-run": cmd_dry_run,
    "verify": cmd_verify, "quarantine": cmd_quarantine,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--dsn", required=True, help="PostgreSQL DSN. Never logged unredacted.")
    p.add_argument("--json", help="write the report to this path")
    args = p.parse_args()

    print(f"# database: {redact_dsn(args.dsn)}", file=sys.stderr)

    async def run() -> int:
        conn = await connect(args.dsn)
        try:
            await refuse_if_the_role_cannot_see_everything(conn)
            return await COMMANDS[args.command](conn, args)
        finally:
            await conn.close()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
