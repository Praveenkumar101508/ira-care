"""050 makes `workspace_id` NOT NULL, and the chain proves it actually did.

The constraint itself is a one-liner per table. The interesting half is what it
makes true elsewhere — 046's composite foreign keys are MATCH SIMPLE and were
vacuous while the column could be NULL — and the way this migration nearly
shipped doing nothing at all.

── The defect this file exists to keep closed ──────────────────────────────

Migrations run as the schema owner, and FORCE ROW LEVEL SECURITY applies to the
owner too. A migration that binds no `app.tenant_id` therefore counts zero rows
and updates zero rows **without failing**.

`045` knew this and opened a `NO FORCE` window over the seventeen content
tables. It then backfilled them using `default_workspace_id(tenant_id)` — a
function that reads `workspaces`, which was **not** in that window. So the
lookup returned NULL for every row, the backfill wrote NULL over NULL, and the
migration reported success having achieved nothing.

Nobody noticed, because a NULL `workspace_id` was legal until this batch. It
surfaced when 050's `SET NOT NULL` refused on a row that every preflight before
it had counted as absent.

`test_the_chain_actually_places_rows_when_run_as_the_migrator` is the guard.
It applies the whole chain to a disposable database **as `ira_migrator`**, which
is the only way to catch this class of defect: as a superuser everything works
and every assertion passes.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import uuid

from _dsn import as_role

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from core.privacy.erasure import WORKSPACE_SCOPED_TABLES  # noqa: E402

POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "050_workspace_not_null.sql"
ROLLBACK = POSTGRES / "rollback" / "050_workspace_not_null_down.sql"

TABLES = tuple(sorted(WORKSPACE_SCOPED_TABLES))


def _body(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    return re.sub(r"(?im)^COMMIT;\s*$", "", text)


@pytest.fixture
async def db():
    """A connection inside a transaction that is always rolled back."""
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _run_migration(conn, path: pathlib.Path) -> None:
    """Execute a migration body, with 033's deferred triggers settled first.

    `ALTER TABLE workspaces` — which the RLS window performs — refuses while a
    deferred constraint trigger event is outstanding, and a test that has just
    inserted a workspace has one. Firing them now is a statement about this
    transaction's timing, not about the migration.
    """
    await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    await conn.execute(_body(path))


# ─────────────────────────────────────────────────────────────────────────────
# The constraint
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", TABLES)
async def test_workspace_id_is_not_null(db, table):
    notnull = await db.fetchval(
        "SELECT attnotnull FROM pg_attribute "
        " WHERE attrelid = $1::regclass AND attname = 'workspace_id'",
        table,
    )
    assert notnull, f"{table}.workspace_id is still nullable"


@pytest.mark.parametrize("table", TABLES)
async def test_forced_rls_and_the_policies_survive(db, table):
    """An ALTER COLUMN cannot remove either, which is exactly why it is worth
    checking — the migration opens a NO FORCE window to do its counting, and a
    window that failed to close would hand the owner a hole in every policy."""
    row = await db.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
        table,
    )
    assert row["relrowsecurity"] and row["relforcerowsecurity"], table
    count = await db.fetchval(
        "SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename=$1",
        table,
    )
    assert count == 4, f"{table} has {count} policies, expected 049's four"


async def test_the_rls_window_closed_on_workspaces_too(db):
    """`workspaces` is in the window because the backfill READS it.

    It is the table whose omission from 045's window caused the whole defect, so
    it is the one most likely to be forgotten when the window is next edited.
    """
    row = await db.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        " WHERE oid = 'workspaces'::regclass")
    assert row["relrowsecurity"] and row["relforcerowsecurity"]


async def test_a_null_workspace_is_now_impossible(db):
    """Not merely unwritten — unrepresentable.

    049 already refuses this row through its policies, so this is the second
    independent reason it cannot exist. Two is the point: the policy protects
    while the constraint is absent, and the constraint protects if a policy is
    ever relaxed.
    """
    tenant = uuid.uuid4()
    await db.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                     tenant, f"m050-{tenant.hex[:8]}")
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await db.execute(
            "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
            "VALUES ($1, NULL, 'm050', 'pending')", tenant)


# ─────────────────────────────────────────────────────────────────────────────
# The preflight
# ─────────────────────────────────────────────────────────────────────────────


async def test_it_refuses_with_a_work_list_rather_than_one_row(db):
    """`SET NOT NULL` would refuse anyway — with a message naming one row and
    no context. The preflight names every table and how many, which is the
    difference between an error and a work list."""
    await _run_migration(db, ROLLBACK)  # make NULLs possible again

    tenant = uuid.uuid4()
    await db.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                     tenant, f"m050-{tenant.hex[:8]}")
    # 049's policies refuse a NULL workspace, so the row goes in with the
    # window open — the same mechanism the migration itself uses.
    await db.execute("ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY")
    await db.execute(
        "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
        "VALUES ($1, NULL, 'm050-orphan', 'pending')", tenant)
    await db.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        async with db.transaction():
            await _run_migration(db, MIGRATION)

    message = str(exc.value)
    assert "Refusing to make workspace_id NOT NULL" in message
    assert "tasks: 1 row(s)" in message
    assert "m050-orphan" not in message, "the refusal printed row content"


# ─────────────────────────────────────────────────────────────────────────────
# What 050 makes true elsewhere
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_composite_foreign_keys_span_the_workspace(db):
    """These are what stop a child row referencing a parent in another
    workspace. They were MATCH SIMPLE and therefore vacuous while the column
    was nullable; 050 is what activates them."""
    rows = await db.fetch(
        """
        SELECT conname, conrelid::regclass::text AS child,
               (SELECT array_agg(a.attname ORDER BY x.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS x(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = x.attnum
               ) AS cols
          FROM pg_constraint c
         WHERE contype = 'f' AND array_length(conkey, 1) > 1
           AND conrelid::regclass::text IN ('edges', 'reminders')
        """
    )
    assert rows, "no composite foreign keys found on edges or reminders"
    for row in rows:
        assert "workspace_id" in row["cols"], (
            f"{row['conname']} on {row['child']} does not span workspace_id")


@pytest.mark.parametrize("index", [
    "agents_tenant_workspace_name_key",
    "idx_entities_scope_type_name",
    "idx_edges_scope_src_dst_relation",
])
async def test_the_scoped_unique_indexes_include_the_workspace(db, index):
    definition = await db.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname=$1",
        index)
    assert definition is not None, f"{index} is missing"
    assert "workspace_id" in definition, f"{index} does not include workspace_id"


# ─────────────────────────────────────────────────────────────────────────────
# The rollback
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_rollback_drops_the_constraint_and_keeps_everything_else(db):
    await _run_migration(db, ROLLBACK)

    for table in TABLES:
        notnull = await db.fetchval(
            "SELECT attnotnull FROM pg_attribute "
            " WHERE attrelid = $1::regclass AND attname = 'workspace_id'", table)
        assert not notnull, f"{table}.workspace_id is still NOT NULL"

        row = await db.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid=$1::regclass",
            table)
        assert row["relrowsecurity"] and row["relforcerowsecurity"], (
            f"{table} lost forced RLS during the rollback")


async def test_apply_rollback_reapply_composes(db):
    await _run_migration(db, ROLLBACK)
    assert not await db.fetchval(
        "SELECT attnotnull FROM pg_attribute "
        " WHERE attrelid='tasks'::regclass AND attname='workspace_id'")

    await _run_migration(db, MIGRATION)
    assert await db.fetchval(
        "SELECT attnotnull FROM pg_attribute "
        " WHERE attrelid='tasks'::regclass AND attname='workspace_id'")


# ─────────────────────────────────────────────────────────────────────────────
# The regression guard for the whole class of defect
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.getenv("IRA_TEST_ADMIN_DSN", "").startswith("postgresql://postgres"),
    reason="needs a superuser DSN to create and drop a disposable database",
)
async def test_the_chain_actually_places_rows_when_run_as_the_migrator():
    """Apply the entire chain as `ira_migrator` and assert nothing is left NULL.

    This is the only shape of test that catches the defect. As a superuser every
    backfill works, every count is accurate and every assertion passes — the
    failure exists only on the path the product actually uses, which is why it
    survived `045`, `046` and `047` unnoticed.

    A disposable database, created and dropped here. Nothing touches the shared
    one: a chain application is not something to run against a database other
    tests are using.
    """
    import asyncpg as _asyncpg

    # Parsed, not string-split. The local harness DSN carries a socket
    # directory in its query string — `host=/var/lib/postgresql/run` — so
    # `rsplit("/", 1)` cuts inside the parameters and produces a DSN that
    # connects to a socket that does not exist. The failure reads as a missing
    # database rather than a malformed DSN, which cost a debugging round.
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(ADMIN_DSN)
    dbname = f"ira_chain_{uuid.uuid4().hex[:10]}"
    disposable = urlunsplit(parts._replace(path=f"/{dbname}"))

    admin = await _asyncpg.connect(ADMIN_DSN)
    try:
        # CREATE DATABASE cannot run inside a transaction block.
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    try:
        setup = await _asyncpg.connect(disposable)
        try:
            roles = (POSTGRES / "provisioning" / "001_database_roles.sql").read_text(
                encoding="utf-8")
            await setup.execute(roles)
            for role in ("ira_app", "ira_worker", "ira_migrator"):
                await setup.execute(f"ALTER ROLE {role} PASSWORD '{role}'")
        finally:
            await setup.close()

        # Parsed, not string-replaced — see tests/tenancy/_dsn.py. The old
        # substitution matched only a passwordless `postgresql://postgres@…`,
        # so a CI DSN with a password left this pointing at the SUPERUSER and
        # the migration command refused, which looked like a migration defect.
        migrator = as_role(disposable, "ira_migrator", "ira_migrator")
        result = subprocess.run(
            [sys.executable, "-m", "scripts.migrate", "apply", "--dsn", migrator],
            # `ira/`, where `scripts.migrate` is importable. parents[2] is
            # `tests/`'s grandparent only if this file moves; resolving from the
            # known package root is what stops a rename turning this guard into
            # a FileNotFoundError that reads like an environment problem.
            cwd=str(pathlib.Path(__file__).resolve().parents[2]),
            env={**os.environ, "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[2])},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"the chain failed to apply as ira_migrator:\n{result.stdout}\n{result.stderr}")

        # Read back as the SUPERUSER — unfiltered. Asking the migrator whether
        # it left any NULL rows is asking the blindfolded party to describe the
        # room, and is precisely how the original defect stayed hidden.
        check = await _asyncpg.connect(disposable)
        try:
            outstanding = []
            for table in TABLES:
                n = await check.fetchval(
                    f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")  # noqa: S608
                if n:
                    outstanding.append(f"{table}: {n}")
                notnull = await check.fetchval(
                    "SELECT attnotnull FROM pg_attribute "
                    " WHERE attrelid = $1::regclass AND attname = 'workspace_id'", table)
                assert notnull, f"{table}.workspace_id is nullable after the chain"
            assert not outstanding, (
                f"the chain left rows with no workspace: {outstanding}. A "
                f"backfill running as the owner under FORCE RLS matches zero "
                f"rows without failing — check the NO FORCE window covers every "
                f"table the migration READS, not only those it writes.")
        finally:
            await check.close()
    finally:
        admin = await _asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
