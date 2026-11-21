"""Phase 3B Batch 4 — the last four tenant-scoped tables.

The test that matters most is
`test_two_tenants_can_calibrate_the_same_domain`. Before `044`,
`strategy_calibration` had `PRIMARY KEY (domain)` and
`agents/strategy_calibration.py` UPSERTS on it — so a second tenant calibrating
`sales` did not get an error. It **overwrote** the first tenant's multiplier,
offset and sample count, and the first tenant's forecasts then shifted for
reasons nothing in their account explained.

A refusal is a bug report. A silent overwrite is a data-corruption incident, and
it is the reason this table needed its own migration.

Everything runs against a real PostgreSQL under a **non-superuser** role that
owns nothing, asserted before any isolation claim is made.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — Batch 4 tests skipped",
                allow_module_level=True)

#: The four tables Batch 4 migrated. Named once; every sweep below iterates it,
#: so adding a fifth table to the batch without a test is not possible.
BATCH_4_TABLES = ("business_events", "model_performance",
                  "strategy_predictions", "strategy_calibration")

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


def bind(cur, tenant=None, user=None):
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant) if tenant else "",))
    cur.execute("SELECT set_config('app.user_id',   %s, true)", (str(user) if user else "",))


@pytest.fixture
def two_tenants(admin_db):
    made = {}
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            tid = uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name, edition, status) "
                        "VALUES (%s, %s, 'cloud', 'active')", (str(tid), f"b4-{label}"))
            made[label] = tid
    admin_db.commit()
    yield made
    admin_db.rollback()
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            # RESTRICT on tenant_id, so content goes before the tenant does —
            # which is the constraint working, not an inconvenience.
            for table in BATCH_4_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (str(made[label]),))
            cur.execute("DELETE FROM tenants WHERE id = %s", (str(made[label]),))
    admin_db.commit()


def _insert(cur, table, tenant, marker):
    """One row per table, with a value the test can find again."""
    if table == "business_events":
        cur.execute("INSERT INTO business_events (event_type, title, tenant_id) "
                    "VALUES ('lead', %s, %s) RETURNING id", (marker, str(tenant)))
    elif table == "model_performance":
        cur.execute("INSERT INTO model_performance (model_name, latency_ms, tenant_id) "
                    "VALUES (%s, 1, %s) RETURNING id", (marker, str(tenant)))
    elif table == "strategy_predictions":
        cur.execute("INSERT INTO strategy_predictions "
                    "(question, option_name, success_probability, risk, effort, utility, tenant_id) "
                    "VALUES (%s, 'opt', 0.5, 0.1, 0.1, 0.5, %s) RETURNING id", (marker, str(tenant)))
    elif table == "strategy_calibration":
        cur.execute("INSERT INTO strategy_calibration (domain, tenant_id) "
                    "VALUES (%s, %s) RETURNING domain", (marker, str(tenant)))
    return cur.fetchone()[0]


def _marker_column(table):
    return {"business_events": "title", "model_performance": "model_name",
            "strategy_predictions": "question", "strategy_calibration": "domain"}[table]


# ── The harness ──────────────────────────────────────────────────────────────

def test_the_role_is_not_privileged(db):
    with db.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
        role, is_super, bypass = cur.fetchone()
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname='public' AND c.relname = ANY(%s) "
                    "   AND pg_get_userbyid(c.relowner) = current_user", (list(BATCH_4_TABLES),))
        owned = cur.fetchone()[0]
    assert not is_super and not bypass, f"{role} bypasses RLS — nothing below proves anything"
    assert owned == 0, f"{role} owns a Batch 4 table and can therefore disable its policies"


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_the_table_has_a_tenant_and_forced_rls(admin_db, table):
    with admin_db.cursor() as cur:
        cur.execute("SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    " JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE n.nspname='public' AND c.relname = %s", (table,))
        enabled, forced = cur.fetchone()
        cur.execute("SELECT is_nullable FROM information_schema.columns "
                    " WHERE table_schema='public' AND table_name=%s AND column_name='tenant_id'", (table,))
        nullable = cur.fetchone()
    assert enabled and forced, f"{table} does not have RLS enabled AND forced"
    assert nullable is not None, f"{table} has no tenant_id"
    assert nullable[0] == "NO", f"{table}.tenant_id is nullable — the policies rest on it"


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_the_table_has_all_four_command_policies(admin_db, table):
    """A missing DELETE policy means DELETE silently matches nothing; a missing
    UPDATE policy means the same for updates. Both read as "the row is not
    there" rather than "you may not"."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT cmd FROM pg_policies WHERE schemaname='public' AND tablename=%s", (table,))
        cmds = sorted(r[0] for r in cur.fetchall())
    assert cmds == ["DELETE", "INSERT", "SELECT", "UPDATE"], f"{table} has policies {cmds}"


# ── Isolation, per table ─────────────────────────────────────────────────────

@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_tenant_a_can_read_its_own_row(db, two_tenants, table):
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        _insert(cur, table, two_tenants["a"], marker)
        cur.execute(f"SELECT count(*) FROM {table} WHERE {_marker_column(table)} = %s", (marker,))
        assert cur.fetchone()[0] == 1
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_tenant_b_cannot_read_tenant_a(db, admin_db, two_tenants, table):
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        _insert(cur, table, two_tenants["a"], marker)
    admin_db.commit()
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["b"])
        cur.execute(f"SELECT count(*) FROM {table} WHERE {_marker_column(table)} = %s", (marker,))
        assert cur.fetchone()[0] == 0
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_tenant_b_cannot_update_tenant_a(db, admin_db, two_tenants, table):
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        _insert(cur, table, two_tenants["a"], marker)
    admin_db.commit()
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["b"])
        cur.execute(f"UPDATE {table} SET tenant_id = tenant_id "
                    f"WHERE {_marker_column(table)} = %s", (marker,))
        assert cur.rowcount == 0, "tenant B updated tenant A's row"
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_tenant_b_cannot_delete_tenant_a(db, admin_db, two_tenants, table):
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        _insert(cur, table, two_tenants["a"], marker)
    admin_db.commit()
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["b"])
        cur.execute(f"DELETE FROM {table} WHERE {_marker_column(table)} = %s", (marker,))
        assert cur.rowcount == 0, "tenant B deleted tenant A's row"
    db.rollback()
    with admin_db.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {_marker_column(table)} = %s", (marker,))
        assert cur.fetchone()[0] == 1, "the row is gone"
    admin_db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_tenant_b_cannot_reach_tenant_a_through_a_join(db, admin_db, two_tenants, table):
    """A policy that only filtered a direct SELECT would still leak through a
    join against a table the caller CAN read."""
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        _insert(cur, table, two_tenants["a"], marker)
    admin_db.commit()
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["b"])
        cur.execute(
            f"SELECT count(*) FROM {table} t JOIN tenants n ON n.id = t.tenant_id "
            f" WHERE t.{_marker_column(table)} = %s", (marker,))
        assert cur.fetchone()[0] == 0
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_a_row_cannot_be_moved_to_another_tenant(db, two_tenants, table):
    """The UPDATE policy carries WITH CHECK as well as USING. Without it a
    tenant could reassign `tenant_id` — which reads as an ordinary update and is
    a transfer of ownership."""
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        _insert(cur, table, two_tenants["a"], marker)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(f"UPDATE {table} SET tenant_id = %s "
                        f"WHERE {_marker_column(table)} = %s",
                        (str(two_tenants["b"]), marker))
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_missing_tenant_context_returns_nothing(db, admin_db, two_tenants, table):
    """Fail-closed: an unset GUC is NULL, and `tenant_id = NULL` is NULL rather
    than true."""
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        _insert(cur, table, two_tenants["a"], marker)
    admin_db.commit()
    with db.cursor() as cur:
        bind(cur)
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_an_unscoped_insert_fails_rather_than_landing_anywhere(db, table):
    """The DEFAULT resolves to NULL with no tenant bound, which hits NOT NULL.
    An unscoped write is refused rather than landing in an arbitrary tenant."""
    # `tenant_id` is OMITTED, so the column DEFAULT applies — which is the real
    # path an application takes. Passing an explicit NULL would test a different
    # thing (the NOT NULL) and would miss whether the default fails closed.
    omitted = {
        "business_events": "INSERT INTO business_events (event_type, title) VALUES ('lead', 'x')",
        "model_performance": "INSERT INTO model_performance (model_name, latency_ms) VALUES ('x', 1)",
        "strategy_predictions": "INSERT INTO strategy_predictions "
                                "(question, option_name, success_probability, risk, effort, utility) "
                                "VALUES ('x', 'o', 0.5, 0.1, 0.1, 0.5)",
        "strategy_calibration": "INSERT INTO strategy_calibration (domain) VALUES ('x')",
    }[table]
    with db.cursor() as cur:
        bind(cur)
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute(omitted)
        # NOT NULL (23502) or the INSERT policy (42501) — both are refusals, and
        # which one fires depends on evaluation order rather than on intent.
        assert exc.value.pgcode in ("23502", "42501"), exc.value.pgcode
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_context_does_not_survive_the_transaction(db, two_tenants, table):
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        _insert(cur, table, two_tenants["a"], f"m-{uuid.uuid4().hex[:8]}")
    db.rollback()
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, "tenant context survived the transaction"
    db.rollback()


@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_a_failed_transaction_clears_context(db, two_tenants, table):
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        with pytest.raises(psycopg2.Error):
            cur.execute("SELECT 1 FROM does_not_exist")
    db.rollback()
    with db.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true)")
        assert (cur.fetchone()[0] or "") == ""
    db.rollback()


# ── The collision that was silent ────────────────────────────────────────────

def test_two_tenants_can_calibrate_the_same_domain(db, two_tenants):
    """**The defect `044` closes.**

    `PRIMARY KEY (domain)` was global, and the write path upserts — so tenant B
    calibrating `sales` overwrote tenant A's calibration rather than being
    refused. Both rows must now coexist with their own values.
    """
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        cur.execute("INSERT INTO strategy_calibration (domain, multiplier, tenant_id) "
                    "VALUES ('sales', 1.5, %s)", (str(two_tenants["a"]),))
    db.commit()

    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["b"])
        # The same domain name, in a different tenant. Before 044 this was an
        # UPDATE of somebody else's row.
        cur.execute("INSERT INTO strategy_calibration (domain, multiplier, tenant_id) "
                    "VALUES ('sales', 0.5, %s)", (str(two_tenants["b"]),))
    db.commit()

    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        cur.execute("SELECT multiplier FROM strategy_calibration WHERE domain = 'sales'")
        rows = cur.fetchall()
    assert len(rows) == 1 and rows[0][0] == 1.5, (
        f"tenant A's calibration is {rows} — tenant B's write changed it")
    db.rollback()


def test_the_upsert_arbitrates_on_the_tenant_scoped_key(db, two_tenants):
    """`ON CONFLICT (tenant_id, domain)`. Naming the old key would fail to infer
    an arbiter index — a loud error, which is the outcome to want, but the
    application must use the new one."""
    with db.cursor() as cur:
        bind(cur, tenant=two_tenants["a"])
        for multiplier in (1.1, 2.2):
            cur.execute(
                "INSERT INTO strategy_calibration (domain, multiplier, tenant_id) "
                "VALUES ('hiring', %s, %s) "
                "ON CONFLICT (tenant_id, domain) DO UPDATE SET multiplier = EXCLUDED.multiplier",
                (multiplier, str(two_tenants["a"])))
        cur.execute("SELECT multiplier, count(*) OVER () FROM strategy_calibration "
                    " WHERE domain = 'hiring'")
        multiplier, n = cur.fetchone()
    assert n == 1 and multiplier == 2.2, "the upsert made a second row instead of updating"
    db.rollback()


def test_the_old_global_key_is_gone(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    " JOIN pg_class t ON t.oid = c.conrelid "
                    " WHERE t.relname = 'strategy_calibration' AND c.contype = 'p'")
        definition = cur.fetchone()[0]
    assert definition == "PRIMARY KEY (tenant_id, domain)", definition


# ── Verification view ────────────────────────────────────────────────────────

def test_the_status_view_sees_a_planted_row(admin_db, two_tenants):
    """`security_invoker`. A view running as its owner reports what the schema
    owner can see — nothing — to every caller including a superuser."""
    marker = f"m-{uuid.uuid4().hex[:8]}"
    with admin_db.cursor() as cur:
        cur.execute("SELECT total_rows FROM batch4_tenant_scope_status "
                    " WHERE table_name = 'business_events'")
        before = cur.fetchone()[0]
        _insert(cur, "business_events", two_tenants["a"], marker)
        cur.execute("SELECT total_rows, unattributed, contract_ready, vacuous "
                    "  FROM batch4_tenant_scope_status WHERE table_name = 'business_events'")
        total, unattributed, ready, vacuous = cur.fetchone()
    assert total == before + 1, "the view did not see a row that exists"
    assert unattributed == 0 and ready is True
    assert vacuous is False, "a table with a row is reported as empty"
    admin_db.rollback()


def test_an_empty_table_is_not_reported_as_a_finished_migration(admin_db):
    """`contract_ready` is a `count = 0` test, so it is true on an empty table.
    `vacuous` is what stops that reading as "the migration completed"."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT table_name, total_rows, contract_ready, vacuous "
                    "  FROM batch4_tenant_scope_status ORDER BY table_name")
        rows = cur.fetchall()
    assert len(rows) == 4
    for name, total, ready, vacuous in rows:
        assert ready is True, f"{name} has unattributed rows"
        assert vacuous == (total == 0), (
            f"{name}: vacuous={vacuous} with {total} rows — the flag must say "
            f"whether contract_ready means anything")


# ── The runtime still cannot alter these tables ──────────────────────────────

@pytest.mark.parametrize("table", BATCH_4_TABLES)
def test_the_runtime_cannot_disable_the_new_policies(db, table):
    """Batch 4 added policies to four more tables. Role separation must hold for
    them too, or the new isolation is advisory."""
    for statement in (
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"DROP POLICY tenant_select ON {table}",
        f"CREATE POLICY escape ON {table} USING (true)",
        f"TRUNCATE TABLE {table}",
    ):
        with db.cursor() as cur:
            with pytest.raises(psycopg2.Error) as exc:
                cur.execute(statement)
        assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE, f"{statement} -> {exc.value.pgcode}"
        db.rollback()
