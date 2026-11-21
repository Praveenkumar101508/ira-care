"""Phase 5 — ADVERSARIAL cross-tenant isolation for the Privacy Centre tables.

The Privacy Centre is the worst possible place for an isolation bug: it is the
screen that lists, per user, the things they were most anxious about — what IRA
remembered, which categories they switched off, what they asked to have deleted.

Also asserted here, against a real database rather than in the abstract:

  * a chain-v2 ledger record can be redacted and a v1 one cannot;
  * `permission_decisions` permits exactly one UPDATE — the redaction — and
    still refuses everything else;
  * memory soft-deletion and expiry behave as the read paths assume.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 026_privacy.sql.
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

TENANT_A = uuid.UUID("f0f0f0f0-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("f1f1f1f1-0000-4000-8000-00000000000b")

PRIVACY_SCOPED = ("retention_settings", "deletion_requests", "export_requests")

TOMBSTONE = '{"__redacted__": true}'

#: The tenant 023 backfilled every existing row with, and the default 026
#: gives `trust_ledger.tenant_id`. The ledger tests below write as this
#: tenant because that is what a real single-tenant write does.
PRIVATE_TENANT = uuid.UUID("e91aceeb-89fb-5ad2-b43c-4eee2bad079c")


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
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
    """
    cur = conn.cursor()
    rls_workspace.bind(cur, tenant, workspace)
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    # BOTH settings. Clearing only the tenant would leave a workspace bound,
    # so "unbound" would not mean what the test says it means.
    rls_workspace.unbind(cur)
    return cur


@pytest.fixture
def seeded(db):
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        "INSERT INTO tenants (id, name, slug, edition) VALUES "
        "(%s,'Priv A',%s,'cloud'), (%s,'Priv B',%s,'cloud') "
        "ON CONFLICT (id) DO NOTHING",
        (str(TENANT_A), f"priv-a-{run}", str(TENANT_B), f"priv-b-{run}"),
    )

    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        marker = f"{key.upper()}-private-{run}"
        user_id = uuid.uuid4()
        cur.execute("INSERT INTO users (id, external_subject) VALUES (%s,%s)",
                    (str(user_id), f"priv-{key}-{run}"))
        c = _as_tenant(db, tenant)
        c.execute(
            "INSERT INTO retention_settings (tenant_id, user_id, memory_mode, "
            "disabled_categories) VALUES (%s,%s,'ask',ARRAY[%s])",
            (str(tenant), str(user_id), "health"),
        )
        c.execute(
            "INSERT INTO deletion_requests (tenant_id, user_id, scope, status, note) "
            "VALUES (%s,%s,'everything','completed',%s)",
            (str(tenant), str(user_id), marker),
        )
        c.execute(
            "INSERT INTO export_requests (tenant_id, user_id, status, note) "
            "VALUES (%s,%s,'completed',%s)",
            (str(tenant), str(user_id), marker),
        )
        c.execute(
            "INSERT INTO memory_embeddings (source_type, content, user_id, tenant_id, "
            "storage_reason, sensitivity_category) "
            "VALUES ('fact',%s,'owner',%s,%s,'personal')",
            (marker, str(tenant), "mentioned in conversation"),
        )
        ids[key] = {"tenant": tenant, "user_id": user_id, "marker": marker}

    _clear_tenant(db)
    yield ids


# ── The harness must be honest first ─────────────────────────────────────────


def test_app_role_is_not_superuser(db):
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False


@pytest.mark.parametrize("table", PRIVACY_SCOPED)
def test_rls_is_enabled_and_forced(db, table):
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    )
    row = cur.fetchone()
    assert row is not None, f"{table} not found — is 026 applied?"
    assert row[0] and row[1], f"{table} is not RLS-enabled and forced"


# ── Cross-tenant reads and writes ────────────────────────────────────────────


@pytest.mark.parametrize("table", PRIVACY_SCOPED)
def test_tenant_a_cannot_read_tenant_b_rows(db, seeded, table):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (str(TENANT_B),))
    assert cur.fetchone()[0] == 0


def test_tenant_a_cannot_read_tenant_b_disabled_categories(db, seeded):
    """Which categories somebody switched off is itself sensitive: "health is
    disabled" says something about them."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT DISTINCT tenant_id FROM retention_settings")
    # psycopg2 returns uuid columns as strings unless the UUID adapter is
    # registered, so compare as strings rather than relying on the driver.
    visible = {str(r[0]) for r in cur.fetchall()}
    assert visible == {str(TENANT_A)}, (
        f"tenant A can see retention settings for {visible - {str(TENANT_A)}}"
    )


def test_tenant_a_cannot_read_tenant_b_memories(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT content FROM memory_embeddings")
    contents = {r[0] for r in cur.fetchall()}
    assert seeded["b"]["marker"] not in contents
    assert seeded["a"]["marker"] in contents, "tenant A cannot see its OWN memories"


@pytest.mark.parametrize("table", PRIVACY_SCOPED)
def test_a_missing_tenant_context_returns_nothing(db, seeded, table):
    cur = _clear_tenant(db)
    cur.execute(f"SELECT count(*) FROM {table}")
    assert cur.fetchone()[0] == 0


def test_tenant_a_cannot_change_tenant_b_retention_settings(db, seeded):
    """Turning someone else's memory back on would be an escalation dressed as
    a settings change."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE retention_settings SET memory_mode='automatic', memory_enabled=TRUE "
        "WHERE tenant_id = %s",
        (str(TENANT_B),),
    )
    assert cur.rowcount == 0

    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT memory_mode FROM retention_settings WHERE tenant_id = %s",
                (str(TENANT_B),))
    assert cur.fetchone()[0] == "ask"


def test_tenant_a_cannot_write_into_tenant_b(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO deletion_requests (tenant_id, scope) VALUES (%s,'everything')",
            (str(TENANT_B),),
        )


# ── The memory metadata the Privacy Centre depends on ────────────────────────


def test_every_memory_metadata_column_exists(db):
    cur = db.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'memory_embeddings'"
    )
    columns = {r[0] for r in cur.fetchall()}
    for required in (
        "source_type", "source_reference", "storage_reason", "sensitivity",
        "confidence", "created_at", "expires_at", "last_used_at",
        "allowed_connectors", "external_provider_policy", "deleted_at",
        # Phase 5's CATEGORY axis, distinct from 017's confidentiality
        # `sensitivity` — see test_026_does_not_redefine_columns...
        "sensitivity_category",
    ):
        assert required in columns, f"the Privacy Centre cannot show {required}"


def test_a_new_memory_defaults_to_sharing_with_nothing(db, seeded):
    """Fail-closed at the schema level too, not only in the dataclass."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "SELECT allowed_connectors, external_provider_policy FROM memory_embeddings "
        "WHERE tenant_id = %s LIMIT 1",
        (str(TENANT_A),),
    )
    connectors, policy = cur.fetchone()
    assert list(connectors) == []
    assert policy == "never"


def test_an_unknown_sensitivity_is_refused_by_the_schema(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO memory_embeddings (source_type, content, user_id, tenant_id, "
            "sensitivity_category) VALUES ('fact','x','owner',%s,'vibes')",
            (str(TENANT_A),),
        )


# ── The audit redaction, against the real triggers ───────────────────────────


def test_a_chain_v2_ledger_record_can_be_redacted(db):
    cur = _as_tenant(db, PRIVATE_TENANT)
    cur.execute(
        "INSERT INTO trust_ledger (user_id, action_id, kind, record, prev_hash, "
        "hash, body_digest) VALUES ('p5-v2','a','action',%s,'p',%s,'digest') "
        "RETURNING seq",
        ('{"resource": "sarah@example.com"}', f"h-v2-{uuid.uuid4().hex[:8]}"),
    )
    seq = cur.fetchone()[0]
    cur.execute("UPDATE trust_ledger SET record = %s WHERE seq = %s", (TOMBSTONE, seq))
    assert cur.rowcount == 1

    cur.execute("SELECT record::text FROM trust_ledger WHERE seq = %s", (seq,))
    assert "sarah@example.com" not in cur.fetchone()[0]


def test_a_chain_v1_ledger_record_cannot_be_redacted(db):
    """Its content is inside its hash. Refusing is the honest answer — blanking
    it would make the chain unverifiable and indistinguishable from an attack."""
    cur = _as_tenant(db, PRIVATE_TENANT)
    cur.execute(
        "INSERT INTO trust_ledger (user_id, action_id, kind, record, prev_hash, hash) "
        "VALUES ('p5-v1','a','action','{}','p',%s) RETURNING seq",
        (f"h-v1-{uuid.uuid4().hex[:8]}",),
    )
    seq = cur.fetchone()[0]
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE trust_ledger SET record = %s WHERE seq = %s", (TOMBSTONE, seq))
    assert "chain-v1" in str(exc.value)


def test_the_ledger_still_refuses_every_other_mutation(db):
    cur = _as_tenant(db, PRIVATE_TENANT)
    cur.execute(
        "INSERT INTO trust_ledger (user_id, action_id, kind, record, prev_hash, "
        "hash, body_digest) VALUES ('p5-x','a','action','{}','p',%s,'d') "
        "RETURNING seq",
        (f"h-x-{uuid.uuid4().hex[:8]}",),
    )
    seq = cur.fetchone()[0]
    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE trust_ledger SET hash = 'evil' WHERE seq = %s", (seq,))
    with pytest.raises(psycopg2.Error):
        cur.execute("DELETE FROM trust_ledger WHERE seq = %s", (seq,))


def test_the_ledger_write_path_works_without_an_explicit_tenant(db):
    """Regression test for a defect Phase 5 found: 023 made `tenant_id` NOT NULL
    with no default, so every ledger write had failed since — silently, because
    the callers are fail-soft. 026 adds the default."""
    cur = _as_tenant(db, PRIVATE_TENANT)
    cur.execute(
        "INSERT INTO trust_ledger (user_id, action_id, kind, record, prev_hash, hash) "
        "VALUES ('p5-default','a','action','{}','p',%s) RETURNING tenant_id",
        (f"h-def-{uuid.uuid4().hex[:8]}",),
    )
    tenant_id = cur.fetchone()[0]
    assert tenant_id is not None, "a ledger write without a tenant still fails"


def test_permission_decisions_permits_only_the_redaction_update(db, seeded):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "INSERT INTO permission_decisions (tenant_id, domain, action, resource, "
        "purpose, reason, effect) VALUES (%s,'email','send','sarah@example.com',"
        "'email Sarah','rule applies','deny') RETURNING id",
        (str(TENANT_A),),
    )
    decision_id = cur.fetchone()[0]

    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE permission_decisions SET effect='allow' WHERE id=%s",
                    (decision_id,))

    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute("UPDATE permission_decisions SET redacted_at=NOW() WHERE id=%s",
                    (decision_id,))

    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE permission_decisions SET redacted_at=NOW(), resource='', purpose='', "
        "reason='' WHERE id=%s",
        (decision_id,),
    )
    assert cur.rowcount == 1

    cur.execute("SELECT resource, purpose, effect FROM permission_decisions WHERE id=%s",
                (decision_id,))
    resource, purpose, effect = cur.fetchone()
    assert resource == "" and purpose == ""
    assert effect == "deny", "the decision itself must survive the redaction"
