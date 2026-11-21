#!/usr/bin/env python3
"""Rotate the data-encryption keys protecting stored connector credentials.

`RELEASE_READINESS.md` R11, on blocker B3:

    `rewrap` exists with no caller, so rotating the key re-encrypts nothing

This is the caller. It is a deliberate, operator-run command rather than a
background job, because a rotation reads and re-writes every credential a tenant
has, and something that touches that data should be started by a person who
meant to.

── Usage ────────────────────────────────────────────────────────────────────

    # See what a rotation would do, without doing it
    python scripts/rotate_connector_keys.py --tenant <uuid> --dry-run

    # Rotate one tenant
    python scripts/rotate_connector_keys.py --tenant <uuid>

    # Rotate every tenant that has a key
    python scripts/rotate_connector_keys.py --all

    # Introducing rotation somewhere it has never run: keep the old key, so the
    # step is reversible until you have seen it work
    python scripts/rotate_connector_keys.py --all --keep-old-key

Requires `IRA_CONNECTOR_ENCRYPTION_KEY` (the KEK) and a database DSN, exactly as
the application does. Without the KEK it refuses rather than doing half a job.

── What it does NOT do ──────────────────────────────────────────────────────

It does not rotate the **KEK** itself. Doing that means re-wrapping every row in
`tenant_data_keys` under a new environment key, which is a different operation
with a different failure mode (get it wrong and every tenant loses access at
once, rather than one). `docs/security/KEY_MANAGEMENT.md` describes the manual
procedure; automating it is not in this script and is not claimed to be.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Run from anywhere: the package root is this file's grandparent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fail(message: str) -> "None":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _operator_principal(tenant: uuid.UUID):
    """A Principal for a tenant, so RLS lets this script see that tenant's rows.

    This matters more than it looks, and the first draft got it wrong. RLS on
    `tenant_data_keys` and `connector_accounts` is **forced**, and the
    application role is not a superuser — so a query run on a bare pooled
    connection, with no `app.tenant_id` bound, matches **nothing**. It does not
    error. It returns zero rows.

    Running the first draft against a real database produced:

        would re-seal 0 credential(s), replacing 0 live key version(s)

    for a tenant that in fact had one credential and one live key. The dry run
    was reporting an empty rotation for a tenant with work to do — the single
    worst failure mode for a command whose job is to tell an operator what is
    about to happen. Every read here therefore goes through
    `tenant_connection`, exactly as the application's do.

    The user id is derived and does not identify a person: nothing in this
    script writes a row attributed to a user, and RLS keys on the tenant.
    """
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=uuid.uuid5(tenant, "key-rotation-operator"),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
    )


async def _tenants_with_keys(pool) -> list[uuid.UUID]:
    """Every tenant holding a live key.

    Two steps, because there is no single query that can do it: `tenants` is
    global reference data with no RLS, so it can be enumerated; `tenant_data_keys`
    is tenant-scoped and forced, so each tenant must be asked about under its own
    context. Enumerating from the scoped table instead — the first draft — quietly
    returns nothing at all.

    One round trip per tenant, which is linear and slow on a large fleet. That
    is the price of asking the database under each tenant's own context rather
    than reaching around RLS, and for a manual maintenance command run at human
    intervals it is the right trade. Said plainly rather than left for someone
    to discover: `--all` on ten thousand tenants will take a while.
    """
    from core.identity.db_context import tenant_connection

    candidates = [
        r["id"] for r in await pool.fetch("SELECT id FROM tenants ORDER BY id")
    ]

    holders: list[uuid.UUID] = []
    for tenant in candidates:
        async with tenant_connection(_operator_principal(tenant)) as conn:
            live = await conn.fetchval(
                "SELECT COUNT(*) FROM tenant_data_keys "
                "WHERE tenant_id = $1 AND destroyed_at IS NULL",
                tenant,
            )
        if int(live or 0):
            holders.append(tenant)
    return holders


async def _credential_counts(tenant: uuid.UUID) -> tuple[int, int]:
    """(credentials, live key versions) for a tenant, for the dry run.

    Under the tenant's own context — see `_operator_principal`.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(_operator_principal(tenant)) as conn:
        creds = await conn.fetchval(
            "SELECT COUNT(*) FROM connector_accounts WHERE tenant_id = $1 "
            "AND credential_ciphertext IS NOT NULL AND revoked_at IS NULL",
            tenant,
        )
        keys = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_data_keys WHERE tenant_id = $1 "
            "AND destroyed_at IS NULL",
            tenant,
        )
    return int(creds or 0), int(keys or 0)


async def _run(args: argparse.Namespace) -> int:
    import asyncpg

    # Named imports, not `from core.connectors import keyring`.
    # tests/architecture/test_credential_boundary.py refuses the module-level
    # form anywhere outside core/connectors/, because importing the module hands
    # the importer `keyring.dek_for` — and it caught this script on the first
    # run. `rotate_tenant` is permitted for the same reason `destroy_keys` is:
    # it re-seals credentials internally and returns a count, never plaintext
    # and never key bytes. `is_configured` returns a boolean.
    from core.connectors.keyring import rotate_tenant
    from core.connectors.secrets import KEY_ENV, is_configured
    import utils.db as db_module

    if not is_configured():
        _fail(
            f"{KEY_ENV} is not set or is not a valid 32-byte base64 key. A "
            f"rotation cannot unwrap the existing keys without it, and a "
            f"half-finished rotation is worse than none."
        )

    dsn = args.dsn or os.getenv("IRA_DATABASE_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        _fail(
            "no database DSN. Pass --dsn, or set IRA_DATABASE_DSN / DATABASE_URL."
        )

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = pool

    try:
        if args.all:
            tenants = await _tenants_with_keys(pool)
            if not tenants:
                print("No tenant holds a live key. Nothing to rotate.")
                return 0
        else:
            try:
                tenants = [uuid.UUID(args.tenant)]
            except (TypeError, ValueError):
                _fail(f"--tenant {args.tenant!r} is not a UUID")
                return 2

        print(f"{len(tenants)} tenant(s) to rotate.\n")

        if args.dry_run:
            for tenant in tenants:
                creds, keys = await _credential_counts(tenant)
                print(
                    f"  {tenant}  would re-seal {creds} credential(s), "
                    f"replacing {keys} live key version(s)"
                )
            print(
                "\nDry run — nothing was changed. Re-run without --dry-run to "
                "rotate."
            )
            return 0

        failures = 0
        for tenant in tenants:
            principal = _operator_principal(tenant)
            try:
                result = await rotate_tenant(
                    principal,
                    destroy_old=not args.keep_old_key,
                    reason=args.reason,
                )
            except Exception as exc:  # noqa: BLE001 - reported per tenant
                # One tenant's failure must not stop the others: an operator
                # running --all needs the rest rotated and a list of what did
                # not work, not an abort at the first problem.
                failures += 1
                print(f"  {tenant}  FAILED: {type(exc).__name__}: {exc}")
                continue

            destroyed = "old key destroyed" if result.old_key_destroyed else (
                result.note or "old key KEPT"
            )
            print(
                f"  {tenant}  v{result.from_version or '-'} → "
                f"v{result.to_version}, {result.credentials_rewrapped} "
                f"credential(s) re-sealed, {destroyed}"
            )

        if failures:
            print(f"\n{failures} tenant(s) failed. Re-run to retry them: a "
                  f"rotation is safe to repeat.", file=sys.stderr)
            return 1
        print("\nDone.")
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
    target.add_argument("--tenant", help="rotate one tenant, by UUID")
    target.add_argument(
        "--all", action="store_true", help="rotate every tenant holding a key"
    )
    parser.add_argument("--dsn", help="database DSN (default: IRA_DATABASE_DSN)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be re-sealed and change nothing",
    )
    parser.add_argument(
        "--keep-old-key", action="store_true",
        help=(
            "do not destroy the superseded key. Makes the rotation reversible, "
            "at the cost of the old key continuing to exist"
        ),
    )
    parser.add_argument(
        "--reason", default="scheduled key rotation",
        help="recorded on the destroyed key's tombstone (min 8 characters)",
    )
    args = parser.parse_args()

    if len(args.reason.strip()) < 8:
        _fail("--reason must be at least 8 characters: it is what explains a "
              "destroyed key to whoever asks about it later")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
