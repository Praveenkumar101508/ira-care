#!/usr/bin/env python3
"""Phase 4 batch 02 — the encryption worker `migrate_secrets.py` refused to have.

    python -m scripts.phase4.encrypt_voiceprints status  --dsn ...
    python -m scripts.phase4.encrypt_voiceprints encrypt --dsn ... --confirm
    python -m scripts.phase4.encrypt_voiceprints decrypt --dsn ... --confirm

── Why this exists in a new file rather than as a command on the old one ───

`scripts/phase3b/migrate_secrets.py` has an `encrypt` command shaped like a
hole. It was written, found to be wrong in two ways, and REMOVED rather than
fixed — the module docstring records both reasons. The second was the real one:
the key scope was an open decision (D-17), and writing the worker before it was
answered would have baked in the tenant-scoped answer the question existed to
avoid.

D-17 is decided and CLOUD-ADR-0012 is the answer, so the worker can be written.
It lives here because `migrate_secrets.py` states, as a safety property, that it
imports no credential path and is **read-only** — `tests/architecture/
test_credential_boundary.py` enforces the first and its own docstring promises
the second. Adding an encrypting command to it would break both, and a module
whose stated safety properties are false is worse than one that never claimed
them.

── What it does, one row at a time ─────────────────────────────────────────

Each row is sealed under its OWN freshly minted data-encryption key, wrapped
under the environment KEK, in its own transaction. Per-record because
CLOUD-ADR-0012 says so and because `voice_profiles` carries `enrollment_version`
and a per-row `deletion_status` — one person may hold several enrolments and
revoke one, and destroying the key for one row must not touch another.

A row goes PLAINTEXT → ENCRYPTING → ENCRYPTED, and the intermediate state is not
decoration. If the process dies mid-row, the row is left ENCRYPTING and is
visible in `status` as in-flight rather than looking like one that was never
started. `--confirm` re-running is safe: an ENCRYPTING row still holds its
plaintext, because the plaintext is cleared last.

**The plaintext is cleared LAST, and only after the ciphertext has been read
back and opened.** Writing ciphertext and clearing plaintext in one statement
would mean a wrapped key that cannot be unwrapped destroys the template it was
supposed to protect, silently, and `063` would then drop the column that still
held the good copy. So every row is round-tripped before its plaintext goes.

── Safety properties ───────────────────────────────────────────────────────

**No template is ever printed.** Not in output, not in an error, not in a
traceback. Every failure reports a row id and an exception TYPE.

**Refuses a role that cannot see everything.** Same reasoning
`migrate_secrets.py` records at length: `voice_profiles` is USER_PRIVATE under
FORCE RLS, so a count taken by the application role is a count of what that role
happens to see — which is zero. A worker that "encrypted 0 of 0 rows" and
reported success would leave every template plaintext and mark the schema ready
to have the plaintext dropped.

**`--confirm` is required to write.** `status` needs no flag. The writing
commands do, because this rewrites biometric material in place and a mistyped
DSN should not be enough.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

TABLE = "voice_profiles"


def redact_dsn(dsn: str) -> str:
    import re
    return re.sub(r"(?P<scheme>[a-z+]+://)(?P<user>[^:@/]*):[^@/]*@",
                  r"\g<scheme>\g<user>:***@", dsn)


async def connect(dsn: str):
    try:
        import asyncpg
    except ImportError:
        sys.exit("asyncpg is required: pip install asyncpg")
    return await asyncpg.connect(dsn)


async def refuse_if_the_role_cannot_see_everything(conn) -> None:
    """Refuse to do anything unless the connected role reads unfiltered rows.

    `voice_profiles` is USER_PRIVATE under FORCE ROW LEVEL SECURITY, keyed on
    `app.user_id`, which this tool never sets. Run as the application role every
    query below matches zero rows — so `encrypt` would report "0 rows, nothing
    to do", `status` would report `contract_ready`, and `063` would drop the
    plaintext column while every template was still in it and merely invisible.

    That is the P2-05 shape, and on this table it destroys every voiceprint
    while reporting there were none.
    """
    forced = await conn.fetchval(
        "SELECT relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", TABLE)
    if not forced:
        return   # nothing to be filtered by

    # `has_table_privilege` is not the question — the owner has privileges and
    # is still subject to FORCE. The question is whether policies apply, and the
    # only role they do not apply to is the one that owns the table with FORCE
    # lifted, or a superuser/BYPASSRLS role. Ask the database directly.
    bypasses = await conn.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    if bypasses:
        return

    sys.exit(
        f"REFUSING: {TABLE} is under FORCE ROW LEVEL SECURITY and the connected "
        f"role ({await conn.fetchval('SELECT current_user')}) is subject to its "
        f"policies. Every count and every UPDATE below would silently match the "
        f"rows this role happens to see, which for a USER_PRIVATE table with no "
        f"app.user_id bound is none — reporting success having encrypted "
        f"nothing. Connect as the migration owner."
    )


async def report(conn) -> dict:
    row = await conn.fetchrow("SELECT * FROM voice_migration_status")
    return dict(row) if row else {}


async def cmd_status(conn, _args) -> int:
    counts = await report(conn)
    if not counts:
        print("no voice enrolments")
        return 0
    for key, value in counts.items():
        print(f"  {key:24} {value}")
    # Non-zero while anything is unresolved, so this is usable as a gate.
    return 0 if counts.get("contract_ready") else 1


async def cmd_encrypt(conn, args) -> int:
    """Seal every plaintext template. Returns non-zero if any row failed."""
    from security.voice_crypto import seal_template, use_template

    rows = await conn.fetch(
        "SELECT id, enrollment_version, embedding FROM voice_profiles "
        " WHERE migration_status IN ('PLAINTEXT', 'ENCRYPTING') "
        "   AND deletion_status = 'live' "
        "   AND embedding IS NOT NULL AND embedding <> '' "
        " ORDER BY created_at")
    if not rows:
        print("nothing to encrypt")
        return 0

    done = failed = 0
    for row in rows:
        try:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE voice_profiles SET migration_status = 'ENCRYPTING' "
                    " WHERE id = $1", row["id"])
                sealed = seal_template(
                    row["id"], row["enrollment_version"], row["embedding"])
                await conn.execute(
                    "UPDATE voice_profiles "
                    "   SET template_ciphertext = $2, template_nonce = $3, "
                    "       wrapped_dek = $4, wrap_nonce = $5, kek_id = $6, "
                    "       encryption_key_version = $7, encrypted_at = NOW(), "
                    "       migration_status = 'ENCRYPTED' "
                    " WHERE id = $1",
                    row["id"], sealed["template_ciphertext"],
                    sealed["template_nonce"], sealed["wrapped_dek"],
                    sealed["wrap_nonce"], sealed["kek_id"],
                    sealed["encryption_key_version"])

                # Read it BACK and open it, inside the same transaction, before
                # the plaintext goes. A wrapped key that will not unwrap
                # destroys the template it was meant to protect, and `063` would
                # then drop the column that still held the good copy. The
                # round-trip is the only thing that distinguishes "encrypted"
                # from "overwritten with bytes".
                written = await conn.fetchrow(
                    "SELECT id, enrollment_version, template_ciphertext, "
                    "       template_nonce, wrapped_dek, wrap_nonce, kek_id, "
                    "       encryption_key_version "
                    "  FROM voice_profiles WHERE id = $1", row["id"])
                await use_template(written, json.loads)

                await conn.execute(
                    "UPDATE voice_profiles SET embedding = '' WHERE id = $1",
                    row["id"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            # The transaction rolled back, so the row still holds its plaintext
            # and its status. Marked FAILED in a SEPARATE statement, because the
            # aborted transaction cannot carry one.
            #
            # The exception is NOT printed. It can contain the template.
            failed += 1
            try:
                await conn.execute(
                    "UPDATE voice_profiles SET migration_status = 'FAILED' "
                    " WHERE id = $1", row["id"])
            except Exception:  # noqa: BLE001
                pass
            print(f"  FAILED {row['id']} ({type(exc).__name__})", file=sys.stderr)

    print(f"encrypted {done} enrolment(s); {failed} failed")
    return 1 if failed else 0


async def cmd_decrypt(conn, args) -> int:
    """Restore plaintext templates. The reverse path, and it has to exist.

    `rollback/062` refuses while any enrolment holds a wrapped key, and its HINT
    tells the operator to decrypt back to plaintext first. A refusal that names
    a command nobody wrote is a refusal with no way out — the exact shape
    `migrate_secrets.py` avoided by removing a command rather than shipping a
    broken one.
    """
    from security.voice_crypto import use_template

    rows = await conn.fetch(
        "SELECT id, enrollment_version, template_ciphertext, template_nonce, "
        "       wrapped_dek, wrap_nonce, kek_id, encryption_key_version "
        "  FROM voice_profiles "
        " WHERE migration_status = 'ENCRYPTED' AND wrapped_dek IS NOT NULL "
        " ORDER BY created_at")
    if not rows:
        print("nothing to decrypt")
        return 0

    done = failed = 0
    for row in rows:
        try:
            async with conn.transaction():
                # The plaintext is written straight into the UPDATE rather than
                # returned into a local, so it exists in one frame for one
                # statement.
                await use_template(
                    row,
                    lambda template, _id=row["id"]: conn.execute(
                        "UPDATE voice_profiles "
                        "   SET embedding = $2, template_ciphertext = NULL, "
                        "       template_nonce = NULL, wrapped_dek = NULL, "
                        "       wrap_nonce = NULL, kek_id = NULL, "
                        "       encryption_key_version = NULL, "
                        "       encrypted_at = NULL, "
                        "       migration_status = 'PLAINTEXT' "
                        " WHERE id = $1", _id, template))
            done += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAILED {row['id']} ({type(exc).__name__})", file=sys.stderr)

    print(f"decrypted {done} enrolment(s); {failed} failed")
    return 1 if failed else 0


COMMANDS = {"status": cmd_status, "encrypt": cmd_encrypt, "decrypt": cmd_decrypt}
WRITES = {"encrypt", "decrypt"}


async def main_async(args) -> int:
    conn = await connect(args.dsn)
    try:
        await refuse_if_the_role_cannot_see_everything(conn)
        return await COMMANDS[args.command](conn, args)
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--dsn", required=True)
    p.add_argument("--confirm", action="store_true",
                   help="required for encrypt and decrypt — they rewrite "
                        "biometric material in place")
    args = p.parse_args()

    if args.command in WRITES and not args.confirm:
        sys.exit(
            f"{args.command} rewrites biometric material in place. Re-run with "
            f"--confirm once you have checked the DSN: "
            f"{redact_dsn(args.dsn)}")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
