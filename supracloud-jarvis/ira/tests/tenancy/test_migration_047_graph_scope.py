"""R-68 — entity and edge identity is unique WITHIN a workspace, not globally.

`046` rescoped `agents.name`, `calendar_events.external_id` and
`projects.slug`. It did not reach `entities (type, name)` or
`edges (src, dst, relation)`, so until `047` the Life Graph was the last place
where one tenant's choice of a name constrained another's.

**Why RLS cannot fix this and the constraint has to.** A unique index is
checked against the physical heap, not against the rows a policy makes visible.
Under the old global key, tenant B's

    INSERT … ON CONFLICT (type, name) DO UPDATE

conflicted with tenant A's row — a row B cannot see — and then UPDATED it. The
policy is never consulted for conflict detection, so no arrangement of policies
closes it. That is the test at the centre of this file.

Runs against a real PostgreSQL: an index's behaviour under `ON CONFLICT` is not
something a mock can demonstrate.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

MIGRATIONS = __import__("pathlib").Path(__file__).resolve().parents[3] / "postgres"


class World:
    pass


@pytest.fixture
async def world():
    """Tenant A with workspaces A and B; tenant B with workspace C.

    The minimum shape that can distinguish "scoped by tenant" from "scoped by
    workspace" — a two-tenant fixture alone would pass with a tenant-only key.
    """
    w = World()
    w.tenant_a, w.tenant_b = uuid.uuid4(), uuid.uuid4()
    w.ws_a, w.ws_b, w.ws_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for t, n in ((w.tenant_a, "047a"), (w.tenant_b, "047b")):
            await conn.execute(
                "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$3)",
                t, f"{n}-{t.hex[:8]}", f"{n}-{t.hex[:8]}")
        org_a = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name) VALUES ($1,'A') RETURNING id",
            w.tenant_a)
        org_b = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name) VALUES ($1,'B') RETURNING id",
            w.tenant_b)
        await conn.execute(
            """INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default,status)
               VALUES ($1,$2,$3,'A','a-047',TRUE,'active'),
                      ($4,$2,$3,'B','b-047',FALSE,'active'),
                      ($5,$6,$7,'C','c-047',TRUE,'active')""",
            w.ws_a, w.tenant_a, org_a, w.ws_b, w.ws_c, w.tenant_b, org_b)
        yield w
    finally:
        for t in (w.tenant_a, w.tenant_b):
            await conn.execute("DELETE FROM edges WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM entities WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
            await conn.execute("DELETE FROM tenants WHERE id=$1", t)
        await conn.close()


async def _entity(conn, tenant, workspace, name, description):
    return await conn.fetchval(
        """INSERT INTO entities (tenant_id,workspace_id,type,name,description)
           VALUES ($1,$2,'person',$3,$4) RETURNING id""",
        tenant, workspace, name, description)


# ─────────────────────────────────────────────────────────────────────────────
# The identity is scoped
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_same_entity_can_exist_in_two_workspaces_of_one_tenant(world):
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "in-A")
        await _entity(conn, world.tenant_a, world.ws_b, "Alice", "in-B")
        rows = await conn.fetch(
            "SELECT description FROM entities WHERE tenant_id=$1 AND name='Alice'"
            " ORDER BY description", world.tenant_a)
        assert [r["description"] for r in rows] == ["in-A", "in-B"]
    finally:
        await conn.close()


async def test_the_same_entity_can_exist_in_two_tenants(world):
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "tenant-A")
        await _entity(conn, world.tenant_b, world.ws_c, "Alice", "tenant-B")
        assert await conn.fetchval(
            "SELECT count(*) FROM entities WHERE name='Alice' AND tenant_id = ANY($1)",
            [world.tenant_a, world.tenant_b]) == 2
    finally:
        await conn.close()


async def test_the_same_edge_can_exist_in_two_workspaces(world):
    """Edges are keyed on entity ids, which differ per workspace — so this is
    really asserting that the SAME relation between the SAME logical pair is
    allowed twice, once per workspace."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for ws in (world.ws_a, world.ws_b):
            src = await _entity(conn, world.tenant_a, ws, "Alice", str(ws))
            dst = await _entity(conn, world.tenant_a, ws, "Bob", str(ws))
            await conn.execute(
                """INSERT INTO edges (tenant_id,workspace_id,src_entity_id,
                                      dst_entity_id,relation)
                   VALUES ($1,$2,$3,$4,'knows')""",
                world.tenant_a, ws, src, dst)
        assert await conn.fetchval(
            "SELECT count(*) FROM edges WHERE tenant_id=$1 AND relation='knows'",
            world.tenant_a) == 2
    finally:
        await conn.close()


async def test_an_edge_cannot_reference_an_entity_in_another_workspace(world):
    """046's composite FK, still enforcing after 047 replaced the indexes.

    `edges_src_within_workspace` is `MATCH SIMPLE` on
    `(tenant_id, workspace_id, src_entity_id)`, so an edge cannot point at an
    entity that lives somewhere else. Asserted here because 047 drops and
    recreates indexes on both tables, which is exactly when a constraint gets
    lost by accident.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        src = await _entity(conn, world.tenant_a, world.ws_a, "Alice", "in-A")
        dst = await _entity(conn, world.tenant_a, world.ws_a, "Bob", "in-A")
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await conn.execute(
                """INSERT INTO edges (tenant_id,workspace_id,src_entity_id,
                                      dst_entity_id,relation)
                   VALUES ($1,$2,$3,$4,'knows')""",
                world.tenant_a, world.ws_b, src, dst)
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# The R-68 defect itself
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_upsert_cannot_overwrite_another_workspaces_hidden_row(world):
    """The whole reason 047 exists.

    Under `UNIQUE (type, name)`, workspace B's upsert conflicted with workspace
    A's row and `DO UPDATE` modified it — a cross-tenant/cross-workspace WRITE
    that RLS does not prevent, because conflict detection reads the heap rather
    than the policy-visible rows.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "ORIGINAL-A")
        await conn.execute(
            """INSERT INTO entities (tenant_id,workspace_id,type,name,description)
               VALUES ($1,$2,'person','Alice','WRITTEN-BY-B')
               ON CONFLICT (tenant_id, workspace_id, type, name) DO UPDATE
                 SET description = EXCLUDED.description""",
            world.tenant_a, world.ws_b)

        assert await conn.fetchval(
            "SELECT description FROM entities WHERE workspace_id=$1 AND name='Alice'",
            world.ws_a) == "ORIGINAL-A", (
            "workspace B's upsert modified workspace A's row — R-68 has "
            "regressed, and no policy can catch this"
        )
        assert await conn.fetchval(
            "SELECT description FROM entities WHERE workspace_id=$1 AND name='Alice'",
            world.ws_b) == "WRITTEN-BY-B"
    finally:
        await conn.close()


async def test_a_second_upsert_in_the_same_workspace_still_updates(world):
    """The other half: scoping the key must not turn an upsert into an insert.
    Within ONE workspace, `DO UPDATE` still does what it says."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "first")
        await conn.execute(
            """INSERT INTO entities (tenant_id,workspace_id,type,name,description)
               VALUES ($1,$2,'person','Alice','second')
               ON CONFLICT (tenant_id, workspace_id, type, name) DO UPDATE
                 SET description = EXCLUDED.description""",
            world.tenant_a, world.ws_a)
        rows = await conn.fetch(
            "SELECT description FROM entities WHERE workspace_id=$1 AND name='Alice'",
            world.ws_a)
        assert [r["description"] for r in rows] == ["second"]
    finally:
        await conn.close()


async def test_a_duplicate_within_one_workspace_is_still_refused(world):
    """Scoping is not loosening. Two rows with the same identity in the SAME
    workspace remain impossible."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "one")
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await _entity(conn, world.tenant_a, world.ws_a, "Alice", "two")
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# The migration and its rollback
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_scoped_indexes_exist_and_the_global_ones_do_not():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        names = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('entities','edges')")}
        assert "idx_entities_scope_type_name" in names
        assert "idx_edges_scope_src_dst_relation" in names
        assert "idx_entities_type_name" not in names, (
            "the global entity key is back — one tenant can overwrite another's row"
        )
        assert "idx_edges_src_dst_relation" not in names
    finally:
        await conn.close()


async def test_the_scoped_index_uses_nulls_not_distinct():
    """Load-bearing while `workspace_id` is nullable.

    Under the default NULLS DISTINCT, every un-backfilled row would be unique
    regardless of type and name — the constraint would stop constraining
    exactly the population most likely to collide.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for index in ("idx_entities_scope_type_name", "idx_edges_scope_src_dst_relation"):
            definition = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = $1", index)
            assert "NULLS NOT DISTINCT" in definition, f"{index}: {definition}"
    finally:
        await conn.close()


async def test_forced_rls_survived_the_index_swap():
    """Dropping and recreating indexes is exactly the kind of operation that
    gets done with the protections off."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for table in ("entities", "edges"):
            row = await conn.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                " WHERE oid = $1::regclass", table)
            assert row["relrowsecurity"] and row["relforcerowsecurity"], table
    finally:
        await conn.close()


async def test_the_rollback_refuses_when_restoring_would_collide(world):
    """The rollback must not resolve a collision by deleting somebody's row.

    Two workspaces holding ("person", "Alice") is the state 047 exists to
    permit, so restoring a global key would require choosing a survivor. The
    script reports and stops.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "in-A")
        await _entity(conn, world.tenant_a, world.ws_b, "Alice", "in-B")

        # The script runs on its OWN connection: a RAISE inside a
        # multi-statement script aborts that connection's implicit transaction,
        # and every later assertion here would fail with
        # InFailedSQLTransactionError rather than on its own merits.
        sql = (MIGRATIONS / "rollback" / "047_graph_scope_constraints_down.sql").read_text()
        runner = await asyncpg.connect(ADMIN_DSN)
        try:
            with pytest.raises(asyncpg.exceptions.RaiseError) as e:
                await runner.execute(sql)
        finally:
            await runner.close()
        message = str(e.value)
        assert "Refusing to restore the global UNIQUE" in message
        assert "held by more than one workspace" in message

        # And nothing was changed by the attempt.
        names = {r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename='entities'")}
        assert "idx_entities_scope_type_name" in names
        assert "idx_entities_type_name" not in names
        assert await conn.fetchval(
            "SELECT count(*) FROM entities WHERE tenant_id=$1 AND name='Alice'",
            world.tenant_a) == 2, "the rollback deleted rows it was refusing over"
    finally:
        await conn.close()


async def test_the_forward_migration_refuses_a_duplicate_within_one_workspace(world):
    """Self-test of the preflight, by planting the defect it exists to catch.

    The migration is re-run against a database that already has the scoped
    index plus a duplicate that could only exist if the index were absent — so
    the index is dropped for the duration, exactly reproducing the state a
    real upgrade would meet.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("DROP INDEX IF EXISTS idx_entities_scope_type_name")
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "one")
        await _entity(conn, world.tenant_a, world.ws_a, "Alice", "two")

        sql = (MIGRATIONS / "047_graph_scope_constraints.sql").read_text()
        runner = await asyncpg.connect(ADMIN_DSN)
        try:
            with pytest.raises(asyncpg.exceptions.RaiseError) as e:
                await runner.execute(sql)
        finally:
            await runner.close()
        message = str(e.value)
        assert "Refusing to create the scoped UNIQUE on entities" in message
        # The diagnostic must name where the problem is, not just that there is
        # one — an operator has to find these rows.
        assert str(world.tenant_a) in message
        assert str(world.ws_a) in message
        assert "Alice" in message

        assert await conn.fetchval(
            "SELECT count(*) FROM entities WHERE workspace_id=$1 AND name='Alice'",
            world.ws_a) == 2, "the migration deleted or merged rows it was refusing over"
    finally:
        # Restore the index the fixture teardown and every other test rely on.
        await conn.execute("DELETE FROM entities WHERE tenant_id=$1", world.tenant_a)
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_scope_type_name "
            "ON entities (tenant_id, workspace_id, type, name) NULLS NOT DISTINCT")
        await conn.close()
