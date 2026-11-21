"""Phase 3B-3.5 — isolation holds for every role that can reach the database.

Isolation has always been tested as `ira_app`. That was one role out of several,
and the interesting ones are the others: a background worker with no request
context, and a migrator that holds ownership through `SET ROLE`.

The rule this suite exists to enforce is that **no role except the schema owner,
acting deliberately, escapes row-level security** — and that the schema owner's
escape is bounded to the moment a migration needs it.

Superusers are deliberately absent as proof of anything. A superuser bypasses
RLS entirely, so a passing test under one proves only that the SQL parsed. Where
a privileged connection is needed to arrange a fixture it is used for that and
never for an assertion.
"""
from __future__ import annotations

import os
import uuid

import pytest

from _dsn import as_role

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

APP_DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not APP_DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)


def _dsn_as(role: str) -> str:
    """The app DSN with the user swapped. Keeps host/port/database identical, so
    a difference in behaviour is attributable to the role and nothing else.

    The password is supplied, and that is not cosmetic. This used to emit
    `postgresql://ira_app@host/db` with no credential, which works against a
    `trust` pg_hba (a local socket, a developer machine) and fails against the
    password auth CI uses — so `_connect_as` skipped, and all seventeen
    role-boundary assertions in this file had never once run in CI. The roles
    are provisioned with their own name as the password, here and in the
    workflow.
    """
    if "://" not in APP_DSN:
        pytest.skip("cannot derive a per-role DSN from this DSN form")
    return as_role(APP_DSN, role, role)


def _connect_as(role: str):
    try:
        return psycopg2.connect(_dsn_as(role))
    except psycopg2.OperationalError as exc:
        pytest.skip(f"cannot connect as {role}: {type(exc).__name__}")


@pytest.fixture
def admin_db():
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def two_tenants(admin_db):
    made = {}
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            tid, uid = uuid.uuid4(), uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name, edition, status) "
                        "VALUES (%s, %s, 'cloud', 'active')", (str(tid), f"role-{label}"))
            cur.execute("INSERT INTO users (id, email) VALUES (%s, %s)",
                        (str(uid), f"role-{label}-{tid.hex[:8]}@example.com"))
            cur.execute("INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at) "
                        "VALUES (%s, %s, 'primary_owner', 'active', NOW())", (str(tid), str(uid)))
            cur.execute("SELECT provision_tenant_substrate(%s)", (str(tid),))
            wid = cur.fetchone()[0]
            cur.execute("INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) "
                        "VALUES (%s, %s, %s, 'workspace_admin', 'active')",
                        (str(tid), str(wid), str(uid)))
            made[label] = {"tenant": tid, "user": uid, "workspace": wid}
    admin_db.commit()
    yield made
    admin_db.rollback()
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            cur.execute("DELETE FROM tenants WHERE id = %s", (str(made[label]["tenant"]),))
            cur.execute("DELETE FROM users   WHERE id = %s", (str(made[label]["user"]),))
    admin_db.commit()


def _bind(cur, *, tenant=None, user=None, workspace=None):
    cur.execute("SELECT set_config('app.tenant_id',    %s, true)", (str(tenant) if tenant else "",))
    cur.execute("SELECT set_config('app.user_id',      %s, true)", (str(user) if user else "",))
    cur.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace) if workspace else "",))


ISOLATED_ROLES = ["ira_app", "ira_worker"]


# ── Every runtime role is subject to RLS ─────────────────────────────────────

@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_the_role_is_subject_to_row_level_security(role):
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            is_super, bypass = cur.fetchone()
        assert not is_super and not bypass, (
            f"{role} bypasses row-level security, so every assertion below would "
            f"pass while protecting nothing")
    finally:
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_tenant_a_cannot_read_tenant_b(role, two_tenants):
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
            cur.execute("SELECT count(*) FROM organisations WHERE tenant_id = %s",
                        (str(two_tenants["b"]["tenant"]),))
            assert cur.fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_workspace_a_cannot_read_workspace_b(role, two_tenants):
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
            cur.execute("SELECT count(*) FROM workspace_memberships WHERE workspace_id = %s",
                        (str(two_tenants["b"]["workspace"]),))
            assert cur.fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_unbound_context_sees_nothing(role, two_tenants):
    """The fail-closed direction: an unset GUC is NULL, and `tenant_id = NULL` is
    NULL rather than true."""
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur)
            cur.execute("SELECT count(*) FROM organisations")
            assert cur.fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_user_private_tables_need_the_user_context(role, two_tenants):
    """A tenant admin must not reach a member's second factor. Binding only the
    tenant must not be enough."""
    uid = two_tenants["a"]["user"]
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=uid)
            # 052: tenant_id must equal the BOUND tenant, not a literal — the
            # INSERT policy's WITH CHECK compares them. Passing the fixture's
            # own tenant is the fixture reflecting the new invariant; hardcoding
            # one would just be routing around it.
            cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) "
                        "VALUES (%s, 'not-a-real-secret', %s, %s)",
                        (f"rls-{uid.hex[:8]}", str(uid),
                         str(two_tenants["a"]["tenant"])))
        conn.commit()

        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"])   # tenant only, no user
            cur.execute("SELECT count(*) FROM totp_secrets WHERE user_id = %s", (str(uid),))
            assert cur.fetchone()[0] == 0, (
                "a tenant-only context read a user-private row; USER_PRIVATE "
                "predicates on app.user_id precisely so this cannot happen")
        conn.rollback()

        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=uid)
            cur.execute("SELECT count(*) FROM totp_secrets WHERE user_id = %s", (str(uid),))
            assert cur.fetchone()[0] == 1, "the owner cannot see their own row"
        conn.rollback()
    finally:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=uid)
            cur.execute("DELETE FROM totp_secrets WHERE user_id = %s", (str(uid),))
        conn.commit()
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_context_does_not_survive_the_transaction(role, two_tenants):
    """`set_config(..., true)` is transaction-local, so a pooled connection
    carries no tenant to its next borrower."""
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
            cur.execute("SELECT count(*) FROM organisations")
            assert cur.fetchone()[0] == 1
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM organisations")
            assert cur.fetchone()[0] == 0, "tenant context survived the transaction"
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("role", ISOLATED_ROLES)
def test_a_failed_transaction_does_not_preserve_context(role, two_tenants):
    """A rolled-back transaction must not leave a bound tenant behind — that is
    the shape of a leak that only appears under error conditions."""
    conn = _connect_as(role)
    try:
        with conn.cursor() as cur:
            _bind(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
            with pytest.raises(psycopg2.Error):
                cur.execute("SELECT 1 FROM does_not_exist")
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            assert (cur.fetchone()[0] or "") == ""
        conn.rollback()
    finally:
        conn.close()


# ── The migrator ─────────────────────────────────────────────────────────────

def test_the_migrator_is_subject_to_rls_until_it_sets_the_owner_role():
    """NOINHERIT is what makes this true. With INHERIT the migrator would carry
    the owner's privileges from its first statement, and the audit trail would
    never show when ownership was actually used."""
    conn = _connect_as("ira_migrator")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            is_super, bypass = cur.fetchone()
            assert not is_super and not bypass

            # Without SET ROLE it is not the owner, so DDL is refused.
            with pytest.raises(psycopg2.Error) as exc:
                cur.execute("ALTER TABLE tenant_memberships DISABLE ROW LEVEL SECURITY")
            assert exc.value.pgcode == "42501"
        conn.rollback()
    finally:
        conn.close()


def test_the_migrator_can_reach_ownership_deliberately():
    """The one permitted escalation, and it must be explicit. This is what the
    migration command does."""
    conn = _connect_as("ira_migrator")
    try:
        with conn.cursor() as cur:
            cur.execute("SET ROLE ira_schema_owner")
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == "ira_schema_owner"
            cur.execute("RESET ROLE")
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == "ira_migrator"
        conn.rollback()
    finally:
        conn.close()


def test_a_role_with_no_membership_sees_nothing(two_tenants):
    """The negative control. `ira_worker` has no membership rows of its own and
    no tenant bound, so it must read nothing — proving the earlier reads
    succeeded because of the bound context and not because the table was open."""
    conn = _connect_as("ira_worker")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tenant_memberships")
            assert cur.fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()


# ── Migration state is not left broken ───────────────────────────────────────

def test_no_table_is_left_with_force_disabled(admin_db):
    """Migrations lift FORCE to backfill and restore it. A migration that failed
    between the two would leave the schema owner able to bypass that table's
    policies, and nothing in a request would notice."""
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "   AND c.relrowsecurity AND NOT c.relforcerowsecurity")
        unforced = sorted(r[0] for r in cur.fetchall())
    assert unforced == ["legacy_identity_map"], (
        f"RLS enabled but not forced on {unforced}. Only legacy_identity_map is "
        f"intended (CLOUD-ADR-0011); anything else is an unclosed backfill window.")
