"""Phase 4 — ADVERSARIAL cross-tenant isolation for the governance tables.

Same posture as Phases 2 and 3: two tenants, one writes, the other actively
tries to reach it, against a real PostgreSQL with a non-superuser role.

What is at stake here is different from earlier phases, and worse. A user's
Constitution is a list of the things they are most anxious about — health,
family, money, what their assistant must never do. Reading another tenant's
constitution_rules would disclose that directly. And `permission_decisions` is a
minute-by-minute record of what someone asked their assistant to do.

Also asserted, because "deliberately not scoped" is indistinguishable from
"somebody forgot" unless it is checked: `tool_policies` is global, carries no
`tenant_id`, and is readable without a tenant context — while the four
customer-data tables deny without one.

Running them: see the header of test_tenant_isolation_rls.py. The migration
chain must include 025_governance.sql.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database RLS tests skipped"
)

TENANT_A = uuid.UUID("dddddddd-0000-4000-8000-00000000000d")
TENANT_B = uuid.UUID("eeeeeeee-0000-4000-8000-00000000000e")

#: New in Phase 4, carrying customer data.
GOVERNANCE_SCOPED = (
    "constitution_rules",
    "permission_grants",
    "permission_decisions",
    "approval_events",
)


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _as_tenant(conn, tenant: uuid.UUID):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    return cur


@pytest.fixture
def seeded(db):
    """Two tenants, each with a constitution, a grant and a decision."""
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO tenants (id, name, slug, edition) VALUES
            (%s, 'Gov A', %s, 'cloud'), (%s, 'Gov B', %s, 'cloud')
        ON CONFLICT (id) DO NOTHING
        """,
        (str(TENANT_A), f"gov-a-{run}", str(TENANT_B), f"gov-b-{run}"),
    )

    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        marker = f"{key.upper()}-secret-{run}"
        user_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, external_subject) VALUES (%s, %s)",
            (str(user_id), f"gov-{key}-{run}"),
        )
        c = _as_tenant(db, tenant)
        c.execute(
            "INSERT INTO constitution_rules "
            "(tenant_id, user_id, domain, action, effect, label) "
            "VALUES (%s,%s,'memory','write','deny',%s) RETURNING id",
            (str(tenant), str(user_id), marker),
        )
        rule_id = c.fetchone()[0]
        c.execute(
            "INSERT INTO permission_grants "
            "(tenant_id, user_id, connector, scope, purpose, granted_at) "
            "VALUES (%s,%s,'calendar','read',%s, NOW()) RETURNING id",
            (str(tenant), str(user_id), marker),
        )
        grant_id = c.fetchone()[0]
        c.execute(
            "INSERT INTO permission_decisions "
            "(tenant_id, user_id, domain, action, purpose, effect, reason) "
            "VALUES (%s,%s,'email','send',%s,'deny',%s)",
            (str(tenant), str(user_id), marker, marker),
        )
        c.execute(
            "INSERT INTO approval_requests (tenant_id, domain, action, purpose) "
            "VALUES (%s,'email','send',%s) RETURNING id",
            (str(tenant), marker),
        )
        approval_id = c.fetchone()[0]
        c.execute(
            "INSERT INTO approval_events (tenant_id, approval_request_id, event, detail) "
            "VALUES (%s,%s,'requested','{}'::jsonb)",
            (str(tenant), str(approval_id)),
        )
        ids[key] = {
            "tenant": tenant, "user_id": user_id, "marker": marker,
            "rule_id": rule_id, "grant_id": grant_id, "approval_id": approval_id,
        }

    _clear_tenant(db)
    yield ids


# ── The harness must be honest first ─────────────────────────────────────────


def test_app_role_is_not_superuser(db):
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False, (
        "The test role is a SUPERUSER — superusers bypass RLS, so these tests "
        "would pass without proving anything."
    )


@pytest.mark.parametrize("table", GOVERNANCE_SCOPED)
def test_rls_is_enabled_and_forced(db, table):
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    )
    row = cur.fetchone()
    assert row is not None, f"{table} not found — is 025 applied?"
    enabled, forced = row
    assert enabled, f"{table} does not have ROW LEVEL SECURITY enabled"
    assert forced, f"{table} does not FORCE ROW LEVEL SECURITY (owner bypasses)"


# ── Cross-tenant reads ───────────────────────────────────────────────────────


@pytest.mark.parametrize("table", GOVERNANCE_SCOPED)
def test_tenant_a_cannot_read_tenant_b_rows(db, seeded, table):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (str(TENANT_B),))
    assert cur.fetchone()[0] == 0, f"tenant A read tenant B's {table}"


def test_tenant_a_cannot_read_tenant_b_constitution(db, seeded):
    """A Constitution is a list of what someone is most anxious about."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT label FROM constitution_rules")
    labels = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in labels
    assert seeded["a"]["marker"] in labels, "tenant A cannot see its OWN rules"


def test_tenant_a_cannot_read_tenant_b_decision_history(db, seeded):
    """permission_decisions is a minute-by-minute record of what someone asked
    their assistant to do."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT reason FROM permission_decisions")
    reasons = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in reasons


def test_tenant_a_cannot_read_tenant_b_grants(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT purpose FROM permission_grants")
    purposes = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in purposes


@pytest.mark.parametrize("table", GOVERNANCE_SCOPED)
def test_a_missing_tenant_context_returns_nothing(db, seeded, table):
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] == 0, f"{table} returned rows with no tenant context"


@pytest.mark.parametrize("table", GOVERNANCE_SCOPED)
def test_a_cleared_context_denies_cleanly_rather_than_raising(db, seeded, table):
    """NULLIF(..., '') — a cleared GUC is the empty string, and ''::uuid raises."""
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")  # must not raise
    assert cur.fetchone()[0] == 0


# ── Cross-tenant writes ──────────────────────────────────────────────────────


def test_tenant_a_cannot_write_a_rule_into_tenant_b(db, seeded):
    """Writing a permissive rule into someone else's Constitution would be the
    most direct possible escalation."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO constitution_rules (tenant_id, domain, action, effect, label) "
            "VALUES (%s,'*','*','allow','planted')",
            (str(TENANT_B),),
        )


def test_tenant_a_cannot_disable_tenant_b_rules(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE constitution_rules SET enabled = FALSE WHERE tenant_id = %s",
        (str(TENANT_B),),
    )
    assert cur.rowcount == 0, "tenant A disabled tenant B's constitution"

    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT enabled FROM constitution_rules WHERE tenant_id = %s",
                (str(TENANT_B),))
    assert all(row[0] for row in cur.fetchall()), "tenant B's rules were disabled"


def test_tenant_a_cannot_revoke_tenant_b_grants(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE permission_grants SET revoked_at = NOW() WHERE tenant_id = %s",
        (str(TENANT_B),),
    )
    assert cur.rowcount == 0


def test_a_write_with_no_tenant_context_is_refused(db, seeded):
    cur = _clear_tenant(db)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO constitution_rules (tenant_id, domain, action, effect) "
            "VALUES (%s,'*','*','deny')",
            (str(TENANT_A),),
        )


# ── The decision log is append-only ──────────────────────────────────────────


def test_a_decision_cannot_be_edited_even_by_its_own_tenant(db, seeded):
    """Evidence that can be rewritten is not evidence. This is the same posture
    020_trust_ledger.sql takes."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE permission_decisions SET effect = 'allow'")
    assert "append-only" in str(exc.value).lower()


def test_a_decision_cannot_be_deleted_even_by_its_own_tenant(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("DELETE FROM permission_decisions")
    assert "append-only" in str(exc.value).lower()


def test_a_denial_is_recorded_not_only_a_permission(db, seeded):
    """A log that only records what happened cannot answer "why did IRA refuse?"
    — and a denial leaving no trace is indistinguishable from a request that was
    never made, which is what someone probing the boundary would want."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT effect FROM permission_decisions WHERE tenant_id = %s",
                (str(TENANT_A),))
    effects = {r[0] for r in cur.fetchall()}
    assert "deny" in effects


# ── tool_policies is global ON PURPOSE ───────────────────────────────────────


def test_tool_policies_is_readable_without_a_tenant_context(db):
    """It describes what the BUILD permits, identically for every tenant.
    Per-tenant copies are how a safety rail ends up tightened for some customers
    and not others."""
    cur = _clear_tenant(db)
    cur.execute("SELECT count(*) FROM tool_policies")
    assert cur.fetchone()[0] > 0, "tool_policies is empty — was 025's seed applied?"


def test_tool_policies_carries_no_tenant_column(db):
    """The tripwire for somebody starting to scope it without adding a policy,
    which would look scoped and enforce nothing."""
    cur = db.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'tool_policies' AND column_name = 'tenant_id'"
    )
    assert cur.fetchone() is None


def test_the_never_automatic_actions_are_seeded_that_way(db):
    """The database's own copy of the ceilings the code enforces. Both must
    agree, or which one is true depends on the code path."""
    cur = db.cursor()
    cur.execute(
        "SELECT domain, action, max_effect FROM tool_policies "
        "WHERE (domain, action) IN (('email','send'), ('payment','execute'), "
        "('social','publish'), ('security','change'), ('email','delete'))"
    )
    rows = dict(((d, a), m) for d, a, m in cur.fetchall())
    assert rows[("email", "send")] == "require_confirmation"
    for key in (("payment", "execute"), ("social", "publish"),
                ("security", "change"), ("email", "delete")):
        assert rows[key] == "deny", f"{key} is not seeded as denied"
