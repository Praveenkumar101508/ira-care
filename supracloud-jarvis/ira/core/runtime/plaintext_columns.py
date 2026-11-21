"""Which plaintext secret columns still exist (Phase 4 batch 03).

`063` drops `totp_secrets.secret` and `voice_profiles.embedding`. Code and
schema do not move at the same instant, so for one window a running process
faces a database that may or may not still have them, and it has to be correct
in both states:

  * **before `063`** — un-migrated rows exist and their ONLY copy is the
    plaintext column. Code that stopped reading it would break voice
    verification and TOTP for every deployment that has not run the workers,
    silently and in the fail-open direction for voice.
  * **after `063`** — the column is gone. A statement that names it raises
    `UndefinedColumn`, which on the enrolment path is a 500 on a working system.

This is the ordinary expand/contract problem, and the ordinary answer is to
split it across two releases: stop referencing the column in release N, drop it
in release N+1. That is not available here — the migration and the code ship
together — so the process asks the database once and remembers.

── Why a cached introspection and not a try/except ─────────────────────────

`except UndefinedColumn: retry without the column` would work and is worse. It
puts a schema question on the error path of a hot query, so the answer is only
ever discovered by failing; and an `UndefinedColumn` raised for some OTHER
reason — a typo in a later edit — would be silently absorbed into "must be
post-063", which is exactly the class of bug this file exists to avoid.

Asking once, explicitly, means the answer is a fact rather than an inference
from a failure.

── Why the cache is never invalidated ──────────────────────────────────────

A column is dropped by a migration, and a migration is a deployment. A process
that was running before `063` and is still running after it is a process that
survived its own deployment, which does not happen here — and if it somehow did,
the stale answer is "the column exists", whose failure mode is a loud
`UndefinedColumn` rather than a silent wrong result.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("ira.runtime.plaintext_columns")

#: `None` until asked. Not a bool default, because `False` and "not yet asked"
#: must not be the same value — defaulting to False would mean a failed lookup
#: reads as "the column is gone", and the enrolment path would stop writing a
#: plaintext column that is still NOT NULL.
_HAS: dict[str, Optional[bool]] = {}


async def exists(conn, table: str, column: str) -> bool:
    """True if `table.column` is present. Asked once per process, then cached.

    Takes the caller's connection rather than opening one: this is asked inside
    flows that already hold a bound connection, and opening a second one to ask
    a schema question would be a second transaction with different visibility.
    """
    key = f"{table}.{column}"
    cached = _HAS.get(key)
    if cached is not None:
        return cached

    present = bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "                WHERE table_schema = 'public' "
        "                  AND table_name = $1 AND column_name = $2)",
        table, column))
    _HAS[key] = present
    logger.info("plaintext column %s is %s", key,
                "still present (pre-063)" if present else "gone (post-063)")
    return present


def reset_cache() -> None:
    """Forget what was asked. For tests that move the schema underneath a
    process, which is the one place it legitimately happens."""
    _HAS.clear()


__all__ = ["exists", "reset_cache"]
