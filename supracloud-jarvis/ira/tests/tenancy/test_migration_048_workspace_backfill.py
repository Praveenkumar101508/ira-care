"""048 places what is determined and refuses what is not.

The migration's whole value is in the second half. Backfilling a tenant that has
one workspace is arithmetic and hard to get wrong; the thing that matters is
that it does NOT quietly place rows belonging to a tenant with several
workspaces, because that would be a migration silently deciding which of a
customer's workspaces their memories live in, and nobody would find out.

── How these run without damaging anything ─────────────────────────────────

Every test drives the real `postgres/048_workspace_backfill.sql`, inside an
explicit transaction that is ALWAYS rolled back. That is a standing rule here
after `465ea85`, where a guard test executed a rollback script for real against
the shared test database, and — because that script had no `BEGIN`/`COMMIT` —
committed the statements before its guard, dropping a function the whole suite
depended on. Five failures and eighty-six errors on the next run.

The script's own `BEGIN`/`COMMIT` are stripped before execution, because an
inner `COMMIT` would commit the OUTER transaction and defeat the containment.
`test_the_migration_is_wrapped_in_a_transaction` asserts they are in the file,
so stripping them here cannot hide their absence there.

Rolling back also lets each test start from a clean slate: the shared database
carries NULL rows from every other suite's fixtures, so a test that needs "no
outstanding rows" clears them inside the transaction and they come back.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from core.privacy.erasure import WORKSPACE_SCOPED_TABLES  # noqa: E402

POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "048_workspace_backfill.sql"
ROLLBACK = POSTGRES / "rollback" / "048_workspace_backfill_down.sql"

TABLES = tuple(sorted(WORKSPACE_SCOPED_TABLES))


def _body(path: pathlib.Path) -> str:
    """The script with its outermost BEGIN/COMMIT removed.

    Only the outermost pair: `BEGIN`/`END` inside a `DO $$ ... $$` block are
    PL/pgSQL block delimiters, not transaction control, and must survive. The
    pattern is anchored to a line containing nothing else, which is how the
    migration files are written and how the DO-block ones are not.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    text = re.sub(r"(?im)^COMMIT;\s*$", "", text)
    return text


class World:
    pass


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

    `033` installs a DEFERRABLE constraint trigger on `workspaces` enforcing one
    active default per tenant. A test that has just inserted a workspace still
    has that event pending, and `ALTER TABLE workspaces` — which the migration's
    RLS window performs — refuses while any trigger event is outstanding:

        cannot ALTER TABLE "workspaces" because it has pending trigger events

    `SET CONSTRAINTS ALL IMMEDIATE` fires them now. It is a statement about this
    transaction's timing and changes nothing the migration does; in production
    the migration runs on its own connection with nothing pending.
    """
    await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    await conn.execute(_body(path))


async def _clear_outstanding(conn) -> None:
    """Remove every pre-existing NULL-workspace row, inside the transaction.

    The shared database accumulates fixture rows from the whole suite, and 048
    refuses while any remain — so without this, every test would observe a
    refusal and none of them would be testing their own scenario. Rolled back
    with everything else.

    Child-first: `messages` references `conversations`, `commitments` and
    `lifeos_events` reference tenant-scoped parents. Deleting a parent first
    fails on the FK rather than cascading, which is the same ordering lesson
    `core/privacy/erasure.py` records.
    """
    order = (
        "messages", "edges", "entities", "commitments", "lifeos_events",
        "decisions", "reminders", "tasks", "projects", "people", "files",
        "companion_versions", "companions", "calendar_events",
        "memory_embeddings", "agents", "conversations",
    )
    assert set(order) == set(TABLES), "the delete order has drifted from the table list"
    for table in order:
        await conn.execute(f"DELETE FROM {table} WHERE workspace_id IS NULL")  # noqa: S608


async def _allow_null_workspace(conn, *tables: str) -> None:
    """Make a NULL workspace representable again, inside this transaction.

    Two protections now stand in the way of writing the row these tests exist to
    handle: `050` made the column NOT NULL, and `049`'s policies refuse it even
    on the owner's connection under FORCE RLS.

    Both are lifted here and both come back with the rollback. That is not a
    workaround — it is the only way to exercise a migration whose whole purpose
    is to deal with rows that were written *before* those protections existed.
    A test that could not create the state would be asserting about nothing.
    """
    for table in tables:
        await conn.execute(f"ALTER TABLE {table} ALTER COLUMN workspace_id DROP NOT NULL")
        await conn.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

async def _tenant(conn, *, workspaces: int) -> World:
    """A tenant holding `workspaces` active workspaces, and one NULL-workspace
    task."""
    w = World()
    w.tenant = uuid.uuid4()
    await conn.execute(
        "INSERT INTO tenants (id, name) VALUES ($1, $2)",
        w.tenant, f"m048-{w.tenant.hex[:8]}",
    )
    w.workspaces = []
    if workspaces:
        w.workspaces.append(
            await conn.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        )
        org = await conn.fetchval(
            "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant
        )
        for i in range(1, workspaces):
            ws = uuid.uuid4()
            await conn.execute(
                """INSERT INTO workspaces
                     (id, tenant_id, organisation_id, name, slug, is_default, status)
                   VALUES ($1,$2,$3,$4,$5,false,'active')""",
                ws, w.tenant, org, f"Extra {i}", f"m048-extra-{i}-{w.tenant.hex[:6]}",
            )
            w.workspaces.append(ws)
    await _allow_null_workspace(conn, "tasks")
    await conn.execute(
        """INSERT INTO tasks (tenant_id, workspace_id, title, status)
           VALUES ($1, NULL, $2, 'pending')""",
        w.tenant, f"m048-secret-title-{w.tenant.hex[:8]}",
    )
    return w


async def _null_tasks(conn, tenant) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM tasks WHERE tenant_id = $1 AND workspace_id IS NULL",
        tenant,
    )


# ─────────────────────────────────────────────────────────────────────────────
# It places what is determined
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_with_one_workspace_gets_its_rows_placed(db):
    await _clear_outstanding(db)
    w = await _tenant(db, workspaces=1)

    await _run_migration(db, MIGRATION)

    assert await _null_tasks(db, w.tenant) == 0
    placed = await db.fetchval(
        "SELECT workspace_id FROM tasks WHERE tenant_id = $1", w.tenant
    )
    assert placed == w.workspaces[0]


async def test_rows_that_already_had_a_workspace_are_left_alone(db):
    """The backfill is restricted to `workspace_id IS NULL`.

    A statement that moved placed rows as well would relocate content between
    workspaces — the exact thing the refusal below exists to prevent, arriving
    through the half of the migration nobody suspects.
    """
    await _clear_outstanding(db)
    w = await _tenant(db, workspaces=2)
    await db.execute(
        """INSERT INTO tasks (tenant_id, workspace_id, title, status)
           VALUES ($1,$2,'m048-already-placed','pending')""",
        w.tenant, w.workspaces[1],
    )
    # Give the tenant's NULL row a home so the migration can complete.
    await db.execute(
        "UPDATE tasks SET workspace_id = $1 WHERE tenant_id = $2 AND workspace_id IS NULL",
        w.workspaces[0], w.tenant,
    )

    await _run_migration(db, MIGRATION)

    still_there = await db.fetchval(
        "SELECT workspace_id FROM tasks WHERE tenant_id = $1 AND title = 'm048-already-placed'",
        w.tenant,
    )
    assert still_there == w.workspaces[1]


# ─────────────────────────────────────────────────────────────────────────────
# It refuses what is not
# ─────────────────────────────────────────────────────────────────────────────


async def test_it_refuses_when_a_tenant_holds_several_workspaces(db):
    """The one that matters.

    Two workspaces and a homeless task: something has to decide which workspace
    the task belongs to, and that is a statement about somebody's content. A
    migration that picked the default, or the first, or the oldest would be
    indistinguishable from one that was right — until somebody looked.
    """
    await _clear_outstanding(db)
    w = await _tenant(db, workspaces=2)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        await _run_migration(db, MIGRATION)

    message = str(exc.value)
    assert "AMBIGUOUS" in message
    assert str(w.tenant) in message
    assert "workspaces=2" in message


async def test_it_refuses_when_a_tenant_holds_no_workspace(db):
    await _clear_outstanding(db)
    w = await _tenant(db, workspaces=0)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        await _run_migration(db, MIGRATION)

    message = str(exc.value)
    assert "HOMELESS" in message
    assert str(w.tenant) in message


async def test_the_two_refusals_are_reported_separately(db):
    """They need opposite remedies, so merging them would send the operator
    looking for content decisions that do not exist.

    An ambiguous row needs somebody to choose a workspace. A homeless row needs
    the tenant provisioned or the row removed. "1584 rows need a decision" and
    "1584 rows are in tenants that were never provisioned" are different
    findings with different amounts of work behind them.
    """
    await _clear_outstanding(db)
    ambiguous = await _tenant(db, workspaces=2)
    homeless = await _tenant(db, workspaces=0)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        await _run_migration(db, MIGRATION)

    message = str(exc.value)
    head, _, tail = message.partition("HOMELESS")
    assert str(ambiguous.tenant) in head, "the ambiguous tenant is not under AMBIGUOUS"
    assert str(homeless.tenant) in tail, "the homeless tenant is not under HOMELESS"
    assert str(homeless.tenant) not in head
    assert str(ambiguous.tenant) not in tail


async def test_a_refusal_changes_nothing(db):
    """Placed rows and unplaced rows both survive a refusal unchanged.

    The migration does its backfill BEFORE the refusal check, so "it refused"
    and "it left the database alone" are two different claims. The transaction
    is what makes the second one true, and this is the test that it does.
    """
    await _clear_outstanding(db)
    placeable = await _tenant(db, workspaces=1)
    blocker = await _tenant(db, workspaces=2)

    # A savepoint, so the connection is still usable after the RAISE. Without
    # one, the abort leaves the session in `InFailedSQLTransactionError` and the
    # assertions below cannot run at all — which would make this test pass or
    # error for reasons unrelated to what it is checking. The savepoint also
    # models the real thing: 048's own BEGIN/COMMIT would discard the same work.
    with pytest.raises(asyncpg.exceptions.RaiseError):
        async with db.transaction():
            await _run_migration(db, MIGRATION)

    # The placeable tenant's row was updated in step 2 and must be back to NULL:
    # one tenant's unresolvable state stops the whole migration, which is the
    # point of doing it in one transaction.
    assert await _null_tasks(db, placeable.tenant) == 1
    assert await _null_tasks(db, blocker.tenant) == 1


async def test_the_refusal_names_no_row_contents(db):
    """Tenants, tables and counts. Not titles.

    A diagnostic that quoted the rows would put customer content into migration
    logs and into whatever collects them.
    """
    await _clear_outstanding(db)
    w = await _tenant(db, workspaces=0)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        await _run_migration(db, MIGRATION)

    assert "m048-secret-title" not in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# Properties of the file itself
# ─────────────────────────────────────────────────────────────────────────────


def test_the_migration_is_wrapped_in_a_transaction():
    """`_body()` strips these to run the script under test containment, so
    without this assertion their removal from the real file would be invisible
    here — and a partially-applied backfill is exactly the state
    `test_a_refusal_changes_nothing` claims cannot happen."""
    text = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r"(?im)^BEGIN;\s*$", text), "048 is not wrapped in a transaction"
    assert re.search(r"(?im)^COMMIT;\s*$", text), "048 never commits"


def test_it_covers_every_workspace_scoped_table():
    """The table list is written out in SQL and cannot import the Python
    frozenset, so this is the join between them.

    A table missing from the migration would keep its NULL rows, pass the
    migration, and go invisible on the day the policies land.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    missing = [t for t in TABLES if f"'{t}'" not in text]
    assert not missing, (
        f"048 does not mention {missing}. Those tables are workspace-scoped, so "
        f"their NULL rows would survive the backfill and the refusal alike."
    )
    assert len(TABLES) == 17


def test_the_backfill_only_touches_null_rows():
    """Read from the SQL, because the behavioural test above can only observe
    the rows it created. Every UPDATE in this migration is guarded."""
    text = MIGRATION.read_text(encoding="utf-8")
    # Anchored on the actual statement, not the word. The RLS-window comment
    # added to this migration contains "UPDATE" in prose, and a loose pattern
    # matched that instead — a test that passes by reading a comment.
    updates = re.findall(r"UPDATE\s+%I\s+AS\s+target.*?(?=\$fmt\$)", text, re.S | re.I)
    assert updates, "no UPDATE found — has the backfill been removed?"
    for statement in updates:
        assert "workspace_id IS NULL" in statement, (
            "an UPDATE in 048 is not restricted to rows with no workspace. "
            "Unrestricted, it would relocate content between workspaces."
        )


def test_the_rollback_refuses_rather_than_clearing_workspaces(db):
    """Reversing 048 would recreate the invisibility it exists to prevent.

    A NULL `workspace_id` is not neutral: under a workspace policy the
    predicate evaluates to NULL rather than false, and a predicate that is not
    TRUE denies. So the rollback stops, and this pins that it does — the
    tempting "fix" when the rollback cycle fails is to make it clear the column.
    """
    source = ROLLBACK.read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in source
    assert "Refusing to reverse 048" in source
    lowered = source.lower()
    assert "set workspace_id = null" not in lowered, (
        "rollback/048 now clears workspace_id. That manufactures rows no policy "
        "can read, which is the defect 048 exists to remove."
    )


async def test_the_rollback_refuses_when_actually_run(db):
    """The text assertions above check the file; this checks the behaviour."""
    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        await _run_migration(db, ROLLBACK)

    assert "Refusing to reverse 048" in str(exc.value)
