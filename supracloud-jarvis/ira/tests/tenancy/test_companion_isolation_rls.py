"""Phase 3 — ADVERSARIAL cross-tenant isolation for the companion tables.

Same posture as Phase 2's suite and for the same reason: nothing is mocked, two
tenants are created, one writes, and the other actively tries to reach it.

There is a second question here that Phase 2 did not have to answer. Phase 3
adds two KINDS of table:

  * `companion_preferences` and `companion_versions` are CUSTOMER DATA and must
    be isolated exactly like everything in Phase 2.
  * `avatar_catalogue`, `voice_catalogue` and `personality_presets` are GLOBAL
    REFERENCE DATA — which avatars this deployment offers, under what licence.
    They contain nobody's data and are deliberately NOT tenant-scoped.

"Deliberately not scoped" is indistinguishable from "somebody forgot" unless it
is asserted, so both halves are tested: the customer tables deny without a
tenant context, and the catalogue tables are readable without one BECAUSE they
hold no customer data — while still being unwritable by a tenant.

Running them: see the header of test_tenant_isolation_rls.py. The migration
chain must include 024_companion_catalogue.sql.
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

TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

#: New in Phase 3, and carrying customer data.
COMPANION_SCOPED = ("companion_preferences", "companion_versions")

#: New in Phase 3, and deliberately global.
CATALOGUE_TABLES = ("avatar_catalogue", "voice_catalogue", "personality_presets")


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


#: The member `seeded` created for each tenant. `060` made
#: `companion_preferences` user-scoped, so a binding without a user reads zero
#: rows and cannot insert — and every test in this file writes as somebody.
#: Defaulting here rather than threading a user through each call site keeps the
#: subject of the file (tenant isolation) unchanged.
_SEEDED_MEMBER: dict[uuid.UUID, uuid.UUID] = {}


def _as_tenant(conn, tenant: uuid.UUID, workspace=None, user=None):
    """Bind to a tenant, one of its workspaces, and the member acting.

    049 made the seventeen workspace-scoped tables require BOTH settings, so a
    tenant-only binding now reads zero rows and cannot insert. That does not
    change what this file tests — tenant isolation is still the subject — it
    just means the fixture has to say which workspace it is writing into, the
    same as any other caller. See tests/tenancy/rls_workspace.py.

    `060` did the same thing again for `companion_preferences`, one level down:
    it requires `app.user_id` as well, because one member's settings are not
    their colleagues' to read. The member defaults to the one `seeded` made for
    this tenant.
    """
    cur = conn.cursor()
    rls_workspace.bind(cur, tenant, workspace)
    bound = user if user is not None else _SEEDED_MEMBER.get(tenant)
    cur.execute("SELECT set_config('app.user_id', %s, false)",
                (str(bound) if bound else "",))
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    # EVERY setting. Clearing only the tenant would leave a workspace or a user
    # bound, so "unbound" would not mean what the test says it means.
    rls_workspace.unbind(cur)
    cur.execute("SELECT set_config('app.user_id', '', false)")
    return cur


@pytest.fixture
def seeded(db):
    """Two tenants, each with a user, a companion, preferences and a version."""
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO tenants (id, name, slug, edition) VALUES
            (%s, 'Companion A', %s, 'cloud'),
            (%s, 'Companion B', %s, 'cloud')
        ON CONFLICT (id) DO NOTHING
        """,
        (str(TENANT_A), f"companion-a-{run}", str(TENANT_B), f"companion-b-{run}"),
    )

    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        marker = f"{key.upper()}-companion-{run}"
        user_id = uuid.uuid4()
        _SEEDED_MEMBER[tenant] = user_id
        # `users` has no tenant_id and no RLS — it is the identity record, and
        # membership is what binds a user to a tenant (see 023).
        cur.execute(
            "INSERT INTO users (id, external_subject) VALUES (%s, %s)",
            (str(user_id), f"{key}-{run}"),
        )
        c = _as_tenant(db, tenant)
        c.execute(
            "INSERT INTO companions (tenant_id, user_id, display_name, status) "
            "VALUES (%s, %s, %s, 'active') RETURNING id",
            (str(tenant), str(user_id), marker),
        )
        companion_id = c.fetchone()[0]
        c.execute(
            "INSERT INTO companion_preferences (tenant_id, user_id, companion_id, greeting) "
            "VALUES (%s, %s, %s, %s)",
            (str(tenant), str(user_id), str(companion_id), marker),
        )
        c.execute(
            "INSERT INTO companion_versions "
            "(tenant_id, user_id, companion_id, version, snapshot, reason) "
            "VALUES (%s, %s, %s, 1, %s::jsonb, %s)",
            (str(tenant), str(user_id), str(companion_id),
             '{"display_name": "' + marker + '"}', marker),
        )
        ids[key] = {
            "tenant": tenant,
            "user_id": user_id,
            "companion_id": companion_id,
            "marker": marker,
        }

    _clear_tenant(db)
    yield ids


# ── The harness must be honest before any claim it makes ─────────────────────


def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS, which would make everything below vacuous."""
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False, (
        "The test role is a SUPERUSER — these isolation tests would pass "
        "without proving anything."
    )


@pytest.mark.parametrize("table", COMPANION_SCOPED)
def test_rls_is_enabled_and_forced(db, table):
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    )
    row = cur.fetchone()
    assert row is not None, f"{table} not found — is 024 applied?"
    enabled, forced = row
    assert enabled, f"{table} does not have ROW LEVEL SECURITY enabled"
    assert forced, f"{table} does not FORCE ROW LEVEL SECURITY (owner would bypass)"


# ── Cross-tenant reads ───────────────────────────────────────────────────────


@pytest.mark.parametrize("table", COMPANION_SCOPED)
def test_tenant_a_cannot_read_tenant_b_rows(db, seeded, table):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (str(TENANT_B),))
    assert cur.fetchone()[0] == 0, f"tenant A read tenant B's {table}"


def test_tenant_a_cannot_read_tenant_b_greeting(db, seeded):
    """The greeting is free text a user wrote. It is exactly the kind of thing
    a cross-tenant read would expose."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT greeting FROM companion_preferences")
    greetings = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in greetings


def test_tenant_a_cannot_read_tenant_b_companion_history(db, seeded):
    """companion_versions is the record of what somebody's assistant has been
    configured to be over time — a behavioural profile, not just settings."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT reason FROM companion_versions")
    reasons = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in reasons
    assert seeded["a"]["marker"] in reasons, "tenant A cannot see its OWN row"


@pytest.mark.parametrize("table", COMPANION_SCOPED)
def test_a_missing_tenant_context_returns_nothing(db, seeded, table):
    """The single most important property: unset context denies by
    construction, not by a check somebody has to remember to write."""
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] == 0, f"{table} returned rows with no tenant context"


@pytest.mark.parametrize("table", COMPANION_SCOPED)
def test_a_cleared_tenant_context_denies_cleanly_rather_than_raising(db, seeded, table):
    """NULLIF(..., '') is load-bearing: resetting a GUC leaves it as the EMPTY
    STRING, and ''::uuid raises rather than returning nothing. Still fail-closed,
    but an error application code might catch and treat as a transient fault."""
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")  # must not raise
    assert cur.fetchone()[0] == 0


# ── Cross-tenant writes ──────────────────────────────────────────────────────


def test_tenant_a_cannot_insert_a_row_into_tenant_b(db, seeded):
    """WITH CHECK. A read-only policy would leave writes able to cross."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO companion_preferences (tenant_id, user_id, companion_id, greeting) "
            "VALUES (%s, %s, %s, 'planted')",
            (str(TENANT_B), str(seeded["b"]["user_id"]), str(seeded["b"]["companion_id"])),
        )


def test_tenant_a_cannot_update_tenant_b_preferences(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE companion_preferences SET greeting = 'hijacked' WHERE tenant_id = %s",
        (str(TENANT_B),),
    )
    assert cur.rowcount == 0, "tenant A updated tenant B's rows"

    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT greeting FROM companion_preferences")
    assert seeded["b"]["marker"] in {r[0] for r in cur.fetchall()}


def test_tenant_a_cannot_delete_tenant_b_companion_history(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute("DELETE FROM companion_versions WHERE tenant_id = %s", (str(TENANT_B),))
    assert cur.rowcount == 0

    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM companion_versions")
    assert cur.fetchone()[0] >= 1, "tenant B's history was destroyed"


def test_a_write_with_no_tenant_context_is_refused(db, seeded):
    cur = _clear_tenant(db)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO companion_preferences (tenant_id, user_id, companion_id) "
            "VALUES (%s, %s, %s)",
            (str(TENANT_A), str(seeded["a"]["user_id"]), str(seeded["a"]["companion_id"])),
        )


# ── The catalogue tables are global ON PURPOSE ───────────────────────────────


@pytest.mark.parametrize("table", CATALOGUE_TABLES)
def test_catalogue_tables_are_readable_without_a_tenant_context(db, table):
    """The other half of the decision. These hold no customer data — only which
    avatars and voices this deployment offers — so scoping them would mean
    copying the catalogue per tenant, which is how a licence revocation ends up
    applied to some tenants and not others.

    If somebody later adds customer data to one of these tables, this test
    passing is the signal that it is being served to everyone.
    """
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] > 0, f"{table} is empty — was 024's seed applied?"


@pytest.mark.parametrize("table", CATALOGUE_TABLES)
def test_catalogue_tables_carry_no_tenant_column(db, table):
    """Asserts the design rather than the current data: a `tenant_id` appearing
    here would mean somebody started scoping these tables without also adding a
    policy — the worst of both, since it would look scoped and enforce nothing."""
    cur = db.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = 'tenant_id'",
        (table,),
    )
    assert cur.fetchone() is None, (
        f"{table} has a tenant_id column but no RLS policy. Either scope it "
        f"properly (add it to 024's policy loop) or remove the column."
    )


def test_the_owner_portrait_is_seeded_private_only(db):
    """The database's own copy of the decision the licence gate enforces in
    code. Both must agree, or which one is true depends on the code path."""
    cur = db.cursor()
    cur.execute(
        "SELECT edition_availability, licence_verified FROM avatar_catalogue "
        "WHERE slug = 'ira-portrait'"
    )
    row = cur.fetchone()
    assert row is not None, "ira-portrait is not seeded"
    availability, verified = row
    assert availability == "private", (
        "the owner's personal likeness is seeded as available beyond the "
        "private edition"
    )
    assert verified is False


def test_exactly_the_licence_free_avatar_is_cloud_available(db):
    cur = db.cursor()
    cur.execute(
        "SELECT slug, renderer FROM avatar_catalogue "
        "WHERE active AND edition_availability IN ('cloud', 'both')"
    )
    rows = cur.fetchall()
    assert rows, "no avatar is offerable to cloud users at all"
    for slug, renderer in rows:
        assert renderer == "procedural", (
            f"{slug} is offered to cloud users but is not procedural — it must "
            f"carry a verified redistribution licence to be here"
        )
