"""Phase 8 — ADVERSARIAL isolation, and the break-glass guarantees, against a
real database.

Two things are asserted here that cannot be asserted anywhere else.

**Isolation.** `model_usage` is the billing record and the answer to "where did
my words go?". `admin_access_log` is the record of every time an administrator
read someone's content. A cross-tenant read of either is a serious incident.

**The break-glass bounds.** The threat model requires administrator access to be
time-bound, user-authorised and logged. Each of those is a database constraint or
trigger here, and each is attacked: a grant with no authorisation, a week-long
window, a log entry with no grant, one outside the window, one for a different
admin, and an attempt to extend a grant or edit the log.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 029_commercial.sql.
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

TENANT_A = uuid.UUID("f6f6f6f6-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("f7f7f7f7-0000-4000-8000-00000000000b")

SCOPED = ("usage_quotas", "model_usage", "admin_access_grants", "admin_access_log")

REASON = "investigating the billing discrepancy Praveen reported"


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")


@pytest.fixture
def catalogue_writer():
    """A connection that may write `model_providers`.

    ── Why this exists (P8-07) ────────────────────────────────────────────

    The three constraint tests below used the `db` fixture, which connects as
    `ira_app`. That worked because `ira_app` could write this table — and it
    could write it because `029` predates `067`/`068`, each of which revoked
    deliberately on their own tables, so `029`'s kept the DML that `042`'s
    default privileges hand out. `076` revokes it: the process that answers
    requests must not be able to edit the record of where it sent them.

    So these tests moved to the role that legitimately writes the catalogue.
    Nothing about what they assert changed — the CHECK constraints are still
    exercised, and `test_the_runtime_role_cannot_write_the_catalogue` below is
    the new assertion that `ira_app` may not.
    """
    if not ADMIN_DSN:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")
    conn = psycopg2.connect(ADMIN_DSN)
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
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        "INSERT INTO tenants (id, name, slug, edition) VALUES "
        "(%s,'Comm A',%s,'cloud'), (%s,'Comm B',%s,'cloud') "
        "ON CONFLICT (id) DO NOTHING",
        (str(TENANT_A), f"comm-a-{run}", str(TENANT_B), f"comm-b-{run}"),
    )
    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        user_id = uuid.uuid4()
        cur.execute("INSERT INTO users (id, external_subject) VALUES (%s,%s)",
                    (str(user_id), f"comm-{key}-{run}"))
        cur = _as_tenant(db, tenant)
        cur.execute(
            "INSERT INTO usage_quotas (tenant_id, monthly_token_limit) "
            "VALUES (%s, 1000000)", (str(tenant),),
        )
        cur.execute(
            "INSERT INTO model_usage (tenant_id,user_id,provider_id,locality,"
            "tier,sensitivity,privacy_mode,outcome,prompt_tokens,"
            "completion_tokens,cost) VALUES (%s,%s,'ollama-local','local','fast',"
            "'health','local_only','ok',100,200,0) RETURNING id",
            (str(tenant), str(user_id)),
        )
        usage_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,scope,user_authorised_at,expires_at) "
            "VALUES (%s,%s,%s,%s,'conversations',NOW(),NOW()+interval '2 hours') "
            "RETURNING id",
            (str(tenant), str(user_id), f"support-{key}@example.com", REASON),
        )
        grant_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO admin_access_log (tenant_id,grant_id,admin_identity,"
            "what_was_read,record_count) VALUES (%s,%s,%s,'12 conversations "
            "record(s)',12) RETURNING id",
            (str(tenant), str(grant_id), f"support-{key}@example.com"),
        )
        ids[key] = {
            "tenant": tenant, "user_id": user_id, "usage_id": usage_id,
            "grant_id": grant_id, "log_id": cur.fetchone()[0],
            "admin": f"support-{key}@example.com",
        }
    _clear_tenant(db)
    yield ids
    # Grants and the log are append-only; only usage and quotas are removable.
    cur = db.cursor()
    for entry in ids.values():
        cur.execute("SELECT set_config('app.tenant_id', %s, false)",
                    (str(entry["tenant"]),))
        cur.execute("DELETE FROM model_usage WHERE tenant_id = %s",
                    (str(entry["tenant"]),))
        cur.execute("DELETE FROM usage_quotas WHERE tenant_id = %s",
                    (str(entry["tenant"]),))
    _clear_tenant(db)


# ── The harness must be honest first ─────────────────────────────────────────


def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS, making every test below vacuous."""
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False


@pytest.mark.parametrize("table", SCOPED)
def test_row_level_security_is_enabled_and_forced(db, table):
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
        (table,),
    )
    enabled, forced = cur.fetchone()
    assert enabled and forced, f"{table} does not ENABLE and FORCE RLS"


# ── Isolation ────────────────────────────────────────────────────────────────


def test_a_tenant_cannot_see_another_tenants_usage(seeded, db):
    """The billing record, and the answer to "where did my words go?"."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM model_usage WHERE id = %s",
                (str(seeded["a"]["usage_id"]),))
    assert cur.fetchone()[0] == 0
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT count(*) FROM model_usage WHERE id = %s",
                (str(seeded["a"]["usage_id"]),))
    assert cur.fetchone()[0] == 1


def test_a_tenant_cannot_see_another_tenants_quota(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM usage_quotas WHERE tenant_id = %s",
                (str(TENANT_A),))
    assert cur.fetchone()[0] == 0


def test_a_tenant_cannot_see_another_tenants_admin_access_log(seeded, db):
    """Who read whose content is not a cross-tenant fact."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM admin_access_log WHERE id = %s",
                (str(seeded["a"]["log_id"]),))
    assert cur.fetchone()[0] == 0


def test_no_tenant_context_sees_nothing(seeded, db):
    cur = _clear_tenant(db)
    for table in SCOPED:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, f"{table} is readable with no tenant context"


def test_a_tenant_cannot_write_usage_into_another_tenant(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO model_usage (tenant_id,provider_id) VALUES (%s,'ollama-local')",
            (str(TENANT_A),),
        )


def test_a_tenant_cannot_grant_itself_access_to_another_tenants_user(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,user_authorised_at,expires_at) "
            "VALUES (%s,%s,'attacker@ex',%s,NOW(),NOW()+interval '1 hour')",
            (str(TENANT_A), str(seeded["a"]["user_id"]), REASON),
        )


def test_a_tenant_cannot_raise_another_tenants_quota(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    cur.execute("UPDATE usage_quotas SET monthly_token_limit = 999999999 "
                "WHERE tenant_id = %s", (str(TENANT_A),))
    assert cur.rowcount == 0


# ── The metering record cannot contradict the routing guarantee ──────────────


def test_local_only_cannot_record_a_successful_external_call(seeded, db):
    """The routing guarantee written into the billing record, so a violation is
    visible in the data and not only in the code that produced it."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO model_usage (tenant_id,provider_id,privacy_mode,"
            "outcome,locality) VALUES (%s,'ollama-local','local_only','ok',"
            "'external')", (str(TENANT_A),),
        )
    assert "local_only_stayed_local" in str(exc.value)


def test_credential_content_cannot_record_a_successful_non_local_call(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO model_usage (tenant_id,provider_id,sensitivity,"
            "outcome,locality) VALUES (%s,'ollama-local','credential','ok',"
            "'external')", (str(TENANT_A),),
        )
    assert "credentials_never_left_the_machine" in str(exc.value)


def test_a_refusal_cannot_be_billed(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO model_usage (tenant_id,provider_id,outcome,cost) "
            "VALUES (%s,'ollama-local','refused_quota',5)", (str(TENANT_A),),
        )
    assert "refusals_consume_nothing" in str(exc.value)


def test_a_prompt_cannot_be_hidden_in_the_model_column(seeded, db):
    """The usage table is the most-exported table in the system."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO model_usage (tenant_id,provider_id,model) "
            "VALUES (%s,'ollama-local',%s)",
            (str(TENANT_A), "my private health question " * 20),
        )


def test_no_phase_8_table_has_a_column_that_could_hold_a_prompt(db):
    """Checked against the live catalogue rather than the file, so a later
    migration adding a JSONB column here is caught too."""
    cur = db.cursor()
    cur.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_name IN ('model_providers','usage_quotas','model_usage',"
        "'admin_access_grants','admin_access_log') "
        "AND data_type IN ('jsonb','json','bytea','xml')"
    )
    assert cur.fetchall() == []


# ── Unknown retention stays unknown ──────────────────────────────────────────


def test_a_provider_may_leave_its_retention_unstated(catalogue_writer):
    """NULL is a real answer, not missing data. `NOT NULL DEFAULT FALSE` here
    would silently convert "we have not checked" into "it does not retain"."""
    cur = catalogue_writer.cursor()
    cur.execute(
        "INSERT INTO model_providers (provider_id,display_name,locality) "
        "VALUES ('rls-mystery','Mystery','external') "
        "ON CONFLICT (provider_id) DO NOTHING"
    )
    cur.execute("SELECT retains_prompts, trains_on_data FROM model_providers "
                "WHERE provider_id='rls-mystery'")
    assert cur.fetchone() == (None, None)
    cur.execute("DELETE FROM model_providers WHERE provider_id='rls-mystery'")


def test_a_stated_posture_must_cite_a_source(catalogue_writer):
    """A retention claim with no provenance is a rumour."""
    cur = catalogue_writer.cursor()
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO model_providers (provider_id,display_name,locality,"
            "retains_prompts,trains_on_data) VALUES ('rls-unsourced','U',"
            "'external',FALSE,FALSE)"
        )
    assert "stated_postures_cite_a_source" in str(exc.value)


def test_a_local_provider_cannot_claim_to_retain_elsewhere(catalogue_writer):
    cur = catalogue_writer.cursor()
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO model_providers (provider_id,display_name,locality,"
            "retains_prompts,trains_on_data,disclosure_source) "
            "VALUES ('rls-bad-local','B','local',TRUE,FALSE,'x')"
        )
    assert "local_providers_keep_nothing_elsewhere" in str(exc.value)


def test_the_runtime_role_cannot_write_the_catalogue(db):
    """P8-07. `api/routes/models.py` says there is no endpoint that changes a
    provider's disclosure, because "a retention claim is a fact about a third
    party's terms, not a user setting". That was true of the API and was the
    whole of the control — the database let `ira_app` write the table.

    Asserted for all three verbs. A revoke that removed only INSERT would leave
    a deployment able to edit an existing row, which is the case that matters:
    marking a retaining provider as clean does not need a new row.
    """
    cur = db.cursor()
    for sql in (
        "INSERT INTO model_providers (provider_id,display_name,locality) "
        "VALUES ('rls-forged','F','external')",
        "UPDATE model_providers SET retains_prompts = FALSE "
        "WHERE provider_id = 'ollama-local'",
        "DELETE FROM model_providers WHERE provider_id = 'ollama-local'",
    ):
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute(sql)
        assert "permission denied" in str(exc.value).lower(), (
            f"ira_app was allowed to run: {sql}")


def test_the_runtime_role_can_still_read_the_catalogue(db):
    """Revoking the read would take the Privacy Centre offline in order to
    protect it, which is a worse outcome than the defect."""
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM model_providers")
    assert cur.fetchone()[0] >= 1


def test_the_local_provider_is_seeded_and_matches_the_phase_1_disclosure():
    """A drift between the catalogue row and LOCAL_OLLAMA_DISCLOSURE would mean
    the routing policy and the database disagree about the local path."""
    from core.interfaces.model_provider import LOCAL_OLLAMA_DISCLOSURE

    assert LOCAL_OLLAMA_DISCLOSURE.provider_id == "ollama-local"
    assert LOCAL_OLLAMA_DISCLOSURE.retains_prompts is False
    assert LOCAL_OLLAMA_DISCLOSURE.trains_on_data is False


def test_the_seeded_local_row_agrees_with_the_python_disclosure(db):
    from core.interfaces.model_provider import LOCAL_OLLAMA_DISCLOSURE

    cur = db.cursor()
    cur.execute(
        "SELECT display_name, locality, retains_prompts, trains_on_data, region "
        "FROM model_providers WHERE provider_id = %s",
        (LOCAL_OLLAMA_DISCLOSURE.provider_id,),
    )
    row = cur.fetchone()
    assert row is not None, "the local provider is not seeded"
    name, locality, retains, trains, region = row
    assert name == LOCAL_OLLAMA_DISCLOSURE.display_name
    assert locality == LOCAL_OLLAMA_DISCLOSURE.locality.value
    assert retains is LOCAL_OLLAMA_DISCLOSURE.retains_prompts
    assert trains is LOCAL_OLLAMA_DISCLOSURE.trains_on_data
    assert region == LOCAL_OLLAMA_DISCLOSURE.region


# ── Break-glass: time-bound, user-authorised, logged ─────────────────────────


def test_a_grant_with_no_user_authorisation_cannot_exist(seeded, db):
    """There is deliberately no column for "authorised by an administrator"."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,expires_at) VALUES (%s,%s,'s@ex',%s,"
            "NOW()+interval '1 hour')",
            (str(TENANT_A), str(seeded["a"]["user_id"]), REASON),
        )
    assert "user_authorised_at" in str(exc.value)


def test_a_grant_window_longer_than_a_working_day_is_refused(seeded, db):
    """A window that long is not break-glass access, it is access."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,user_authorised_at,expires_at) "
            "VALUES (%s,%s,'s@ex',%s,NOW(),NOW()+interval '7 days')",
            (str(TENANT_A), str(seeded["a"]["user_id"]), REASON),
        )
    assert "grant_window_is_bounded" in str(exc.value)


def test_a_grant_needs_a_real_reason(seeded, db):
    """"support" tells a user nothing about why their conversations were read."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,user_authorised_at,expires_at) "
            "VALUES (%s,%s,'s@ex','support',NOW(),NOW()+interval '1 hour')",
            (str(TENANT_A), str(seeded["a"]["user_id"])),
        )


def test_authorisation_cannot_post_date_the_grant_it_authorises(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_grants (tenant_id,subject_user_id,"
            "admin_identity,reason,user_authorised_at,expires_at) "
            "VALUES (%s,%s,'s@ex',%s,NOW()+interval '2 hours',"
            "NOW()+interval '3 hours')",
            (str(TENANT_A), str(seeded["a"]["user_id"]), REASON),
        )
    assert "authorisation_precedes_the_grant" in str(exc.value)


def test_a_log_entry_with_no_grant_is_refused(seeded, db):
    """The property that makes the audit trustworthy: it cannot contain an entry
    that was not authorised."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_log (tenant_id,grant_id,admin_identity) "
            "VALUES (%s,%s,'s@ex')", (str(TENANT_A), str(uuid.uuid4())),
        )
    assert "does not exist" in str(exc.value) or "foreign key" in str(exc.value).lower()


def test_a_log_entry_outside_the_window_is_refused(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_log (tenant_id,grant_id,admin_identity,at) "
            "VALUES (%s,%s,%s,NOW()+interval '5 hours')",
            (str(TENANT_A), str(seeded["a"]["grant_id"]), seeded["a"]["admin"]),
        )
    assert "outside the authorised window" in str(exc.value)


def test_a_log_entry_for_a_different_admin_is_refused(seeded, db):
    """A grant authorises one named person, not the support team."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_log (tenant_id,grant_id,admin_identity) "
            "VALUES (%s,%s,'someone-else@example.com')",
            (str(TENANT_A), str(seeded["a"]["grant_id"])),
        )
    assert "authorises" in str(exc.value)


def test_the_access_log_cannot_be_edited_or_deleted(seeded, db):
    """An administrator who can edit the record of what they read is not
    audited."""
    cur = _as_tenant(db, TENANT_A)
    for statement in (
        "UPDATE admin_access_log SET what_was_read='nothing' WHERE id=%s",
        "DELETE FROM admin_access_log WHERE id=%s",
    ):
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute(statement, (str(seeded["a"]["log_id"]),))
        assert "append-only" in str(exc.value)


def test_a_grant_can_be_revoked_but_not_extended(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "UPDATE admin_access_grants SET expires_at=NOW()+interval '6 hours', "
            "revoked_at=NOW() WHERE id=%s", (str(seeded["a"]["grant_id"]),),
        )
    assert "may not alter what the grant authorised" in str(exc.value)

    cur.execute("UPDATE admin_access_grants SET revoked_at=NOW() WHERE id=%s",
                (str(seeded["a"]["grant_id"]),))
    assert cur.rowcount == 1


def test_a_revoked_grant_cannot_be_used_again(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    cur.execute("UPDATE admin_access_grants SET revoked_at=NOW() WHERE id=%s",
                (str(seeded["a"]["grant_id"]),))
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO admin_access_log (tenant_id,grant_id,admin_identity) "
            "VALUES (%s,%s,%s)",
            (str(TENANT_A), str(seeded["a"]["grant_id"]), seeded["a"]["admin"]),
        )
    assert "was revoked" in str(exc.value)


def test_a_grant_cannot_be_deleted(seeded, db):
    """Removing it would erase the basis for accesses already logged against it."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("DELETE FROM admin_access_grants WHERE id=%s",
                    (str(seeded["a"]["grant_id"]),))
    assert "append-only" in str(exc.value)


def test_the_user_can_see_who_read_their_data_and_why(seeded, db):
    """The surface that makes break-glass access accountable rather than merely
    bounded."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "SELECT l.admin_identity, l.record_count, g.reason, g.user_authorised_at "
        "FROM admin_access_log l JOIN admin_access_grants g ON g.id = l.grant_id "
        "WHERE l.tenant_id = %s AND g.subject_user_id = %s",
        (str(TENANT_A), str(seeded["a"]["user_id"])),
    )
    rows = cur.fetchall()
    assert rows, "the user cannot see that their data was read"
    admin, count, reason, authorised = rows[0]
    assert admin == seeded["a"]["admin"]
    assert count == 12
    assert reason == REASON
    assert authorised is not None
