"""Phase 3B Batch 5B — workspace columns and the constraints that scope them.

Batch 5B adds `workspace_id` to all 17 workspace-scoped tables, closes R-64 by
giving the seven unprotected tables a tenant boundary at last, and rescopes the
three global unique constraints that R-65 found.

**It does not enforce workspace isolation**, and several tests below exist
specifically to prove that it does not — because a reader of the schema could
easily conclude otherwise from the presence of the column, and acting on that
belief in Batch 5C would skip the step that makes it true.

Everything runs against a real PostgreSQL as a non-superuser, NOBYPASSRLS role.
A mock cannot refuse a row.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

# psycopg2 does not adapt `uuid.UUID` unless told to, and the fixtures below
# pass real UUID objects rather than strings so a malformed identifier fails
# here rather than reaching the database as text.
import psycopg2.extras  # noqa: E402

psycopg2.extras.register_uuid()

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — Batch 5B tests skipped",
                allow_module_level=True)

#: All 17. Named once so adding an eighteenth without a test is not possible.
GROUP_A = ("commitments", "companion_versions", "companions", "conversations",
           "files", "lifeos_events", "memory_embeddings", "messages", "people", "tasks")
GROUP_B = ("agents", "calendar_events", "decisions", "edges",
           "entities", "projects", "reminders")
ALL_17 = GROUP_A + GROUP_B

PRIVATE_TENANT = "e91aceeb-89fb-5ad2-b43c-4eee2bad079c"
UNIQUE_VIOLATION = "23505"
NOT_NULL_VIOLATION = "23502"
FK_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def admin_db():
    dsn = os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not dsn:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def bind(cur, tenant=None, workspace=None):
    # `set_config` takes text. The UUID adapter registered above would otherwise
    # send a uuid-typed parameter and PostgreSQL would report no such function.
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant) if tenant else "",))
    cur.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace) if workspace else "",))


@pytest.fixture
def two_worlds(admin_db):
    """Two tenants, each with two workspaces. Enough to ask every scoping
    question: same tenant different workspace, and different tenant entirely."""
    w = {}
    with admin_db.cursor() as cur:
        for key in ("a", "b"):
            t = uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)",
                        (t, f"b5b-{key}-{t.hex[:8]}"))
            cur.execute("SELECT provision_tenant_substrate(%s)", (t,))
            default_ws = cur.fetchone()[0]
            cur.execute("SELECT id FROM organisations WHERE tenant_id = %s", (t,))
            org = cur.fetchone()[0]
            second_ws = uuid.uuid4()
            cur.execute(
                "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
                " is_default, status) VALUES (%s,%s,%s,'Second','second',false,'active')",
                (second_ws, t, org))
            w[key] = {"tenant": t, "ws1": default_ws, "ws2": second_ws}
    admin_db.commit()
    yield w
    with admin_db.cursor() as cur:
        for key in ("a", "b"):
            t = w[key]["tenant"]
            for tbl in ("agents", "calendar_events", "projects", "entities",
                        "edges", "tasks", "reminders"):
                cur.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (t,))
            cur.execute("DELETE FROM workspaces WHERE tenant_id = %s", (t,))
            cur.execute("DELETE FROM organisations WHERE tenant_id = %s", (t,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (t,))
    admin_db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 0. The harness must be able to fail
# ─────────────────────────────────────────────────────────────────────────────


def test_the_role_cannot_bypass_rls(db):
    with db.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is_super, bypass = cur.fetchone()
    assert not is_super, "harness is a superuser; every isolation claim here is vacuous"
    assert not bypass


# ─────────────────────────────────────────────────────────────────────────────
# 1. The columns exist — and R-64 is closed
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ALL_17)
def test_every_workspace_scoped_table_has_both_columns(db, table):
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_attribute WHERE attrelid = %s::regclass"
            " AND attname IN ('tenant_id','workspace_id') AND NOT attisdropped", (table,))
        assert cur.fetchone()[0] == 2, f"{table} is missing tenant_id or workspace_id"


@pytest.mark.parametrize("table", GROUP_B)
def test_r64_is_closed(db, table):
    """The seven that had no tenant boundary at all now have one, forced."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity,"
            " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid),"
            " (SELECT attnotnull FROM pg_attribute WHERE attrelid = c.oid AND attname='tenant_id')"
            " FROM pg_class c WHERE c.oid = %s::regclass", (table,))
        enabled, forced, policies, tenant_not_null = cur.fetchone()
    assert enabled and forced, f"{table} does not have forced RLS"
    assert policies == 4, f"{table} has {policies} policies, expected four per-command"
    assert tenant_not_null, f"{table}.tenant_id is still nullable"


@pytest.mark.parametrize("table", GROUP_B)
def test_group_b_refuses_an_unscoped_write(db, table):
    """The point of R-64 being closed: no tenant bound, no row written."""
    with db.cursor() as cur:
        bind(cur, tenant=None)
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(f"INSERT INTO {table} DEFAULT VALUES")
        assert e.value.pgcode in (INSUFFICIENT_PRIVILEGE, NOT_NULL_VIOLATION)
    db.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Workspace isolation is NOT yet enforced — asserted on purpose
# ─────────────────────────────────────────────────────────────────────────────


def test_every_workspace_scoped_table_now_reads_the_workspace_guc(db):
    """R-43, inverted by 049 — which is what R-43 said would happen.

    Batch 5B carried the opposite assertion: NO policy may read
    `app.workspace_id`, because nothing called `workspace_connection()` yet and
    a workspace predicate would have returned zero rows for every query in the
    application. That was correct for 5B and is the thing 5C exists to change.

    It is inverted rather than deleted. A test asserting "the predicate is
    absent" would keep passing if 049 were reverted, and its name would read as
    an accepted state of affairs — the same trap
    `test_cross_workspace_read_is_not_refused_yet` was written to avoid.
    """
    with db.cursor() as cur:
        for table in ALL_17:
            cur.execute(
                "SELECT count(*) FROM pg_policy"
                " WHERE polrelid = %s::regclass"
                "   AND (pg_get_expr(polqual, polrelid) LIKE '%%app.workspace_id%%'"
                "     OR pg_get_expr(polwithcheck, polrelid) LIKE '%%app.workspace_id%%')",
                (table,))
            assert cur.fetchone()[0] == 4, (
                f"{table} does not have four policies reading app.workspace_id. "
                f"049 gives every workspace-scoped table one policy per command, "
                f"each requiring BOTH settings; a table with fewer has an "
                f"operation the workspace boundary does not cover.")


@pytest.mark.parametrize("table", ALL_17)
def test_workspace_id_is_not_null(db, table):
    """050 completed the sequence 045 started.

    The three stages had to land in this order and this test has moved with
    them: 045 added the column NULLABLE, because NOT NULL on a populated table
    with no writer supplying a value cannot apply; 049 made existing NULL rows
    invisible and new ones unwritable; 050 made them unrepresentable.

    Applying the constraint earlier would have failed on rows that still
    existed. Applying only the policy would have left the column able to hold a
    value nothing can read — legal in the schema, unreachable in practice, and
    exactly the kind of gap somebody later closes by relaxing the policy.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT attnotnull FROM pg_attribute"
            " WHERE attrelid = %s::regclass AND attname = 'workspace_id'", (table,))
        assert cur.fetchone()[0] is True, (
            f"{table}.workspace_id is nullable again — 050 has been reverted, and "
            f"046's composite foreign keys are vacuous for any row with a NULL")


# ─────────────────────────────────────────────────────────────────────────────
# 3. R-65 — the three rescoped constraints
# ─────────────────────────────────────────────────────────────────────────────


def test_two_tenants_can_hold_an_agent_with_the_same_name(db, two_worlds):
    """The headline. Before 046 the second tenant did not get an error — the
    write path upserts on `name`, so it OVERWROTE the first tenant's `code` and
    `docker_config`."""
    a, b = two_worlds["a"], two_worlds["b"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO agents (name, description, code, workspace_id)"
                    " VALUES ('research','A','print(1)',%s)", (a["ws1"],))
        bind(cur, tenant=b["tenant"], workspace=b["ws1"])
        cur.execute("INSERT INTO agents (name, description, code, workspace_id)"
                    " VALUES ('research','B','print(2)',%s)", (b["ws1"],))
        db.commit()

        # Read back inside the workspace each row was written to. Since 049 a
        # tenant-only binding reads nothing from these tables, which is the
        # boundary working rather than the row being missing — so the read has
        # to name a workspace, exactly as the write did.
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("SELECT code FROM agents WHERE name = 'research'")
        assert cur.fetchall() == [("print(1)",)], "tenant A's agent was overwritten"
        bind(cur, tenant=b["tenant"], workspace=b["ws1"])
        cur.execute("SELECT code FROM agents WHERE name = 'research'")
        assert cur.fetchall() == [("print(2)",)]


def test_two_workspaces_in_one_tenant_can_hold_the_same_project_slug(db, two_worlds):
    """`launch` in Engineering and `launch` in Marketing. Tenant-only scoping
    would make the first workspace take the name from the whole tenant — R-42
    one level down rather than fixed."""
    a = two_worlds["a"]
    with db.cursor() as cur:
        # One row per workspace, each written from that workspace. Since 049 a
        # connection bound to ws1 cannot insert a row carrying ws2 — WITH CHECK
        # refuses it — which is the same rule the application follows, so the
        # fixture follows it too rather than reaching around it.
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO projects (id, name, slug, workspace_id)"
                    " VALUES (%s,'Launch','launch',%s)", (uuid.uuid4(), a["ws1"]))
        bind(cur, tenant=a["tenant"], workspace=a["ws2"])
        cur.execute("INSERT INTO projects (id, name, slug, workspace_id)"
                    " VALUES (%s,'Launch','launch',%s)", (uuid.uuid4(), a["ws2"]))
        db.commit()
        # Both rows exist and neither displaced the other — which is now read
        # one workspace at a time, because that is what the boundary permits.
        for ws in (a["ws1"], a["ws2"]):
            bind(cur, tenant=a["tenant"], workspace=ws)
            cur.execute("SELECT count(*) FROM projects WHERE slug = 'launch'")
            assert cur.fetchone()[0] == 1, f"workspace {ws} lost its project"


def test_the_same_workspace_still_refuses_a_duplicate_slug(db, two_worlds):
    """Rescoping must not mean abandoning uniqueness."""
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO projects (id, name, slug, workspace_id)"
                    " VALUES (%s,'Launch','launch',%s)", (uuid.uuid4(), a["ws1"]))
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("INSERT INTO projects (id, name, slug, workspace_id)"
                        " VALUES (%s,'Launch again','launch',%s)", (uuid.uuid4(), a["ws1"]))
        assert e.value.pgcode == UNIQUE_VIOLATION
    db.rollback()


def test_two_workspaces_can_import_the_same_external_calendar_id(db, two_worlds):
    """`external_id` is issued by the customer's calendar provider and is unique
    only within that account. Two workspaces connecting two different Google
    accounts legitimately collide."""
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO calendar_events (external_id, title, start_at, end_at, workspace_id)"
                    " VALUES ('evt-1','Standup',NOW(),NOW(),%s)", (a["ws1"],))
        bind(cur, tenant=a["tenant"], workspace=a["ws2"])
        cur.execute("INSERT INTO calendar_events (external_id, title, start_at, end_at, workspace_id)"
                    " VALUES ('evt-1','Retro',NOW(),NOW(),%s)", (a["ws2"],))
        db.commit()
        for ws, title in ((a["ws1"], "Standup"), (a["ws2"], "Retro")):
            bind(cur, tenant=a["tenant"], workspace=ws)
            cur.execute("SELECT title FROM calendar_events WHERE external_id = 'evt-1'")
            assert cur.fetchall() == [(title,)], (
                "each workspace must keep its own event for the same provider id")


def test_a_workspace_still_cannot_hold_two_agents_with_one_name(db, two_worlds):
    """The scoped constraint constrains, now that every row has a workspace.

    This test used to prove something narrower and stranger: that NULLS NOT
    DISTINCT kept the constraint working while `workspace_id` was NULL, because
    NULL was what every row the application wrote actually had. 049 closed that
    case from the other end — a connection cannot write a NULL-workspace row at
    all any more, because WITH CHECK requires the bound workspace — so the
    transitional case is no longer reachable and the ordinary one is what is
    left to prove.

    NULLS NOT DISTINCT stays in the index until 050 makes the column NOT NULL.
    `test_the_index_still_treats_nulls_as_equal` below pins it there.
    """
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO agents (name, description, code, workspace_id)"
                    " VALUES ('dup','A','x',%s)", (a["ws1"],))
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("INSERT INTO agents (name, description, code, workspace_id)"
                        " VALUES ('dup','B','y',%s)", (a["ws1"],))
        assert e.value.pgcode == UNIQUE_VIOLATION
    db.rollback()


def test_the_index_still_treats_nulls_as_equal(db):
    """NULLS NOT DISTINCT is still on the scoped indexes, and must stay until
    050.

    No connection can write a NULL workspace any more, but rows written before
    049 could — and while any exist, dropping this property would let a tenant
    hold unlimited same-named agents among exactly the rows most likely to
    collide. It comes out when the column becomes NOT NULL and not before.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = 'public' AND indexname = 'agents_tenant_workspace_name_key'")
        row = cur.fetchone()
    assert row is not None, "the scoped agent-name index is gone"
    assert "NULLS NOT DISTINCT" in row[0].upper(), (
        "the scoped index no longer treats NULLs as equal. While any "
        "NULL-workspace row survives, that lets one tenant hold unlimited "
        "agents sharing a name.")


@pytest.mark.parametrize("old", ["agents_name_key", "calendar_events_external_id_key",
                                 "projects_slug_key"])
def test_the_global_constraints_are_gone(db, old):
    """Adding the scoped constraint alongside the global one changes nothing —
    the old one still refuses the second tenant. There is no additive fix."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conname = %s", (old,))
        assert cur.fetchone()[0] == 0, f"{old} still exists; the rescope is decorative"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Composite foreign keys — a relationship cannot span workspaces
# ─────────────────────────────────────────────────────────────────────────────


def test_an_edge_cannot_join_entities_in_two_workspaces(db, two_worlds):
    """Scoping the edge row is not enough: an edge whose endpoints live in
    different workspaces is a cross-workspace join expressed as data, and it
    would survive any policy written on the edge alone."""
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Ada',%s) RETURNING id", (a["ws1"],))
        here = cur.fetchone()[0]
        # Written from ws2, because since 049 a ws1 connection cannot create a
        # ws2 row. The endpoint still ends up where this test needs it; only the
        # route there has changed.
        bind(cur, tenant=a["tenant"], workspace=a["ws2"])
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Grace',%s) RETURNING id", (a["ws2"],))
        elsewhere = cur.fetchone()[0]

        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(
                "INSERT INTO edges (src_entity_id, dst_entity_id, relation, workspace_id)"
                " VALUES (%s,%s,'knows',%s)", (here, elsewhere, a["ws1"]))
        assert e.value.pgcode == FK_VIOLATION
    db.rollback()


def test_an_edge_within_one_workspace_is_permitted(db, two_worlds):
    """The other half — the constraint must not forbid the ordinary case."""
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Ada',%s) RETURNING id", (a["ws1"],))
        src = cur.fetchone()[0]
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Grace',%s) RETURNING id", (a["ws1"],))
        dst = cur.fetchone()[0]
        cur.execute("INSERT INTO edges (src_entity_id, dst_entity_id, relation, workspace_id)"
                    " VALUES (%s,%s,'knows',%s)", (src, dst, a["ws1"]))
        db.commit()


def test_a_reminder_cannot_reference_a_task_in_another_workspace(db, two_worlds):
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws2"])
        cur.execute("INSERT INTO tasks (title, tenant_id, workspace_id)"
                    " VALUES ('Ship it',%s,%s) RETURNING id", (a["tenant"], a["ws2"]))
        task_elsewhere = cur.fetchone()[0]

        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("INSERT INTO reminders (task_id, title, remind_at, workspace_id)"
                        " VALUES (%s,'Nudge',NOW(),%s)", (task_elsewhere, a["ws1"]))
        assert e.value.pgcode == FK_VIOLATION
    db.rollback()


def test_the_composite_fk_can_no_longer_be_dodged_with_a_null_workspace(db, two_worlds):
    """049 closed the MATCH SIMPLE escape hatch — from the other end.

    This test used to assert the opposite, and was right to. The composite FKs
    are MATCH SIMPLE, so a NULL in any column skips the check entirely: an edge
    written with `workspace_id = NULL` could join entities in two different
    workspaces and no constraint would object. That was the transitional limit
    of Batch 5B, recorded here so nobody read the constraint list and concluded
    the gap was already shut.

    It is shut now, and not by the FK. 049's WITH CHECK requires the row's
    workspace to equal the bound one, so `workspace_id = NULL` is refused before
    the FK is ever consulted. The vacuity still exists in the constraint and is
    simply unreachable: no connection can produce the row that would exercise it.

    050 removes the vacuity itself by making the column NOT NULL. Until then
    this is the honest statement — the hole is covered, not filled.
    """
    a = two_worlds["a"]
    with db.cursor() as cur:
        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Ada',%s) RETURNING id", (a["ws1"],))
        src = cur.fetchone()[0]
        bind(cur, tenant=a["tenant"], workspace=a["ws2"])
        cur.execute("INSERT INTO entities (type, name, workspace_id) VALUES"
                    " ('person','Grace',%s) RETURNING id", (a["ws2"],))
        dst = cur.fetchone()[0]

        bind(cur, tenant=a["tenant"], workspace=a["ws1"])
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("INSERT INTO edges (src_entity_id, dst_entity_id, relation, workspace_id)"
                        " VALUES (%s,%s,'knows',NULL)", (src, dst))
        assert e.value.pgcode == INSUFFICIENT_PRIVILEGE, (
            "a NULL-workspace edge was accepted. That row would be invisible to "
            "every reader AND would dodge the composite FK, which is how a "
            "cross-workspace join survives as data.")
    db.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# 5. The verification view
# ─────────────────────────────────────────────────────────────────────────────


def test_the_status_view_reports_vacuity_separately(db):
    """`contract_ready` on an empty table reads true and says nothing. The view
    must publish emptiness as its own column so a reader cannot mistake
    "nothing to do" for "done"."""
    with db.cursor() as cur:
        cur.execute("SELECT table_name, tenant_ready, workspace_backfilled, vacuous"
                    " FROM batch5b_workspace_scope_status ORDER BY table_name")
        rows = cur.fetchall()
    assert len(rows) == 17
    assert {r[0] for r in rows} == set(ALL_17)


def test_the_status_view_is_security_invoker(db):
    """A view runs as its OWNER by default, and the owner is subject to FORCE —
    so a default view reports what the schema owner can see, which is nothing,
    to every caller including a superuser. `036` had to learn this."""
    with db.cursor() as cur:
        cur.execute("SELECT reloptions FROM pg_class WHERE relname = 'batch5b_workspace_scope_status'")
        opts = cur.fetchone()[0] or []
    assert any("security_invoker=true" in o for o in opts), (
        "the status view is not security_invoker; it will report the schema "
        "owner's visibility rather than the caller's")
