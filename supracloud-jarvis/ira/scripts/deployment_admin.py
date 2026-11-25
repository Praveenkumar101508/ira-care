"""Grant, revoke and list deployment administrators; make the device-policy key (D-085, E2.o).

An operator tool, run with the migrator's credentials, like scripts/os_release.py: the runtime
role can read `deployment_administrators` (migration 122) and write nothing there, so no request
to the API, whatever its care role, can make anybody a deployment administrator.

    python -m scripts.deployment_admin keygen
    python -m scripts.deployment_admin grant  --dsn DSN --user-id UUID --label "fleet on-call"
    python -m scripts.deployment_admin revoke --dsn DSN --user-id UUID
    python -m scripts.deployment_admin list   --dsn DSN

A deployment administrator may then issue signed instructions from the closed list
(care/devicepolicy.py) to one watch at a time through
POST /api/v1/care/deployment-admin/device-instructions.
A FACTORY_RESET is only a request until a second, different deployment administrator approves
it within 30 minutes (POST .../device-instructions/{id}/approve, D-089), so a deployment that
wants to be able to reset a watch remotely needs at least two live grants.

Revoking a grant takes effect on the next request: the account can issue nothing more, the
watches stop receiving anything it issued that they have not collected, and what it issued
that has not expired goes onto each watch's revocation list.

`keygen` prints a new device-policy key for IRA_DEVICE_POLICY_KEY and its public key for the
image. Store the private half as a secret on the server only; it is printed once and never
written to disk by this script.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import secrets
import sys
import uuid

from cryptography.hazmat.primitives.asymmetric import ec


def keygen() -> tuple[str, str]:
    from care import devicepolicy
    while True:
        raw = secrets.token_bytes(32)
        try:
            key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
            break
        except ValueError:  # 0 or above the group order: vanishingly rare, draw again
            continue
    return base64.b64encode(raw).decode(), devicepolicy.public_key_b64(key)


async def _as_operator(conn) -> None:
    """Become ira_schema_owner, the only role the table's write policy admits."""
    from utils.migrations import _assume_schema_owner
    await _assume_schema_owner(conn)


async def grant(conn, user_id: uuid.UUID, label: str) -> str:
    await _as_operator(conn)
    try:
        return str(await conn.fetchval(
            "INSERT INTO deployment_administrators (user_id, label) VALUES ($1, $2) RETURNING id", user_id, label))
    finally:
        await conn.execute("RESET ROLE")


async def revoke(conn, user_id: uuid.UUID) -> int:
    await _as_operator(conn)
    try:
        status = await conn.execute(
            "UPDATE deployment_administrators SET revoked_at = NOW() WHERE user_id = $1 AND revoked_at IS NULL",
            user_id)
        return int(status.split()[-1])
    finally:
        await conn.execute("RESET ROLE")


async def _run(args) -> int:
    import asyncpg

    conn = await asyncpg.connect(args.dsn)
    try:
        if args.cmd == "grant":
            print(await grant(conn, uuid.UUID(args.user_id), args.label))
        elif args.cmd == "revoke":
            n = await revoke(conn, uuid.UUID(args.user_id))
            print(f"revoked {n} live grant(s)")
            return 0 if n else 1
        elif args.cmd == "list":
            await _as_operator(conn)
            for r in await conn.fetch("SELECT user_id, label, granted_at, revoked_at FROM deployment_administrators"
                                      " ORDER BY granted_at"):
                state = f"revoked {r['revoked_at']:%Y-%m-%d %H:%M}" if r["revoked_at"] else "live"
                print(f"{r['user_id']}  {r['label']:<24} granted {r['granted_at']:%Y-%m-%d %H:%M}  {state}")
    finally:
        await conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deployment_admin", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    g = sub.add_parser("grant")
    g.add_argument("--dsn", required=True)
    g.add_argument("--user-id", required=True)
    g.add_argument("--label", required=True)
    r = sub.add_parser("revoke")
    r.add_argument("--dsn", required=True)
    r.add_argument("--user-id", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("--dsn", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "keygen":
        private, public = keygen()
        print(f"IRA_DEVICE_POLICY_KEY={private}")
        print(f"public key (for the image): {public}")
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
