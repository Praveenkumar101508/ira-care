"""Both upgrade paths reach the same place: fresh, and repaired.

`045` recorded itself as applied having backfilled nothing — its NO FORCE window
covered the seventeen content tables but not `workspaces`, which
`default_workspace_id()` reads, so under FORCE RLS the lookup returned NULL for
every row and the backfill wrote NULL over NULL.

Correcting `045` fixes new installations and does nothing for existing ones: a
migration recorded as applied does not run again. So a fresh database and an
upgraded one would end up in different states while every migration reported
success — which is the failure this file exists to make impossible.

`050` carries the forward repair for that reason, and these are the two proofs
the repair is owed:

    FRESH        000 → 050 on an empty database
    AFFECTED     a database in the state a blind 045 leaves: Group A rows with
                 NULL workspace_id, 049's policies active, 050 not yet applied

── Why the affected state is constructed rather than replayed ──────────────

Replaying the defect would mean checking out the old `045`, applying it, and
restoring the new one — which tests a file that no longer exists in the tree and
would keep passing after the repair was deleted. What matters is not how the
rows came to be NULL; it is that a database in that condition is repaired. So
the state is built directly, and it is exactly the state an affected deployment
is in.

Every scenario uses a disposable database, created and dropped here. A migration
chain is not something to run against a database other tests are using.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

from _dsn import as_role

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)
if not ADMIN_DSN.startswith("postgresql://postgres"):
    pytest.skip("needs a superuser DSN to create disposable databases",
                allow_module_level=True)

from core.privacy.erasure import WORKSPACE_SCOPED_TABLES  # noqa: E402

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]
POSTGRES = IRA_ROOT.parent / "postgres"
TABLES = tuple(sorted(WORKSPACE_SCOPED_TABLES))

#: The ten tables whose 045 backfill was the blind one. Group B's tenant
#: backfill ran in the same window; it is the workspace lookup that failed, so
#: every table was affected — but Group A is where a real deployment's content
#: lives, and naming them keeps the scenario recognisable.
GROUP_A = ("commitments", "companion_versions", "companions", "conversations",
           "files", "lifeos_events", "memory_embeddings", "messages", "people",
           "tasks")


def _dsn_for(dbname: str) -> str:
    return urlunsplit(urlsplit(ADMIN_DSN)._replace(path=f"/{dbname}"))


class Disposable:
    """A database that exists for one scenario and is dropped afterwards."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.dsn = _dsn_for(name)
        # Parsed, not string-replaced. The substitution this used to do matched
        # only a passwordless `postgresql://postgres@…`, so a CI DSN carrying a
        # password left `self.migrator` pointing at the SUPERUSER — and every
        # test here then failed with the migration command's "REFUSING:
        # connected as 'postgres'", which reads like a migration defect rather
        # than a string that did not match. See tests/tenancy/_dsn.py.
        self.migrator = as_role(self.dsn, "ira_migrator", "ira_migrator")

    async def create(self) -> None:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            # CREATE DATABASE cannot run inside a transaction block.
            await admin.execute(f'CREATE DATABASE "{self.name}"')
        finally:
            await admin.close()

        setup = await asyncpg.connect(self.dsn)
        try:
            await setup.execute(
                (POSTGRES / "provisioning" / "001_database_roles.sql").read_text(
                    encoding="utf-8"))
            for role in ("ira_app", "ira_worker", "ira_migrator"):
                await setup.execute(f"ALTER ROLE {role} PASSWORD '{role}'")
        finally:
            await setup.close()

    async def drop(self) -> None:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        finally:
            await admin.close()

    def apply_chain(self) -> subprocess.CompletedProcess:
        """The whole chain, AS THE MIGRATOR.

        Never as a superuser. A superuser bypasses RLS entirely, so every
        backfill works, every count is accurate and this whole file would prove
        nothing about the path the product actually uses.
        """
        return subprocess.run(
            [sys.executable, "-m", "scripts.migrate", "apply", "--dsn", self.migrator],
            cwd=str(IRA_ROOT), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(IRA_ROOT)},
        )

    def apply_file(self, name: str) -> subprocess.CompletedProcess:
        """One migration file, as the migrator, with owner rights acquired.

        `ira_migrator` is NOINHERIT: membership in `ira_schema_owner` grants
        nothing until it is assumed explicitly, so `ALTER TABLE` fails with
        "must be owner of table". `utils/migrations.py` does the same `SET ROLE`
        before every file, and skipping it here would test a privilege state the
        product never runs in.

        `-c` and `-f` share one psql session and run in the order given.
        """
        return subprocess.run(
            ["psql", self.migrator, "-v", "ON_ERROR_STOP=1",
             "-c", "SET ROLE ira_schema_owner",
             "-f", str(POSTGRES / name)],
            capture_output=True, text=True,
        )


@pytest.fixture
async def disposable():
    db = Disposable(f"ira_up_{uuid.uuid4().hex[:10]}")
    await db.create()
    try:
        yield db
    finally:
        await db.drop()


async def _null_counts(dsn: str) -> dict[str, int]:
    """Read as the SUPERUSER — unfiltered.

    Asking the migrator whether it left NULL rows is asking the blindfolded
    party to describe the room, and is precisely how the original defect stayed
    hidden through three migrations.
    """
    conn = await asyncpg.connect(dsn)
    try:
        out = {}
        for table in TABLES:
            n = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")  # noqa: S608
            if n:
                out[table] = n
        return out
    finally:
        await conn.close()


async def _not_null_columns(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            """
            SELECT count(*) FROM pg_attribute
             WHERE attname = 'workspace_id' AND attnotnull
               AND attrelid = ANY($1::regclass[])
            """,
            list(TABLES),
        )
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Path 1 — a fresh installation
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_fresh_installation_reaches_050_with_nothing_outstanding(disposable):
    result = disposable.apply_chain()
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    assert await _null_counts(disposable.dsn) == {}
    assert await _not_null_columns(disposable.dsn) == 17

    conn = await asyncpg.connect(disposable.dsn)
    try:
        forced = await conn.fetchval(
            "SELECT count(*) FROM pg_class WHERE relname = ANY($1::text[])"
            "   AND relrowsecurity AND relforcerowsecurity",
            list(TABLES) + ["workspaces"])
        assert forced == 18, "forced RLS did not come back on every table"
        policies = await conn.fetchval(
            "SELECT count(*) FROM pg_policies WHERE schemaname='public'"
            "   AND tablename = ANY($1::text[]) AND qual LIKE '%app.workspace_id%'",
            list(TABLES))
        assert policies >= 17 * 3, "the workspace predicates are not on every table"
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Path 2 — a database that consumed the blind 045
# ─────────────────────────────────────────────────────────────────────────────


async def _become_affected(db: Disposable, *, workspaces_per_tenant: int = 1) -> dict:
    """Put an already-migrated database into the state a blind 045 leaves.

    Applies the chain through `049`, then rolls `050` back and NULLs the Group A
    rows — the condition an affected deployment is in, arrived at from the other
    direction. `NO FORCE` and the dropped NOT NULL are what make that possible
    at all, which is itself the point: nothing an application can do produces
    this state any more.
    """
    result = db.apply_chain()
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    conn = await asyncpg.connect(db.dsn)
    try:
        # Undo 050 so the column can hold NULL again.
        await conn.execute(
            (POSTGRES / "rollback" / "050_workspace_not_null_down.sql").read_text(
                encoding="utf-8"))
        # The ledger must forget 050 too, or re-applying it would be skipped —
        # which is the very mechanism that makes correcting 045 useless for an
        # existing database, reproduced here on purpose.
        await conn.execute("DELETE FROM schema_migrations WHERE name LIKE '050%'")

        tenant = uuid.uuid4()
        await conn.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                           tenant, f"upgrade-{tenant.hex[:8]}")
        first = await conn.fetchval("SELECT provision_tenant_substrate($1)", tenant)
        org = await conn.fetchval(
            "SELECT id FROM organisations WHERE tenant_id = $1", tenant)
        extra = []
        for i in range(1, workspaces_per_tenant):
            ws = uuid.uuid4()
            await conn.execute(
                "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
                "                        is_default, status) "
                "VALUES ($1,$2,$3,$4,$5,false,'active')",
                ws, tenant, org, f"Extra {i}", f"upgrade-extra-{i}")
            extra.append(ws)

        # Content that a blind 045 would have left unattributed. Written with
        # the window open, because 049's policies refuse a NULL workspace —
        # which is exactly why no application path can create this state now.
        await conn.execute("ALTER TABLE conversations NO FORCE ROW LEVEL SECURITY")
        await conn.execute("ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY")
        await conn.execute(
            "INSERT INTO conversations (tenant_id, workspace_id, session_id, title) "
            "VALUES ($1, NULL, 'legacy', 'a conversation from before the fix')", tenant)
        await conn.execute(
            "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
            "VALUES ($1, NULL, 'a task from before the fix', 'pending')", tenant)
        # And one row that was placed correctly, to prove the repair does not
        # move what it did not need to touch.
        await conn.execute(
            "INSERT INTO tasks (tenant_id, workspace_id, title, status) "
            "VALUES ($1, $2, 'already placed', 'pending')", tenant, first)
        await conn.execute("ALTER TABLE conversations FORCE ROW LEVEL SECURITY")
        await conn.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")
        return {"tenant": tenant, "first": first, "extra": extra}
    finally:
        await conn.close()


async def test_the_affected_state_is_what_we_think_it_is(disposable):
    """A control. If the scenario builder stopped producing NULL rows, every
    test below would pass by testing nothing."""
    await _become_affected(disposable)

    counts = await _null_counts(disposable.dsn)
    assert counts.get("conversations") == 1
    assert counts.get("tasks") == 1
    assert await _not_null_columns(disposable.dsn) == 0


async def test_050_repairs_an_affected_database(disposable):
    """The upgrade path, end to end.

    The tenant holds exactly one workspace, so every unattributed row has one
    place it could go and the repair is arithmetic.
    """
    world = await _become_affected(disposable)

    result = disposable.apply_file("050_workspace_not_null.sql")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    assert await _null_counts(disposable.dsn) == {}
    assert await _not_null_columns(disposable.dsn) == 17

    conn = await asyncpg.connect(disposable.dsn)
    try:
        rows = await conn.fetch(
            "SELECT title, workspace_id FROM tasks WHERE tenant_id = $1 ORDER BY title",
            world["tenant"])
        placed = {r["title"]: r["workspace_id"] for r in rows}
        assert placed["a task from before the fix"] == world["first"]
        # The row that already had a workspace was not moved.
        assert placed["already placed"] == world["first"]
        assert len(placed) == 2, "the repair added or removed a row"

        conversation = await conn.fetchval(
            "SELECT workspace_id FROM conversations WHERE tenant_id = $1", world["tenant"])
        assert conversation == world["first"]
    finally:
        await conn.close()


async def test_the_repair_reads_workspaces_rather_than_being_blind_to_it(disposable):
    """The specific defect, asserted at the level it occurred.

    `045` failed because it could WRITE the content tables and could not READ
    `workspaces`. Under the migrator this is the difference between a repair and
    a no-op that reports success, and nothing else in this file distinguishes
    them: a repair that silently placed nothing would leave the rows NULL and
    `SET NOT NULL` would fail — which is the assertion below.
    """
    await _become_affected(disposable)

    result = disposable.apply_file("050_workspace_not_null.sql")

    assert result.returncode == 0, (
        f"050 failed on an affected database. If the message names rows with no "
        f"workspace, the repair could not see `workspaces` and placed nothing — "
        f"the original defect, reproduced.\n{result.stdout}\n{result.stderr}")
    assert "repair: placed" in (result.stdout + result.stderr), (
        "050 reported no repaired rows on a database that had them, which means "
        "the backfill matched nothing rather than doing nothing")


async def test_ambiguous_ownership_is_refused_and_nothing_is_guessed(disposable):
    """Two workspaces, an unattributed row, and no answer the migration may give.

    This is where a repair is most tempting to get wrong: `is_default` is
    sitting right there, and using it would look reasonable and would silently
    file somebody's conversation in a workspace they did not choose.
    """
    world = await _become_affected(disposable, workspaces_per_tenant=2)

    result = disposable.apply_file("050_workspace_not_null.sql")

    assert result.returncode != 0, (
        "050 completed on a tenant holding two workspaces and an unattributed "
        "row. Something chose one — check the repair still has HAVING count(*) = 1")
    message = result.stdout + result.stderr
    assert "Refusing to make workspace_id NOT NULL" in message
    assert "conversations: 1 row(s)" in message

    # Nothing moved, nothing was deleted, and the constraint was not applied.
    counts = await _null_counts(disposable.dsn)
    assert counts.get("conversations") == 1
    assert counts.get("tasks") == 1
    assert await _not_null_columns(disposable.dsn) == 0

    conn = await asyncpg.connect(disposable.dsn)
    try:
        # The correctly-placed row is untouched: a refusal is not a rollback of
        # somebody else's data.
        placed = await conn.fetchval(
            "SELECT workspace_id FROM tasks WHERE tenant_id = $1 AND title = 'already placed'",
            world["tenant"])
        assert placed == world["first"]
    finally:
        await conn.close()


async def test_the_refusal_never_picks_the_default_workspace(disposable):
    """Named separately because "the default" is the plausible wrong answer.

    The tenant's first workspace IS its default. If the repair had reached for
    `is_default` instead of `HAVING count(*) = 1`, the previous test would still
    fail — but this one says why, and it is the sentence a future reader needs.
    """
    world = await _become_affected(disposable, workspaces_per_tenant=2)
    disposable.apply_file("050_workspace_not_null.sql")

    conn = await asyncpg.connect(disposable.dsn)
    try:
        assert await conn.fetchval(
            "SELECT is_default FROM workspaces WHERE id = $1", world["first"]), (
            "the scenario no longer has a default workspace, so this proves nothing")
        landed = await conn.fetchval(
            "SELECT workspace_id FROM conversations WHERE tenant_id = $1", world["tenant"])
        assert landed is None, (
            "the unattributed conversation was filed in the default workspace. "
            "A default is where NEW content goes when nobody chose; it is not "
            "evidence about where EXISTING content belonged.")
    finally:
        await conn.close()


async def test_both_paths_end_in_the_same_schema(disposable):
    """A fresh install and a repaired upgrade must be indistinguishable.

    Divergence here is the whole risk of a forward repair: two databases both
    reporting every migration applied, with different constraints on the same
    columns, and nothing to say which one a bug report came from.
    """
    fresh = Disposable(f"ira_fresh_{uuid.uuid4().hex[:8]}")
    await fresh.create()
    try:
        assert fresh.apply_chain().returncode == 0
        await _become_affected(disposable)
        assert disposable.apply_file("050_workspace_not_null.sql").returncode == 0

        async def shape(dsn: str):
            conn = await asyncpg.connect(dsn)
            try:
                return {
                    "not_null": await conn.fetchval(
                        "SELECT count(*) FROM pg_attribute WHERE attname='workspace_id'"
                        "   AND attnotnull AND attrelid = ANY($1::regclass[])",
                        list(TABLES)),
                    "policies": sorted(
                        (r["tablename"], r["policyname"]) for r in await conn.fetch(
                            "SELECT tablename, policyname FROM pg_policies"
                            " WHERE schemaname='public' AND tablename = ANY($1::text[])",
                            list(TABLES))),
                    "forced": await conn.fetchval(
                        "SELECT count(*) FROM pg_class WHERE relname = ANY($1::text[])"
                        "   AND relrowsecurity AND relforcerowsecurity",
                        list(TABLES) + ["workspaces"]),
                }
            finally:
                await conn.close()

        assert await shape(fresh.dsn) == await shape(disposable.dsn)
    finally:
        await fresh.drop()
