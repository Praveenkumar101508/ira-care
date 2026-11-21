#!/usr/bin/env python3
"""Provision a cloud tenant for a verified account (Phase 2 batch 4).

── Why this is an operator tool and not an HTTP route ───────────────────────

There is a genuine ordering problem, and inventing a route would paper over it
rather than solve it.

Provisioning needs the caller to be a verified account. Proving that over HTTP
means presenting a token. A token is issued by `POST /auth/cloud/login`, which
refuses any account with no access-granting membership — which is exactly the
account that needs provisioning. So an authenticated provisioning route cannot
be reached by the only people who need it.

The alternatives are worse. A route that re-checks email and password itself
would be a second credential path beside `cloud_login`, and two credential
paths disagree eventually. A route that provisions during registration would
undo the separation batch 2 built deliberately, where *"registered but not
provisioned"* is a state the system can hold rather than a gap between two
half-applied writes.

Self-serve tenant creation needs a signup flow that decides those questions on
purpose. Until then a tenant is founded by an operator, which is also the
honest shape for a product with no billing and no abuse controls: creating a
tenant is not yet a thing a stranger should be able to do unattended.

── Usage ────────────────────────────────────────────────────────────────────

    # What it would do, without doing it
    python scripts/phase2/provision_tenant.py \\
        --email founder@example.com --organisation "Acme Industries" --dry-run

    # Do it
    python scripts/phase2/provision_tenant.py \\
        --email founder@example.com --organisation "Acme Industries"

    # A named first workspace (default: "General")
    python scripts/phase2/provision_tenant.py \\
        --email founder@example.com --organisation "Acme" --workspace "Studio"

The account must already exist and have verified its address — this does not
create users, and it does not verify addresses. It refuses an account that
already belongs to a tenant; see `provision_tenant`'s docstring for why that
refusal protects the account rather than merely restricting it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# The package root, so `core.identity...` imports resolve when this is run as a
# file rather than as a module. Matches the other scripts in this directory.
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))


async def _run(args) -> int:
    from core.identity.tenant_provisioning import (
        ProvisioningError, ProvisioningRefused, provision_tenant,
    )

    if args.dry_run:
        # Deliberately only the eligibility half. A dry run that opened the
        # provisioning transaction and rolled it back would consume ids and
        # exercise a code path the real run does not take; reporting what is
        # KNOWABLE without writing is more honest than simulating the write.
        from core.identity.tenant_provisioning import _eligible_founder

        try:
            founder = await _eligible_founder(args.email)
        except ProvisioningError as exc:
            print(f"would refuse: {exc}")
            return 1
        print(f"would provision a tenant for {founder['normalised_email']}")
        print(f"  organisation : {args.organisation}")
        print(f"  workspace    : {args.workspace} (default)")
        print("  roles        : primary_owner (tenant), "
              "workspace_admin (workspace)")
        print("\nNothing was written. Re-run without --dry-run.")
        return 0

    try:
        result = await provision_tenant(
            args.email, args.organisation, workspace_name=args.workspace,
        )
    except ProvisioningRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except ProvisioningError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2

    print("provisioned")
    print(f"  tenant       : {result.tenant_id}")
    print(f"  organisation : {result.organisation_id}")
    print(f"  workspace    : {result.workspace_id}  (default)")
    print(f"  founder      : {result.founder_subject}")
    print("\nThe founder can now sign in at POST /auth/cloud/login.")
    return 0


async def _with_pool(args) -> int:
    import utils.db as db_module

    dsn = args.dsn or os.getenv("IRA_DATABASE_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        print("no DSN: pass --dsn or set IRA_DATABASE_DSN", file=sys.stderr)
        return 2

    import asyncpg

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = pool
    try:
        return await _run(args)
    finally:
        db_module._pool = previous
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--email", required=True,
                        help="the founder's verified email address")
    parser.add_argument("--organisation", required=True,
                        help="the customer-facing organisation name")
    parser.add_argument("--workspace", default="General",
                        help="name of the default workspace (default: General)")
    parser.add_argument("--dsn", help="database DSN (default: IRA_DATABASE_DSN)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report eligibility and change nothing")
    args = parser.parse_args()
    return asyncio.run(_with_pool(args))


if __name__ == "__main__":
    raise SystemExit(main())
