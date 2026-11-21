"""The destructive rollbacks refuse while real tenants hold the rows they drop.

Batch 5C's CI step clears fixture rows before running the rollback cycle,
because the erasure suites now provision a workspace per test tenant and the
rollback refuses while more than one tenant holds one.

The guard lives in `rollback/033_organisations_workspaces_down.sql`, not in
`039` — `033` is the migration that CREATES `workspaces`, so its rollback is
the one that drops the table. `039` only seeds the default workspaces on top.
That distinction cost a test run to discover, and is recorded here so the next
reader does not go looking in the wrong file.

The CI cleanup is correct — those fixture rows are disposable — but it creates
a specific risk:

    a guard that is always stepped around stops being tested, and the next
    person to find it inconvenient deletes it.

So this file drives `rollback/033_organisations_workspaces_down.sql` against a
database that DOES have several tenants holding workspaces, and asserts it
refuses and changes nothing. If somebody "fixes" the CI failure by adding an
automatic `DELETE FROM workspaces` to the rollback script, or by dropping the
guard, this fails.

The same shape covers 046 and 047, whose guards were also planted-and-verified
by hand during Batch 5B and 5C and are now pinned here.

Runs against a real PostgreSQL: a `RAISE EXCEPTION` inside a `DO` block is not
something a mock demonstrates.
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

ROLLBACK = pathlib.Path(__file__).resolve().parents[3] / "postgres" / "rollback"


class World:
    pass


@pytest.fixture
async def two_tenants_with_workspaces():
    """Two tenants, each holding a workspace — the state 033 refuses over."""
    w = World()
    w.tenants = [uuid.uuid4(), uuid.uuid4()]
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for t in w.tenants:
            await conn.execute(
                "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
                t, f"guard-{t.hex[:8]}", f"guard-{t.hex[:8]}")
            org = await conn.fetchval(
                "INSERT INTO organisations (tenant_id,name) VALUES ($1,'G') RETURNING id", t)
            await conn.execute(
                """INSERT INTO workspaces (tenant_id,organisation_id,name,slug,
                                           is_default,status)
                   VALUES ($1,$2,'Default',$3,TRUE,'active')""",
                t, org, f"g-{t.hex[:8]}")
        yield w
    finally:
        for t in w.tenants:
            await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM tenants WHERE id=$1", t)
        await conn.close()


async def _run_script(name: str):
    """Execute a rollback script inside a transaction that is ALWAYS rolled back.

    Two reasons, and the second one cost a broken test database to learn:

    1. A `RAISE` inside a multi-statement script aborts that connection's
       implicit transaction, so running it on the caller's connection would
       make every later assertion fail with `InFailedSQLTransactionError`
       rather than on its own merits. Hence a separate connection.

    2. **A rollback script is destructive, and these run against the shared
       test database.** `rollback/033` is not wrapped in `BEGIN`/`COMMIT`, so
       under the simple query protocol its statements commit individually —
       anything before the guard takes effect for real. An explicit
       transaction here means the script can drop whatever it likes and none
       of it survives, whether it refuses or not.

    A guard test must not be able to damage the database it is testing.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute((ROLLBACK / name).read_text(encoding="utf-8"))
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def test_rollback_033_refuses_while_several_tenants_hold_workspaces(
    two_tenants_with_workspaces,
):
    """The guard the CI fixture cleanup steps around, driven directly.

    Dropping `workspaces` while two tenants hold them destroys the boundary
    those rows define — the rollback would take the content separation with it
    and there would be nothing left to say which rows belonged to whom.
    """
    # Snapshot the objects a destructive rollback would take with it, BEFORE
    # and after. `465ea85` shipped a version of this test that ran the script
    # for real and dropped `provision_tenant_substrate`; the whole suite then
    # failed against a database the guard test had taken apart. Asserting the
    # objects survive is how that becomes visible here rather than eighty
    # errors later.
    critical = ("public.workspaces", "public.organisations")

    async def snapshot():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            objects = {c: await conn.fetchval("SELECT to_regclass($1)", c) for c in critical}
            objects["provision_tenant_substrate"] = await conn.fetchval(
                "SELECT count(*) FROM pg_proc WHERE proname = 'provision_tenant_substrate'")
            objects["workspace_rows"] = await conn.fetchval(
                "SELECT count(*) FROM workspaces WHERE tenant_id = ANY($1)",
                two_tenants_with_workspaces.tenants)
            return objects
        finally:
            await conn.close()

    before = await snapshot()
    assert all(before[c] is not None for c in critical), before
    assert before["provision_tenant_substrate"] == 1
    assert before["workspace_rows"] == 2

    with pytest.raises(asyncpg.exceptions.RaiseError) as e:
        await _run_script("033_organisations_workspaces_down.sql")

    assert await snapshot() == before, (
        "the refusal test changed the database. A destructive rollback script "
        "must run inside a transaction that is always rolled back, or against "
        "a disposable database — never against the one the suite uses."
    )

    message = str(e.value)
    assert "Refusing rollback" in message
    assert "tenants hold workspaces" in message




def test_rollback_033_contains_no_automatic_deletion():
    """The guard must refuse, never resolve.

    Adding `DELETE FROM workspaces` here would make the CI cycle pass and
    would silently destroy a real deployment's content boundary on the first
    rollback. The fixture cleanup belongs in the CI step, where it is scoped
    to a disposable database; it must never migrate into the script.
    """
    source = (ROLLBACK / "033_organisations_workspaces_down.sql").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("delete from workspaces", "truncate workspaces",
                      "delete from organisations", "truncate organisations"):
        assert forbidden not in lowered, (
            f"rollback/033 now contains {forbidden!r}. That turns a refusal "
            f"into a silent destruction of the content boundary. Clear "
            f"disposable fixtures in the CI step instead."
        )
    assert "RAISE EXCEPTION" in source, "the refusal has been removed from rollback/033"


def test_the_workspace_scoped_rollbacks_all_still_refuse_rather_than_resolve():
    """046 and 047 carry the same contract, verified the same way.

    Both were planted-and-checked by hand when they were written; this pins
    them so the property survives the next edit.
    """
    for name in ("046_workspace_constraints_down.sql",
                 "047_graph_scope_constraints_down.sql"):
        source = (ROLLBACK / name).read_text(encoding="utf-8")
        assert "RAISE EXCEPTION" in source, f"{name} no longer refuses"
        assert "Refusing to restore" in source, f"{name} lost its refusal message"
        lowered = source.lower()
        assert "delete from entities" not in lowered, name
        assert "delete from agents" not in lowered, name
        assert "delete from edges" not in lowered, name
