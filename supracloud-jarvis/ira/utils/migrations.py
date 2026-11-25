"""Schema migrations: inspection at runtime, application only by the migrator.

**The application no longer applies migrations.** It used to, on every boot,
through the ordinary connection pool — and that single line was the reason the
runtime role owned every table: DDL requires ownership, so the role serving
requests also held the authority to `ALTER TABLE … NO FORCE ROW LEVEL SECURITY`,
drop a policy, or change ownership.

`FORCE ROW LEVEL SECURITY` therefore constrained the application's *queries* and
not the application's *role*, which is the gap D-18 and R-50 record and this
module's split closes.

Two entry points now:

* :func:`migration_status` — read-only. Safe from any role, used by readiness.
  It answers "is the schema at the level this code expects" and nothing else.
* :func:`apply_migrations` — called **only** by `scripts/migrate.py`, over a
  separate migrator DSN, behind an advisory lock and a set of refusals.

The docker-entrypoint-initdb.d mounts remain for first-boot speed on a brand-new
volume. They are not the durable path and never were the security boundary.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("ira.migrations")

# The repository's migration directory (supracloud-jarvis/postgres), for a clone. The deployment
# image copies the files elsewhere and says where with IRA_MIGRATIONS_DIR (deploy/Dockerfile.api).
_POSTGRES_DIR: Path = (Path(__file__).resolve().parent.parent.parent / "postgres").resolve()


def migrations_dir() -> Path:
    """Where the migration files are: IRA_MIGRATIONS_DIR if set, else the repository's."""
    override = (os.getenv("IRA_MIGRATIONS_DIR") or "").strip()
    return Path(override).resolve() if override else _POSTGRES_DIR

#: One fixed key for the migration advisory lock. Arbitrary, but it must be
#: stable across processes and releases: two deployments rolling out at once
#: have to serialise, and the second must fail fast rather than interleave its
#: DDL with the first.
MIGRATION_LOCK_KEY = 7_312_004_119_552_001
_LOCK_KEY = MIGRATION_LOCK_KEY


def migration_files() -> list[Path]:
    """Every migration file, in the order it must be applied.

    `init.sql` is the BASE schema and the numbered files build on it. Plain
    `sorted()` puts `init.sql` AFTER `002…009` (`'i' > '0'`), so on a fresh
    database the numbered migrations would run before the tables they reference
    exist.

    `provisioning/` is deliberately not reachable from here: it is a
    subdirectory, this glob is top-level, and role creation is an operator
    decision rather than a migration.
    """
    where = migrations_dir()
    if not where.is_dir():
        return []
    files = sorted(where.glob("*.sql"))
    files.sort(key=lambda p: (p.name != "init.sql", p.name))
    return files


async def _applied(conn: Any) -> set[str]:
    # `to_regclass`, NOT information_schema. `information_schema.tables` only
    # shows tables the current role holds some privilege on, so a role with no
    # grant sees "table does not exist" and this function would report every
    # migration as pending. That is the same "cannot see it, so it must not be
    # there" confusion that made a status view report contract_ready on a table
    # full of plaintext, and it is worth refusing to repeat.
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    if not exists:
        return set()
    return {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}


async def migration_status(conn: Any) -> dict[str, Any]:
    """What is applied and what is not. **Read-only — applies nothing.**

    Used by the readiness check. A connection holding only `SELECT` on
    `schema_migrations` can call this, which is the point: after `042` the
    runtime role cannot write that table, so it cannot mark an unapplied
    migration as applied and silence its own readiness failure.
    """
    applied = await _applied(conn)
    names = [p.name for p in migration_files()]
    pending = [n for n in names if n not in applied]
    return {
        "total": len(names),
        "applied": len(applied & set(names)),
        "pending": pending,
        # No files at all is a wrong path, not a database with nothing left to do.
        "up_to_date": bool(names) and not pending,
    }


class MigrationRefused(RuntimeError):
    """The connected role or environment is not permitted to apply migrations."""


async def assert_may_apply(conn: Any, *, authorised_for_production: bool) -> dict[str, Any]:
    """Refuse to apply migrations unless every precondition holds.

    Each refusal below is a specific failure that has either happened or was one
    configuration mistake away:

    * **the runtime roles** — applying migrations as `ira_app` is the state D-18
      exists to end. It cannot work anyway (no ownership), but it would fail
      halfway through a DDL statement rather than before the first one.
    * **superuser** — a superuser bypasses row-level security, so a backfill
      written against RLS semantics behaves differently under it than in
      production. That divergence hid two defects for a whole phase.
    * **BYPASSRLS** — same reason, without the superuser flag to make it obvious.
    * **the wrong database in production** — the cheapest possible guard against
      applying a migration chain to a database that merely has a similar name.
    * **missing authorisation** — a tool that silently works everywhere is a
      tool that eventually runs somewhere it should not.
    """
    from core.runtime.environment import get_environment

    row = await conn.fetchrow(
        "SELECT current_user AS role, current_database() AS db, "
        "       r.rolsuper, r.rolbypassrls "
        "  FROM pg_roles r WHERE r.rolname = current_user"
    )
    role, db = row["role"], row["db"]
    env = get_environment().value

    if role in ("ira_app", "ira_worker"):
        raise MigrationRefused(
            f"connected as {role!r}, which is a runtime role. Migrations run over "
            f"the migrator DSN as `ira_migrator`. This separation is the whole of "
            f"D-18: a runtime role that can apply migrations is a runtime role "
            f"that owns the tables and can disable their policies."
        )
    if row["rolsuper"]:
        raise MigrationRefused(
            f"connected as {role!r}, a SUPERUSER. A superuser bypasses row-level "
            f"security, so backfills behave differently here than in production — "
            f"which is exactly how two defects survived a whole phase of "
            f"'verified' migrations. Use `ira_migrator`."
        )
    if row["rolbypassrls"]:
        raise MigrationRefused(
            f"connected as {role!r}, which holds BYPASSRLS. Same problem as "
            f"superuser, without the flag that makes it obvious."
        )

    is_production = env in ("production", "staging")
    if is_production and not authorised_for_production:
        raise MigrationRefused(
            f"IRA_ENVIRONMENT={env}. Applying migrations here requires "
            f"--i-am-authorised-to-migrate-production."
        )

    expected_db = _expected_database()
    if is_production and expected_db and db != expected_db:
        raise MigrationRefused(
            f"connected to database {db!r} but IRA_MIGRATION_DATABASE is "
            f"{expected_db!r}. Refusing rather than migrating a database that "
            f"merely has a similar name."
        )

    return {"role": role, "database": db, "environment": env}


def _expected_database() -> str:
    import os

    return (os.getenv("IRA_MIGRATION_DATABASE") or "").strip()


async def apply_migrations(conn: Any, *, authorised_for_production: bool = False) -> dict[str, Any]:
    """Apply every pending migration, under an advisory lock.

    **Not called by the application.** `scripts/migrate.py` is the only caller.

    Each file runs in its own transaction and is recorded on success, so an
    interrupted run resumes at the file that failed rather than replaying the
    ones that worked.
    """
    identity = await assert_may_apply(
        conn, authorised_for_production=authorised_for_production
    )
    if not migration_files():
        # Before: "nothing to apply", and a fresh database stayed empty while the server started.
        raise MigrationRefused(
            f"no migration files found in {migrations_dir()}. Set IRA_MIGRATIONS_DIR to the "
            "directory that holds them; refusing rather than reporting an empty database as up to date."
        )

    got_lock = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK_KEY)
    if not got_lock:
        raise MigrationRefused(
            "another migration process holds the migration lock. Two concurrent "
            "rollouts would interleave DDL; refusing rather than racing."
        )

    try:
        # ── Become the schema owner, explicitly ──────────────────────────────
        #
        # `ira_migrator` is NOINHERIT, so membership in `ira_schema_owner` grants
        # nothing until it is claimed. That is the point: without this line the
        # migrator holds USAGE on `public` and not CREATE, and the first
        # `CREATE TABLE` fails with `permission denied for schema public`.
        #
        # Claiming it here rather than at connection time keeps the window as
        # narrow as the work requires, and makes "when did the migrator use
        # ownership" answerable from this one place.
        #
        # Every object the chain creates is therefore owned by
        # `ira_schema_owner` from the moment it exists — `042` transfers what
        # already existed; this stops anything new arriving under another owner.
        assumed_owner = await _assume_schema_owner(conn)

        # Owned by the schema owner, so the runtime cannot write it — see 042.
        # `042` also applies the grants, but it may not have run yet on a fresh
        # database, so they are re-applied at the end rather than assumed.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        applied = await _applied(conn)
        results: list[str] = []
        for sql_file in migration_files():
            if sql_file.name in applied:
                continue
            logger.info("migrations: applying %s", sql_file.name)
            async with conn.transaction():
                await conn.execute(sql_file.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES ($1) "
                    "ON CONFLICT DO NOTHING",
                    sql_file.name,
                )
            results.append(sql_file.name)

        await _grant_migration_table(conn)
        return {**identity, "applied": results, "as_owner": assumed_owner}
    finally:
        # RESET before releasing, so a pooled or reused connection never leaves
        # this function still wearing the owner's privileges.
        try:
            await conn.execute("RESET ROLE")
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)


async def _assume_schema_owner(conn: Any) -> bool:
    """`SET ROLE ira_schema_owner`, if this role is a member of it.

    Returns whether it happened. A superuser or a legacy single-role deployment
    is not a member and needs nothing; anything else that is not a member will
    fail on the first DDL statement with a message naming the schema, which is
    why the failure is raised here with a message naming the cause instead.
    """
    owner_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_schema_owner')")
    if not owner_exists:
        logger.warning(
            "migrations: ira_schema_owner does not exist — applying as %s. Run "
            "postgres/provisioning/001_database_roles.sql to separate the roles.",
            await conn.fetchval("SELECT current_user"))
        return False

    is_member = await conn.fetchval(
        "SELECT pg_has_role(current_user, 'ira_schema_owner', 'MEMBER')")
    if not is_member:
        raise MigrationRefused(
            "ira_schema_owner exists but this role is not a member of it, so the "
            "chain would fail on its first CREATE with `permission denied for "
            "schema public`. Grant it: GRANT ira_schema_owner TO ira_migrator."
        )

    await conn.execute("SET ROLE ira_schema_owner")
    return True


async def _grant_migration_table(conn: Any) -> None:
    """Runtime may read the migration history and may not write it.

    A runtime that could INSERT here could mark an unapplied migration as
    applied, and the readiness check that exists to catch an un-migrated
    database would then pass on one.
    """
    for role in ("ira_app", "ira_worker", "ira_migrator"):
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", role
        )
        if not exists:
            continue
        await conn.execute(f"REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {role}")
        await conn.execute(f"GRANT SELECT ON schema_migrations TO {role}")

    # `ira_migrator` is included so `status` and `verify` can read the history
    # WITHOUT claiming ownership. Its writes still go through SET ROLE, so the
    # revoke above costs it nothing and keeps the read path privilege-free.


async def run_migrations(pool: Any) -> None:
    """**Removed.** Kept as a loud failure rather than deleted.

    This was called from `app/lifespan.py` on every boot. Deleting it outright
    would give a stale caller an `ImportError` naming a module rather than the
    reason, and somebody would restore it.
    """
    raise MigrationRefused(
        "run_migrations() no longer exists: the application must not apply "
        "migrations. Doing so requires the runtime role to own the tables, which "
        "lets it disable the row-level security every isolation guarantee rests "
        "on (D-18, R-50). Run `python -m scripts.migrate apply --dsn <migrator>` "
        "as a deployment step; startup now reports readiness FALSE while "
        "migrations are pending instead of applying them."
    )
