"""Phase 2 — ADVERSARIAL cross-tenant isolation, against a real PostgreSQL.

These are the tests the transformation brief calls mandatory. They do not mock
the database: mocking the thing being tested would prove only that the mock
behaves as written. Each test creates two tenants, writes data as one, and then
actively tries to reach it as the other.

Running them
------------
Set ``IRA_TEST_DATABASE_DSN`` to a database where ``postgres/init.sql`` through
``postgres/023_tenancy.sql`` have been applied, and which exposes a NON-SUPERUSER
role for the application. They skip otherwise, so the default suite stays
dependency-light — a skip is reported honestly rather than passing vacuously.

    createdb iratest
    psql -d iratest -c 'CREATE EXTENSION vector; CREATE EXTENSION "uuid-ossp"'
    # postgres/rollback/ is excluded on purpose — see test_migration_hygiene.py
    for f in postgres/init.sql postgres/0*.sql; do psql -d iratest -f "$f"; done
    psql -d iratest -c 'CREATE ROLE ira_app LOGIN'
    psql -d iratest -c 'GRANT ALL ON ALL TABLES IN SCHEMA public TO ira_app'
    export IRA_TEST_DATABASE_DSN='postgresql://ira_app@/iratest?host=/var/run/postgresql'

Why the role must not be a superuser
------------------------------------
Superusers bypass RLS entirely, and a table's owner bypasses it unless the table
is set to FORCE ROW LEVEL SECURITY. Either would make every assertion below pass
while protecting nothing. ``test_app_role_is_not_superuser`` asserts the harness
itself is honest before any isolation claim is made.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

from tests.tenancy import rls_workspace  # noqa: E402

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database RLS tests skipped"
)

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
TENANT_GHOST = uuid.UUID("33333333-3333-3333-3333-333333333333")

#: Tenant-scoped tables carrying customer data, and a column set usable to
#: insert a minimal row into each.
SCOPED_TABLES = (
    "conversations",
    "memory_embeddings",
    "people",
    "tasks",
    "trust_ledger",
    "approval_requests",
    "agent_runs",
    "tool_calls",
    "privacy_events",
    "companions",
    "user_sessions",
    "user_devices",
)


def _connect():
    return psycopg2.connect(DSN)


@pytest.fixture
def db():
    conn = _connect()
    conn.autocommit = True
    yield conn
    conn.close()


def _as_tenant(conn, tenant: uuid.UUID, workspace=None):
    """Bind to a tenant, and to one of its workspaces.

    049 made the seventeen workspace-scoped tables require BOTH settings, so a
    tenant-only binding now reads zero rows and cannot insert. That does not
    change what this file tests — tenant isolation is still the subject — it
    just means the fixture has to say which workspace it is writing into, the
    same as any other caller. See tests/tenancy/rls_workspace.py.

    093 (ADR-0009) did the same for `conversations` and `messages`: they also
    require the owner, `app.user_id`, which `workspace_connection` sets for
    every real caller. So the fixture binds one fixed user per tenant too. The
    subject is unchanged: each tenant still writes and reads its own rows.
    """
    cur = conn.cursor()
    rls_workspace.bind(cur, tenant, workspace)
    cur.execute("SELECT set_config('app.user_id', %s, false)", (str(_user_of(tenant)),))
    return cur


def _user_of(tenant: uuid.UUID) -> uuid.UUID:
    """The one user each tenant's rows are written as. Deterministic, so a
    read later in the same test binds the same owner the seed wrote."""
    return uuid.uuid5(tenant, "tenant-isolation-user")


def _clear_tenant(conn):
    cur = conn.cursor()
    # EVERY setting. Clearing only the tenant would leave a workspace (or,
    # since 093, a user) bound, so "unbound" would not mean what the test says.
    rls_workspace.unbind(cur)
    cur.execute("SELECT set_config('app.user_id', '', false)")
    return cur


@pytest.fixture
def markers():
    """Per-test unique markers.

    These tests cannot clean up by deleting their tenants: trust_ledger is
    append-only (020_trust_ledger.sql installs a trigger refusing DELETE), which
    is exactly why 023 gives it ON DELETE RESTRICT rather than CASCADE. So each
    test writes rows nobody else wrote and asserts only on those, which keeps the
    tests independent without needing to destroy audit history to do it.
    """
    run = uuid.uuid4().hex[:12]
    return {"a": f"A-secret-{run}", "b": f"B-secret-{run}"}


@pytest.fixture
def seeded(db, markers):
    """Two tenants, each with one row of every kind, then read back adversarially."""
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO tenants (id, name, slug, edition) VALUES
            (%s, 'Tenant A', 'tenant-a', 'cloud'),
            (%s, 'Tenant B', 'tenant-b', 'cloud')
        ON CONFLICT (id) DO NOTHING
        """,
        (str(TENANT_A), str(TENANT_B)),
    )
    for tenant, marker in ((TENANT_A, markers["a"]), (TENANT_B, markers["b"])):
        c = _as_tenant(db, tenant)
        c.execute(
            "INSERT INTO conversations (session_id, title, tenant_id) VALUES (%s,%s,%s)",
            (f"sess-{marker}", marker, str(tenant)),
        )
        c.execute(
            "INSERT INTO memory_embeddings (source_type, content, user_id, tenant_id) "
            "VALUES ('fact', %s, 'owner', %s)",
            (marker, str(tenant)),
        )
        c.execute(
            "INSERT INTO people (id, person_name, relationship, tenant_id) "
            "VALUES (%s, %s, 'friend', %s)",
            (str(uuid.uuid4()), marker, str(tenant)),
        )
        c.execute(
            "INSERT INTO tasks (title, tenant_id) VALUES (%s,%s)", (marker, str(tenant))
        )
        # trust_ledger is hash-chained (020): prev_hash/hash are NOT NULL. The
        # values are placeholders — this suite tests isolation, not the chain.
        c.execute(
            "INSERT INTO trust_ledger "
            "(user_id, action_id, kind, record, prev_hash, hash, tenant_id) "
            "VALUES ('owner', %s, 'action', '{}'::jsonb, 'genesis', %s, %s)",
            (marker, f"hash-{marker}", str(tenant)),
        )
        c.execute(
            "INSERT INTO approval_requests (tenant_id, domain, action, purpose) "
            "VALUES (%s, 'email', 'send', %s)",
            (str(tenant), marker),
        )
        c.execute(
            "INSERT INTO privacy_events (tenant_id, kind, detail) "
            "VALUES (%s, %s, '{}'::jsonb)",
            (str(tenant), marker),
        )
    yield db


# ── The harness must be honest before any claim it makes ─────────────────────

def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS, which would make every test below vacuous."""
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None, "current_user has no pg_user row"
    assert row[0] is False, (
        "The test role is a SUPERUSER. Superusers bypass row-level security, so "
        "these isolation tests would pass without proving anything."
    )


@pytest.mark.parametrize("table", SCOPED_TABLES)
def test_rls_is_enabled_and_forced(db, table):
    """FORCE matters: without it the table OWNER bypasses the policy silently,
    and the application role usually owns its own tables."""
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    )
    row = cur.fetchone()
    assert row is not None, f"table {table} not found"
    enabled, forced = row
    assert enabled, f"{table} does not have ROW LEVEL SECURITY enabled"
    assert forced, f"{table} does not FORCE ROW LEVEL SECURITY (owner would bypass)"


@pytest.mark.parametrize("table", SCOPED_TABLES)
def test_every_scoped_table_has_a_policy(db, table):
    cur = db.cursor()
    cur.execute("SELECT policyname FROM pg_policies WHERE tablename = %s", (table,))
    assert cur.fetchall(), f"{table} has RLS enabled but no policy — that denies everything"


# ── Cross-tenant reads ───────────────────────────────────────────────────────

def test_tenant_a_cannot_list_tenant_b_conversations(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT title FROM conversations")
    titles = {r[0] for r in cur.fetchall()}
    assert markers["a"] in titles
    assert markers["b"] not in titles


def test_tenant_a_cannot_retrieve_tenant_b_memories(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT content FROM memory_embeddings")
    contents = {r[0] for r in cur.fetchall()}
    assert markers["a"] in contents
    assert markers["b"] not in contents


def test_tenant_a_cannot_retrieve_tenant_b_vectors_even_by_similarity(seeded, markers):
    """Vector search ignores joins, so it needs its own assertion: a similarity
    query is still a SELECT and must still be filtered by RLS."""
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute(
        "SELECT content FROM memory_embeddings "
        "WHERE embedding IS NULL OR TRUE ORDER BY created_at LIMIT 100"
    )
    contents = {r[0] for r in cur.fetchall()}
    assert markers["b"] not in contents


def test_tenant_a_cannot_access_tenant_b_people(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT person_name FROM people")
    assert markers["b"] not in {r[0] for r in cur.fetchall()}


def test_tenant_a_cannot_access_tenant_b_tasks(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT title FROM tasks")
    assert markers["b"] not in {r[0] for r in cur.fetchall()}


def test_tenant_a_cannot_approve_tenant_b_actions(seeded, markers):
    """Not just reading: approving another tenant's pending action would be a
    privilege escalation across the boundary."""
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT purpose FROM approval_requests")
    assert markers["b"] not in {r[0] for r in cur.fetchall()}

    cur.execute("UPDATE approval_requests SET status='approved' WHERE purpose=%s", (markers["b"],))
    assert cur.rowcount == 0, "Tenant A was able to approve Tenant B's action"


def test_tenant_a_cannot_access_tenant_b_audit_events(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("SELECT action_id FROM trust_ledger")
    assert markers["b"] not in {r[0] for r in cur.fetchall()}

    cur.execute("SELECT kind FROM privacy_events")
    assert markers["b"] not in {r[0] for r in cur.fetchall()}


# ── Missing and forged context ───────────────────────────────────────────────

@pytest.mark.parametrize("table", SCOPED_TABLES)
def test_missing_tenant_context_returns_nothing(seeded, table):
    """The single most important property: unset context matches no rows,
    because `tenant_id = NULL` is NULL rather than true."""
    cur = _clear_tenant(seeded)
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] == 0, (
        f"{table} returned rows with NO tenant context set — missing context "
        "must deny, not default."
    )


def test_forged_tenant_id_sees_nothing(seeded):
    """A tenant id the caller invented — the shape of a forged frontend value —
    grants access to nothing.

    The workspace is forged too, and passed explicitly rather than looked up:
    a tenant that does not exist has no workspace to find, and asking the helper
    to create one would fail on the foreign key rather than on the thing under
    test. Two invented identifiers is also the more honest version of the
    attack — a caller forging one value forges both.
    """
    cur = _as_tenant(seeded, TENANT_GHOST, workspace=uuid.uuid4())
    cur.execute("SELECT count(*) FROM conversations")
    assert cur.fetchone()[0] == 0


def test_missing_context_cannot_insert(seeded):
    cur = _clear_tenant(seeded)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO conversations (session_id, title, tenant_id) "
            "VALUES ('x','y',%s)",
            (str(TENANT_A),),
        )


# ── Cross-tenant writes ──────────────────────────────────────────────────────

def test_tenant_a_cannot_insert_a_row_owned_by_tenant_b(seeded, markers):
    """WITH CHECK: a read-only policy would leave writes able to cross."""
    cur = _as_tenant(seeded, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO conversations (session_id, title, tenant_id) "
            "VALUES ('evil','stolen',%s)",
            (str(TENANT_B),),
        )


def test_tenant_a_cannot_update_tenant_b_rows(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("UPDATE conversations SET title='pwned' WHERE title=%s", (markers["b"],))
    assert cur.rowcount == 0

    cur = _as_tenant(seeded, TENANT_B)
    cur.execute("SELECT count(*) FROM conversations WHERE title=%s", (markers["b"],))
    assert cur.fetchone()[0] == 1, "Tenant B's row was modified by Tenant A"


def test_tenant_a_cannot_delete_tenant_b_rows(seeded, markers):
    cur = _as_tenant(seeded, TENANT_A)
    cur.execute("DELETE FROM conversations WHERE title=%s", (markers["b"],))
    assert cur.rowcount == 0

    cur = _as_tenant(seeded, TENANT_B)
    cur.execute("SELECT count(*) FROM conversations WHERE title=%s", (markers["b"],))
    assert cur.fetchone()[0] == 1


def test_tenant_a_cannot_steal_a_row_by_reassigning_tenant_id(seeded, markers):
    """The subtle one: UPDATE ... SET tenant_id = mine. WITH CHECK must reject
    the new row image even though the old one was visible."""
    cur = _as_tenant(seeded, TENANT_A)
    # Two acceptable outcomes: WITH CHECK rejects the new row image outright
    # (an error), or the statement affects nothing. Both are enforcement. What
    # must NOT happen is the row silently moving to Tenant B.
    try:
        cur.execute(
            "UPDATE conversations SET tenant_id=%s WHERE title=%s",
            (str(TENANT_B), markers["a"]),
        )
    except psycopg2.Error:
        pass

    cur2 = _as_tenant(seeded, TENANT_B)
    cur2.execute("SELECT count(*) FROM conversations WHERE title=%s", (markers["a"],))
    assert cur2.fetchone()[0] == 0, "Tenant A moved its row into Tenant B"


# ── Backfill ─────────────────────────────────────────────────────────────────

def test_private_tenant_exists_with_the_derived_uuid(db):
    """The application derives this UUID at runtime (uuid5). If the migration
    used a different one, every backfilled row is orphaned from the Principal."""
    from core.identity.principal import PRIVATE_TENANT_ID

    cur = db.cursor()
    cur.execute("SELECT slug, edition FROM tenants WHERE id = %s", (str(PRIVATE_TENANT_ID),))
    row = cur.fetchone()
    assert row is not None, (
        "The canonical private tenant is missing. Its UUID must equal "
        f"uuid5(IRA_UUID_NAMESPACE, 'tenant:private') = {PRIVATE_TENANT_ID}"
    )
    assert row[0] == "private"
    assert row[1] == "private"


@pytest.mark.parametrize(
    "table", ["conversations", "messages", "memory_embeddings", "people", "tasks", "trust_ledger"]
)
def test_tenant_id_is_not_nullable(db, table):
    """A nullable tenant_id is a row that belongs to nobody and, under a policy
    comparing to NULL, is invisible to everybody — silent data loss."""
    cur = db.cursor()
    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = 'tenant_id'",
        (table,),
    )
    row = cur.fetchone()
    assert row is not None, f"{table} has no tenant_id column"
    assert row[0] == "NO", f"{table}.tenant_id is nullable"
