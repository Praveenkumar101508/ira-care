"""049 makes the workspace boundary a database boundary — and refuses first.

`test_phase3b_batch5c.py` proves what the policies deny, through the application
helpers, which is the question that matters. This file is about the migration
itself: that it refuses to apply where applying would hide rows, that the shape
it leaves behind is the shape it claims, and that its rollback really reverses.

The distinction is worth keeping separate. A policy set can deny everything
correctly and still be wrong — one leftover permissive policy from before 049 is
OR-ed with the new ones and reopens the boundary while every new policy reads
perfectly in a listing.

Every test drives the real `postgres/049_workspace_policies.sql` inside a
transaction that is ALWAYS rolled back. DDL is transactional in PostgreSQL, so
dropping and recreating seventeen tables' worth of policies leaves nothing
behind — the standing rule since `465ea85`, where a guard test ran a script for
real and took the suite's database apart.
"""
from __future__ import annotations

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
MIGRATION = POSTGRES / "049_workspace_policies.sql"
ROLLBACK = POSTGRES / "rollback" / "049_workspace_policies_down.sql"

TABLES = tuple(sorted(WORKSPACE_SCOPED_TABLES))
EXPECTED = {"workspace_select", "workspace_insert", "workspace_update", "workspace_delete"}


def _body(path: pathlib.Path) -> str:
    """The script without its outermost BEGIN/COMMIT.

    An inner COMMIT would commit the OUTER transaction and defeat the
    containment entirely. `test_the_migration_is_wrapped_in_a_transaction`
    asserts both are present in the real file, so stripping them here cannot
    conceal their removal there.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    return re.sub(r"(?im)^COMMIT;\s*$", "", text)


@pytest.fixture
async def db():
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


async def _policies(conn, table: str) -> dict[str, dict]:
    rows = await conn.fetch(
        """
        SELECT polname AS name,
               polcmd AS cmd,
               pg_get_expr(polqual, polrelid)      AS using_expr,
               pg_get_expr(polwithcheck, polrelid) AS check_expr
          FROM pg_policy WHERE polrelid = $1::regclass
        """,
        table,
    )
    return {r["name"]: dict(r) for r in rows}


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

async def _clear_outstanding(conn) -> None:
    """Remove every pre-existing NULL-workspace row, inside the transaction.

    Only a superuser fixture can create one now — the policies refuse it — but
    the shared database accumulates them from suites that build their rows on
    the admin connection. 049's preflight refuses while any exist, so without
    this the re-apply tests would observe somebody else's leftover rather than
    their own scenario. Rolled back with everything else.

    Child-first: `messages` references `conversations`, and `commitments` and
    `lifeos_events` reference tenant-scoped parents.
    """
    for table in ("messages", "edges", "entities", "commitments", "lifeos_events",
                  "decisions", "reminders", "tasks", "projects", "people", "files",
                  "companion_versions", "companions", "calendar_events",
                  "memory_embeddings", "agents", "conversations"):
        await conn.execute(f"DELETE FROM {table} WHERE workspace_id IS NULL")  # noqa: S608


# ─────────────────────────────────────────────────────────────────────────────
# The shape it leaves behind
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", TABLES)
async def test_every_scoped_table_has_exactly_the_four_policies(db, table):
    """Exactly four, and exactly these four.

    "At least four" would pass with a leftover `tenant_isolation FOR ALL` still
    attached — and a leftover permissive policy is OR-ed with the new ones, so
    the boundary would be open while every policy in the listing looked right.
    That is the failure mode this assertion is shaped around.
    """
    names = set(await _policies(db, table))
    assert names == EXPECTED, f"{table} has {sorted(names)}, expected {sorted(EXPECTED)}"


@pytest.mark.parametrize("table", TABLES)
async def test_every_predicate_requires_both_settings(db, table):
    """Tenant alone is the pre-5C boundary; workspace alone would be a
    boundary with no tenant check at all. Both, in every arm that has one."""
    # Keyed off the policy NAME rather than `pg_policy.polcmd`, whose `"char"`
    # type asyncpg hands back in a form that is easy to compare wrongly — and a
    # wrong comparison here silently checks the wrong arm.
    for name, policy in (await _policies(db, table)).items():
        arms = []
        if name != "workspace_insert":  # INSERT has no USING clause
            arms.append(("USING", policy["using_expr"]))
        if name in ("workspace_insert", "workspace_update"):
            arms.append(("WITH CHECK", policy["check_expr"]))
        assert arms, f"{table}.{name} has neither arm"
        for label, expr in arms:
            assert expr, f"{table}.{name} {label} is empty"
            assert "app.tenant_id" in expr, f"{table}.{name} {label} lost the tenant"
            assert "app.workspace_id" in expr, f"{table}.{name} {label} lost the workspace"


@pytest.mark.parametrize("table", TABLES)
async def test_the_update_policy_states_both_arms(db, table):
    """USING alone would let a row be read from the caller's workspace and
    written into another — it passes the read check on the way in and lands
    anywhere. A cross-workspace write dressed as an edit."""
    update = (await _policies(db, table))["workspace_update"]
    assert update["using_expr"], f"{table}.workspace_update has no USING"
    assert update["check_expr"], (
        f"{table}.workspace_update has no WITH CHECK, so an UPDATE could move a "
        f"row into another workspace")


@pytest.mark.parametrize("table", TABLES)
async def test_forced_rls_survives(db, table):
    """Asserted rather than trusted. Dropping and recreating every policy on a
    table is exactly the operation during which somebody turns the protections
    off to get it applied."""
    row = await db.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
        table,
    )
    assert row["relrowsecurity"] and row["relforcerowsecurity"], (
        f"{table} does not have ENABLE + FORCE ROW LEVEL SECURITY")


# ─────────────────────────────────────────────────────────────────────────────
# The preflight
# ─────────────────────────────────────────────────────────────────────────────


async def test_it_refuses_while_a_row_has_no_workspace(db):
    """The guarantee that makes 048 a prerequisite rather than a suggestion.

    A NULL `workspace_id` matches nothing under the new predicate — the
    comparison is NULL, not false, and a predicate that is not TRUE denies. So
    the row would vanish from every read the instant this applied, with no error
    raised anywhere. Refusing is the only correct behaviour and this is the test
    that it does.

    The planted row goes in on the admin connection, which bypasses RLS. Nothing
    else could create it: since 049 the policies themselves refuse a NULL
    workspace, which is the point.
    """
    tenant = uuid.uuid4()
    await db.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                     tenant, f"m049-{tenant.hex[:8]}")
    await _allow_null_workspace(db, "tasks")
    await db.execute(
        "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
        "VALUES ($1, NULL, 'm049-orphan', 'pending')", tenant)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        async with db.transaction():
            await _run_migration(db, MIGRATION)

    message = str(exc.value)
    assert "Refusing to apply workspace policies while rows have no workspace" in message
    assert "tasks: 1 row(s)" in message


async def test_the_refusal_names_no_row_contents(db):
    """Tables and counts. Not titles — a NULL row's contents are customer data
    and migration output ends up in logs."""
    tenant = uuid.uuid4()
    await db.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                     tenant, f"m049-{tenant.hex[:8]}")
    await _allow_null_workspace(db, "tasks")
    await db.execute(
        "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
        "VALUES ($1, NULL, 'm049-secret-title', 'pending')", tenant)

    with pytest.raises(asyncpg.exceptions.RaiseError) as exc:
        async with db.transaction():
            await _run_migration(db, MIGRATION)

    assert "m049-secret-title" not in str(exc.value)


async def test_a_refusal_leaves_the_policies_untouched(db):
    """The migration drops policies before it would recreate them, so "it
    refused" and "it changed nothing" are two claims. The transaction is what
    makes the second true — and the preflight running FIRST is what makes it
    true even without one."""
    tenant = uuid.uuid4()
    await db.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                     tenant, f"m049-{tenant.hex[:8]}")
    await _allow_null_workspace(db, "tasks")
    await db.execute(
        "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
        "VALUES ($1, NULL, 'm049-blocker', 'pending')", tenant)

    before = set(await _policies(db, "tasks"))
    with pytest.raises(asyncpg.exceptions.RaiseError):
        async with db.transaction():
            await _run_migration(db, MIGRATION)

    assert set(await _policies(db, "tasks")) == before


async def test_it_reapplies_cleanly_when_nothing_is_outstanding(db):
    """Idempotent. Re-running it on a database that already carries the policies
    reads every existing one from the catalogue, drops them, and puts the same
    four back — which is what makes the CI rollback cycle able to re-apply the
    whole chain."""
    await _clear_outstanding(db)
    await _run_migration(db, MIGRATION)
    assert set(await _policies(db, "tasks")) == EXPECTED


# ─────────────────────────────────────────────────────────────────────────────
# The rollback
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_rollback_removes_every_workspace_predicate(db):
    """It reverses, unlike 046, 047 and 048 — because dropping an arm from a
    predicate deletes nothing and moves nothing. It also WIDENS access back to
    the pre-5C state, which the script says in its own header rather than
    leaving a reader to infer."""
    await _run_migration(db, ROLLBACK)

    for table in TABLES:
        for name, policy in (await _policies(db, table)).items():
            for expr in (policy["using_expr"], policy["check_expr"]):
                assert not expr or "app.workspace_id" not in expr, (
                    f"{table}.{name} still reads app.workspace_id after the rollback")


async def test_the_rollback_restores_the_two_original_shapes(db):
    """Ten tables carried one `FOR ALL` policy before 049 and seven carried four
    per-command ones. Restoring everything on the tidier shape would make CI's
    "the recomposed schema matches a fresh one" comparison wrong — or worse,
    let it pass while the two databases genuinely differed."""
    await _run_migration(db, ROLLBACK)

    for table in ("commitments", "companion_versions", "companions", "conversations",
                  "files", "lifeos_events", "memory_embeddings", "messages",
                  "people", "tasks"):
        assert set(await _policies(db, table)) == {"tenant_isolation"}, table

    for table in ("agents", "calendar_events", "decisions", "edges", "entities",
                  "projects", "reminders"):
        assert set(await _policies(db, table)) == {
            "tenant_select", "tenant_insert", "tenant_update", "tenant_delete"}, table


async def test_the_rollback_keeps_forced_rls(db):
    await _run_migration(db, ROLLBACK)
    for table in TABLES:
        row = await db.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
            table)
        assert row["relrowsecurity"] and row["relforcerowsecurity"], table


async def test_apply_rollback_reapply_composes(db):
    """The cycle CI runs, driven here so a failure names the migration rather
    than appearing four steps later in a shell script."""
    await _clear_outstanding(db)
    await _run_migration(db, ROLLBACK)
    assert set(await _policies(db, "tasks")) == {"tenant_isolation"}

    await _run_migration(db, MIGRATION)
    assert set(await _policies(db, "tasks")) == EXPECTED


# ─────────────────────────────────────────────────────────────────────────────
# Properties of the files
# ─────────────────────────────────────────────────────────────────────────────


def test_the_migration_is_wrapped_in_a_transaction():
    text = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r"(?im)^BEGIN;\s*$", text), "049 is not wrapped in a transaction"
    assert re.search(r"(?im)^COMMIT;\s*$", text), "049 never commits"


def test_it_covers_every_workspace_scoped_table():
    """The table list is written out in SQL and cannot import the Python
    frozenset, so this is the join between them. A table missing from 049 keeps
    its tenant-only policy and stays readable across workspaces."""
    text = MIGRATION.read_text(encoding="utf-8")
    missing = [t for t in TABLES if f"'{t}'" not in text]
    assert not missing, f"049 does not mention {missing}"
    assert len(TABLES) == 17
