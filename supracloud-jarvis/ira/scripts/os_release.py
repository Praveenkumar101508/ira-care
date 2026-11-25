"""Publish, stage and withdraw IRA CARE OS releases (care/ota.py, migration 120).

An operator tool, run with the migrator's credentials: the runtime role can only read the
catalogue. Nothing here builds or signs an image; that happens on the build host with the
release keys (docs/os/BUILD.md, E4.c, E4.g).

    python -m scripts.os_release keygen
    python -m scripts.os_release publish --dsn DSN --version 1.0.3 --fingerprint FP \\
        --rollback-index 3 --url https://... --sha256 HEX --size BYTES [--rollout 0] [--notes TEXT]
    python -m scripts.os_release rollout --dsn DSN --id RELEASE_ID --percent 10
    python -m scripts.os_release withdraw --dsn DSN --id RELEASE_ID
    python -m scripts.os_release list --dsn DSN

`keygen` prints a new OTA signing key for IRA_OTA_SIGNING_KEY and its public key for the
image. Store the private half as a secret on the server only; it is printed once and never
written to disk by this script.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption


def keygen() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(seed).decode(), base64.b64encode(pub).decode()


async def _run(args) -> int:
    import asyncpg

    from care import ota

    conn = await asyncpg.connect(args.dsn)
    try:
        if args.cmd == "publish":
            rid = await ota.publish(conn, version=args.version, build_fingerprint=args.fingerprint,
                                    rollback_index=args.rollback_index, payload_url=args.url,
                                    payload_sha256=args.sha256, payload_size=args.size,
                                    rollout_percent=args.rollout, notes=args.notes)
            print(rid)
        elif args.cmd == "rollout":
            await ota.set_rollout(conn, args.id, args.percent)
        elif args.cmd == "withdraw":
            await ota.withdraw(conn, args.id)
        elif args.cmd == "list":
            for r in await conn.fetch("SELECT id, version, rollback_index, rollout_percent, withdrawn, published_at"
                                      " FROM os_releases ORDER BY rollback_index DESC, published_at DESC"):
                print(f"{r['id']}  {r['version']:<16} index {r['rollback_index']:<4} "
                      f"{r['rollout_percent']:>3} %  {'withdrawn' if r['withdrawn'] else 'live'}  {r['published_at']:%Y-%m-%d}")
    finally:
        await conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="os_release", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    p = sub.add_parser("publish")
    p.add_argument("--dsn", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--rollback-index", type=int, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--size", type=int, required=True)
    p.add_argument("--rollout", type=int, default=0)
    p.add_argument("--notes", default="")
    r = sub.add_parser("rollout")
    r.add_argument("--dsn", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--percent", type=int, required=True)
    w = sub.add_parser("withdraw")
    w.add_argument("--dsn", required=True)
    w.add_argument("--id", required=True)
    lst = sub.add_parser("list")
    lst.add_argument("--dsn", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "keygen":
        private, public = keygen()
        print(f"IRA_OTA_SIGNING_KEY={private}")
        print(f"public key (for the image): {public}")
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
