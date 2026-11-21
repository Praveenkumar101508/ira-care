#!/usr/bin/env python3
"""Migrate stored TOTP seeds between plaintext and envelope encryption (P2-05).

`036` added `secret_ciphertext`, `encryption_key_version`, `encrypted_at` and
`migration_status` to `totp_secrets` and left every one of them unused, so the
column that actually held the second factor was `secret` — plaintext, in the
table, for every enrolled user. `053` adds the missing nonce and the key
material. This is the worker that moves the rows.

── Usage ────────────────────────────────────────────────────────────────────

    # What would move, without moving it
    python scripts/phase2/mfa_migrate.py --all --dry-run

    # Encrypt one tenant, then all of them
    python scripts/phase2/mfa_migrate.py --tenant <uuid>
    python scripts/phase2/mfa_migrate.py --all

    # Reverse it — required before rollback/053, which refuses while any row
    # is ENCRYPTED because dropping user_mfa_keys would strand those seeds
    python scripts/phase2/mfa_migrate.py --all --decrypt

── The order, which is the whole design ─────────────────────────────────────

Per row, encrypting:

  1. **Seal the plaintext and write the ciphertext**, leaving `secret` in
     place. Status becomes `ENCRYPTING`. The row now holds both forms, which
     `totp_secrets_has_a_secret` permits and `totp_secrets_encrypted_is_complete`
     tolerates precisely because the status is not yet `ENCRYPTED`.
  2. **Read the ciphertext back and decrypt it**, and compare with the
     plaintext. Not the in-memory value that was just sealed — the bytes that
     reached the table, opened by the same path `/auth/token` will use. A
     verification that checks what it just computed verifies nothing about
     what was stored.
  3. **Only then clear the plaintext** and set `ENCRYPTED`.

Plaintext is never cleared until ciphertext has been written AND proven to
open. An interruption at any point leaves a row that still works:

  * after 1, the row holds both forms and the login path prefers the
    ciphertext — which was not yet verified, so a re-run re-seals from the
    plaintext that is still there;
  * after 2, identical, and the re-run reaches 3;
  * after 3, the row is done and the re-run skips it.

Re-running is always safe, which is the only property that makes a partial
migration recoverable rather than a thing to be reasoned about.

── Why it walks users one at a time ─────────────────────────────────────────

`totp_secrets` policies require BOTH `app.tenant_id` and `app.user_id` to
match (`052`), and `user_mfa_keys` the same (`053`). There is deliberately no
tenant-wide read of either — that is the property that keeps a tenant
administrator away from a member's second factor. So there is no query that
returns "every row needing migration", and a worker that tried would get zero
rows back with no error, which is the failure `rotate_connector_keys.py`
documents at length.

Candidates therefore come from `tenant_memberships` (readable under a tenant
binding), plus — when the deployment holds exactly one tenant — every row in
`users`, which carries no RLS. That union is not an approximation: it is
exactly the derivation `052` used to assign `tenant_id` in the first place, so
any row the backfill could attribute, this can find.

── The one blind spot, stated rather than left to be discovered ─────────────

`totp_secrets.user_id` is still NULLABLE. A row with a NULL `user_id` matches
no policy under any binding, so this worker cannot see it — and neither can
the login path, which means it is already a dead row that authenticates
nobody. It is reported as unreachable rather than silently skipped. `--census`
counts them, using a read that touches no secret column.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Run from anywhere: the package root is this file's great-grandparent.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _principal(tenant: uuid.UUID, user: uuid.UUID):
    """A principal bound to one user, for one row's migration.

    The user id is the row's own, not a derived operator identity, because the
    RLS policies on both tables predicate on `app.user_id` — an operator
    principal would match nothing at all.
    """
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=user,
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
    )


async def _candidates(pool, tenant: uuid.UUID) -> list[uuid.UUID]:
    """Every user in `tenant` that might hold an enrolment. See the header."""
    from core.identity.db_context import tenant_connection

    probe = _principal(tenant, uuid.uuid4())
    async with tenant_connection(probe) as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM tenant_memberships WHERE tenant_id = $1", tenant
        )
    users = {r["user_id"] for r in rows}

    # `tenants` carries no RLS, so this count is honest on any connection.
    if int(await pool.fetchval("SELECT count(*) FROM tenants") or 0) == 1:
        users.update(
            r["id"] for r in await pool.fetch("SELECT id FROM users")
        )
    return sorted(users)


async def _rows_for(principal) -> list[dict]:
    """This user's enrolment rows, under their own binding."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, username, secret, secret_ciphertext, secret_nonce, "
            "       encryption_key_version, migration_status "
            "FROM totp_secrets WHERE tenant_id = $1 AND user_id = $2",
            principal.tenant_id, principal.user_id,
        )
    return [dict(r) for r in rows]


async def _encrypt_row(principal, row: dict) -> str:
    """Move one row to ciphertext. Returns a one-word outcome."""
    from core.identity.db_context import tenant_connection

    if row["migration_status"] == "ENCRYPTED":
        return "already"

    plaintext = row["secret"]

    # The crash-after-step-3 case: plaintext already gone, ciphertext present,
    # status never updated. Nothing to seal — verify and finish.
    if not plaintext and row["secret_ciphertext"] is not None:
        if not await _verify_row(principal, row, expected=None):
            return "unverifiable"
        async with tenant_connection(principal) as conn:
            await conn.execute(
                "UPDATE totp_secrets SET migration_status = 'ENCRYPTED' "
                "WHERE id = $1", row["id"])
        return "completed"

    if not plaintext:
        # No plaintext and no ciphertext. `totp_secrets_has_a_secret` should
        # make this impossible; if it happens the row authenticates nobody and
        # must not be reported as migrated.
        return "empty"

    # ── 1. Seal, and write the ciphertext WITHOUT clearing the plaintext ──
    from security.mfa_crypto import seal_totp_secret

    ciphertext, nonce, version = await seal_totp_secret(principal, plaintext)
    async with tenant_connection(principal) as conn:
        await conn.execute(
            "UPDATE totp_secrets SET secret_ciphertext = $2, secret_nonce = $3, "
            "       encryption_key_version = $4, encrypted_at = NOW(), "
            "       migration_status = 'ENCRYPTING' "
            "WHERE id = $1",
            row["id"], ciphertext, nonce, version)

    # ── 2. Read it back from the table and prove it opens to the same seed ──
    stored = dict(await _refetch(principal, row["id"]))
    if not await _verify_row(principal, stored, expected=plaintext):
        async with tenant_connection(principal) as conn:
            await conn.execute(
                "UPDATE totp_secrets SET migration_status = 'FAILED' "
                "WHERE id = $1", row["id"])
        return "mismatch"

    # ── 3. Only now is the plaintext safe to drop ─────────────────────────
    async with tenant_connection(principal) as conn:
        await conn.execute(
            "UPDATE totp_secrets SET secret = NULL, migration_status = 'ENCRYPTED' "
            "WHERE id = $1 AND secret_ciphertext IS NOT NULL", row["id"])
    return "encrypted"


async def _refetch(principal, row_id):
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        return await conn.fetchrow(
            "SELECT id, secret, secret_ciphertext, secret_nonce, "
            "       encryption_key_version, migration_status "
            "FROM totp_secrets WHERE id = $1", row_id)


async def _verify_row(principal, row: dict, *, expected) -> bool:
    """Does the stored ciphertext open, and to `expected` when one is given?

    The comparison happens inside the decryption callback, so the seed never
    becomes a value this function returns or logs.
    """
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret

    if row["secret_ciphertext"] is None or row["secret_nonce"] is None:
        return False

    def _same(seed: str) -> bool:
        return True if expected is None else seed == expected

    try:
        return bool(await use_totp_secret(
            principal, row["secret_ciphertext"], row["secret_nonce"],
            int(row["encryption_key_version"]), _same))
    except MfaKeyUnavailable:
        return False


async def _decrypt_row(principal, row: dict) -> str:
    """Move one row back to plaintext, for `rollback/053`."""
    from core.identity.db_context import tenant_connection
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret

    if row["secret_ciphertext"] is None:
        return "already"

    async with tenant_connection(principal) as conn:
        async def _write_back(seed: str):
            # Written from INSIDE the callback so the plaintext never becomes a
            # local in this function. The status moves in the same statement:
            # `totp_secrets_encrypted_is_complete` forbids ENCRYPTED alongside a
            # non-NULL secret, so a two-statement version would be rejected.
            await conn.execute(
                "UPDATE totp_secrets SET secret = $2, "
                "       migration_status = 'PLAINTEXT' WHERE id = $1",
                row["id"], seed)

        try:
            await use_totp_secret(
                principal, row["secret_ciphertext"], row["secret_nonce"],
                int(row["encryption_key_version"]), _write_back)
        except MfaKeyUnavailable:
            return "unopenable"

        # The ciphertext goes only after the plaintext is committed to the row,
        # for the same reason the encrypt direction clears last.
        await conn.execute(
            "UPDATE totp_secrets SET secret_ciphertext = NULL, secret_nonce = NULL, "
            "       encryption_key_version = NULL, encrypted_at = NULL "
            "WHERE id = $1 AND secret IS NOT NULL", row["id"])
    return "decrypted"


async def _run(args: argparse.Namespace) -> int:
    import asyncpg

    from core.connectors.secrets import KEY_ENV, is_configured
    import utils.db as db_module

    if not is_configured():
        _fail(f"{KEY_ENV} is not set or is not a valid 32-byte base64 key. "
              f"Without the KEK nothing can be sealed or opened, and a "
              f"half-migrated table is worse than an unmigrated one.")

    dsn = args.dsn or os.getenv("IRA_DATABASE_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        _fail("no database DSN. Pass --dsn, or set IRA_DATABASE_DSN / DATABASE_URL.")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = pool
    verb = "decrypt" if args.decrypt else "encrypt"

    try:
        if args.all:
            tenants = [r["id"] for r in
                       await pool.fetch("SELECT id FROM tenants ORDER BY id")]
        else:
            tenants = [uuid.UUID(args.tenant)]
        if not tenants:
            print("No tenants. Nothing to do.")
            return 0

        totals: dict[str, int] = {}
        examined = 0
        for tenant in tenants:
            users = await _candidates(pool, tenant)
            for user in users:
                principal = _principal(tenant, user)
                for row in await _rows_for(principal):
                    examined += 1
                    if args.dry_run:
                        state = row["migration_status"]
                        want = ("ENCRYPTED" if not args.decrypt else "PLAINTEXT")
                        outcome = "already" if state == want else f"would {verb}"
                        totals[outcome] = totals.get(outcome, 0) + 1
                        continue
                    outcome = (await _decrypt_row(principal, row)
                               if args.decrypt
                               else await _encrypt_row(principal, row))
                    totals[outcome] = totals.get(outcome, 0) + 1

        print(f"{verb}: examined {examined} enrolment(s) across "
              f"{len(tenants)} tenant(s)")
        for outcome in sorted(totals):
            print(f"  {outcome}: {totals[outcome]}")

        # Stated every run, not only when it is non-zero: a worker that reports
        # a clean sweep while rows it structurally cannot see remain behind has
        # told the operator something untrue.
        unreachable = await pool.fetchval(
            "SELECT count(*) FROM totp_secrets WHERE user_id IS NULL")
        print(f"  unreachable (user_id IS NULL, invisible to RLS and to the "
              f"login path): {int(unreachable or 0)}")

        bad = sum(totals.get(k, 0)
                  for k in ("mismatch", "unopenable", "unverifiable", "empty"))
        if bad:
            print(f"\n{bad} row(s) did not complete. Their plaintext was NOT "
                  f"cleared and they are safe to re-run.", file=sys.stderr)
            return 1
        return 0
    finally:
        db_module._pool = previous
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", help="migrate one tenant, by UUID")
    target.add_argument("--all", action="store_true", help="migrate every tenant")
    parser.add_argument("--dsn", help="database DSN (default: IRA_DATABASE_DSN)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would move and change nothing")
    parser.add_argument(
        "--decrypt", action="store_true",
        help=("reverse direction: ciphertext back to plaintext. Required "
              "before rollback/053, which refuses while any row is ENCRYPTED"))
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
