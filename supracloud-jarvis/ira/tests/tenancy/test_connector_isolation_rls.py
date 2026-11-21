"""Phase 7 — ADVERSARIAL cross-tenant isolation for the connector tables.

These tables are the worst place in IRA for an isolation bug. `connector_accounts`
holds the credential that reads someone's mailbox; `action_receipts` records
which of their messages IRA opened. A cross-tenant read here is not a privacy
incident in the abstract — it is one user reading another's email.

Also asserted against a real database, because none of it can be asserted
anywhere else:

  * a plaintext credential cannot be stored, by four different routes;
  * an OAuth state can be consumed exactly once, enforced by trigger rather
    than only by the handler that checked it;
  * a receipt can be redacted but never edited or deleted;
  * disconnect cannot leave a revoked account still holding a token.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 028_connectors.sql.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database RLS tests skipped"
)

TENANT_A = uuid.UUID("f4f4f4f4-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("f5f5f5f5-0000-4000-8000-00000000000b")

SCOPED = ("connector_accounts", "connector_oauth_states", "action_receipts")

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


#: The member `seeded` created for each tenant, so `_as_tenant` can bind a user
#: without every call site having to pass one. Populated by the fixture and
#: cleared by it.
_SEEDED_MEMBER: dict[uuid.UUID, uuid.UUID] = {}


def _as_tenant(conn, tenant: uuid.UUID, user: uuid.UUID | None = None):
    """Bind a tenant, and the member acting within it.

    `060` made `connector_accounts` and `companion_preferences` user-scoped, so
    a tenant-only binding no longer reaches them: it matches zero rows on read
    and is refused outright on write. Every test here that touches a connected
    account therefore needs a user bound.

    Rather than thread one through twenty-odd call sites, the member `seeded`
    created for this tenant is the default — which is what the application does
    too, since a request is always somebody's. Passing `user` explicitly
    overrides it, and that is how the cross-user tests bind somebody else.
    """
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    bound = user if user is not None else _SEEDED_MEMBER.get(tenant)
    cur.execute("SELECT set_config('app.user_id', %s, false)",
                (str(bound) if bound else "",))
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    cur.execute("SELECT set_config('app.user_id', '', false)")
    return cur


@pytest.fixture
def seeded(db):
    """One connected account, one live OAuth state and one receipt per tenant."""
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        "INSERT INTO tenants (id, name, slug, edition) VALUES "
        "(%s,'Conn A',%s,'cloud'), (%s,'Conn B',%s,'cloud') "
        "ON CONFLICT (id) DO NOTHING",
        (str(TENANT_A), f"conn-a-{run}", str(TENANT_B), f"conn-b-{run}"),
    )

    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        user_id = uuid.uuid4()
        cur.execute("INSERT INTO users (id, external_subject) VALUES (%s,%s)",
                    (str(user_id), f"conn-{key}-{run}"))
        _SEEDED_MEMBER[tenant] = user_id
        cur = _as_tenant(db, tenant, user_id)
        cur.execute(
            "INSERT INTO connector_accounts (tenant_id,user_id,connector,"
            "provider_account,granted_scopes,credential_ciphertext,"
            "encryption_key_id,credential_nonce) "
            "VALUES (%s,%s,'gmail',%s,%s,%s,'k1',%s) RETURNING id",
            (str(tenant), str(user_id), f"{key}@example.com", [READONLY],
             psycopg2.Binary(b"\xde\xad\xbe\xef" + key.encode()),
             psycopg2.Binary(b"\x01" * 12)),
        )
        account_id = cur.fetchone()[0]

        state = f"state-{key}-{run}-{'x' * 30}"
        cur.execute(
            "INSERT INTO connector_oauth_states (state,tenant_id,user_id,"
            "connector,requested_scopes,verifier_ciphertext,encryption_key_id,"
            "verifier_nonce,code_challenge,expires_at) "
            "VALUES (%s,%s,%s,'gmail',%s,%s,'k1',%s,'chal',NOW()+interval '10 min')",
            (state, str(tenant), str(user_id), [READONLY],
             psycopg2.Binary(b"\xca\xfe"), psycopg2.Binary(b"\x02" * 12)),
        )

        cur.execute(
            "INSERT INTO action_receipts (tenant_id,user_id,connector,operation,"
            "purpose,data_accessed,influenced_by_external,influence_sources,"
            "injection_findings) VALUES (%s,%s,'gmail','read_selected',%s,%s,"
            "TRUE,%s,%s) RETURNING id",
            (str(tenant), str(user_id), f"read the message {key} selected",
             [f"1 message (gmail:message/{key}1)"],
             [f"gmail:message/{key}1"], ["ignore-previous-instructions"]),
        )
        ids[key] = {
            "tenant": tenant, "user_id": user_id, "account_id": account_id,
            "state": state, "receipt_id": cur.fetchone()[0],
        }
    _clear_tenant(db)
    yield ids


    cur = db.cursor()
    for entry in ids.values():
        cur.execute("SELECT set_config('app.tenant_id', %s, false)",
                    (str(entry["tenant"]),))
        cur.execute("DELETE FROM connector_oauth_states WHERE state = %s",
                    (entry["state"],))
        cur.execute("DELETE FROM connector_accounts WHERE id = %s",
                    (str(entry["account_id"]),))
        # Receipts are append-only; the trigger is dropped only by the rollback.
        # Redact instead so the fixture leaves no readable content behind.
        cur.execute(
            "UPDATE action_receipts SET redacted_at=NOW(), purpose='', "
            "data_accessed='{}', sent_externally='{}', influence_sources='{}' "
            "WHERE id = %s AND redacted_at IS NULL",
            (str(entry["receipt_id"]),),
        )
    _clear_tenant(db)


# ── The harness must be honest before it claims anything ─────────────────────


def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS entirely, making every test below vacuous."""
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False, (
        "IRA_TEST_DATABASE_DSN points at a SUPERUSER, so every isolation "
        "assertion here would pass while protecting nothing."
    )


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


def test_a_tenant_cannot_see_another_tenants_credential(seeded, db):
    """The worst possible cross-tenant read: the token that opens a mailbox."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM connector_accounts WHERE id = %s",
                (str(seeded["a"]["account_id"]),))
    assert cur.fetchone()[0] == 0

    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT count(*) FROM connector_accounts WHERE id = %s",
                (str(seeded["a"]["account_id"]),))
    assert cur.fetchone()[0] == 1, "tenant A cannot see its own account either"


def test_a_tenant_cannot_see_another_tenants_receipts(seeded, db):
    """Receipts name the messages IRA opened."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM action_receipts WHERE id = %s",
                (str(seeded["a"]["receipt_id"]),))
    assert cur.fetchone()[0] == 0


def test_a_tenant_cannot_see_another_tenants_oauth_state(seeded, db):
    """A stolen live state is a CSRF token. It must not even be readable."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM connector_oauth_states WHERE state = %s",
                (seeded["a"]["state"],))
    assert cur.fetchone()[0] == 0


def test_no_tenant_context_sees_nothing(seeded, db):
    cur = _clear_tenant(db)
    for table in SCOPED:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, f"{table} is readable with no tenant context"


def test_a_tenant_cannot_write_an_account_into_another_tenant(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO connector_accounts (tenant_id,user_id,connector) "
            "VALUES (%s,%s,'gmail')",
            (str(TENANT_A), str(seeded["a"]["user_id"])),
        )


def test_a_tenant_cannot_consume_another_tenants_oauth_state(seeded, db):
    """The cross-tenant half of the replay defence."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("UPDATE connector_oauth_states SET consumed_at=NOW() WHERE state=%s",
                (seeded["a"]["state"],))
    assert cur.rowcount == 0, "a cross-tenant state was consumed"


def test_a_tenant_cannot_redact_another_tenants_receipt(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    cur.execute(
        "UPDATE action_receipts SET redacted_at=NOW(), purpose='', "
        "data_accessed='{}', sent_externally='{}', influence_sources='{}' "
        "WHERE id=%s", (str(seeded["a"]["receipt_id"]),),
    )
    assert cur.rowcount == 0


# ── A plaintext credential cannot be stored ──────────────────────────────────


@pytest.mark.parametrize("blob,label", [
    (b"1//0eXaMpLeReFrEsHtOkEn", "a Google refresh token"),
    (b"ya29.a0AfB_byC-example", "a Google access token"),
    (b"eyJhbGciOiJIUzI1NiJ9.eyJ", "a raw JWT"),
    (b"ghp_examplegithubtoken00", "a GitHub token"),
])
def test_an_obviously_plaintext_credential_is_refused(seeded, db, blob, label):
    """A smoke alarm, not a lock. Real ciphertext will not match by accident; a
    developer who assigned the raw token gets an immediate, specific error
    instead of a working INSERT."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO connector_accounts (tenant_id,user_id,connector,"
            "provider_account,credential_ciphertext,encryption_key_id,"
            "credential_nonce) VALUES (%s,%s,'gmail','plain@example.com',%s,'k1',%s)",
            (str(TENANT_A), str(seeded["a"]["user_id"]),
             psycopg2.Binary(blob), psycopg2.Binary(b"\x01" * 12)),
        )
    assert "credential_is_not_obvious_plaintext" in str(exc.value), label


def test_a_credential_without_a_key_id_cannot_exist(seeded, db):
    """A row that has a credential but no key it was encrypted under is a row
    nobody can decrypt and nobody can account for."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO connector_accounts (tenant_id,user_id,connector,"
            "provider_account,credential_ciphertext) "
            "VALUES (%s,%s,'gmail','nokey@example.com',%s)",
            (str(TENANT_A), str(seeded["a"]["user_id"]),
             psycopg2.Binary(b"\xde\xad\xbe\xef")),
        )
    assert "credentials_are_encrypted" in str(exc.value)


def test_a_revoked_account_cannot_still_hold_a_credential(seeded, db):
    """"Disconnect" that leaves the token behind is not a disconnect."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "UPDATE connector_accounts SET revoked_at=NOW() WHERE id=%s",
            (str(seeded["a"]["account_id"]),),
        )
    assert "revoked_accounts_hold_no_credential" in str(exc.value)


def test_disconnect_clears_the_credential_and_marks_revoked(seeded, db):
    """The shape `store.disconnect` uses. The constraint above means it cannot
    half-succeed into "looks disconnected, token still there"."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE connector_accounts SET credential_ciphertext=NULL, "
        "encryption_key_id=NULL, credential_nonce=NULL, status='revoked', "
        "revoked_at=NOW() WHERE id=%s",
        (str(seeded["a"]["account_id"]),),
    )
    assert cur.rowcount == 1
    cur.execute(
        "SELECT credential_ciphertext, status FROM connector_accounts WHERE id=%s",
        (str(seeded["a"]["account_id"]),),
    )
    blob, status = cur.fetchone()
    assert blob is None and status == "revoked"


def test_the_default_access_level_is_not_the_top_of_the_ladder(seeded, db):
    """Connecting an account grants IRA the ability to look, not to act."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT access_level FROM connector_accounts WHERE id=%s",
                (str(seeded["a"]["account_id"]),))
    assert cur.fetchone()[0] == "observe"


# ── OAuth state is single-use ────────────────────────────────────────────────


def test_a_state_can_be_consumed_once(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    cur.execute("UPDATE connector_oauth_states SET consumed_at=NOW() WHERE state=%s",
                (seeded["a"]["state"],))
    assert cur.rowcount == 1


def test_a_replayed_state_is_refused_by_the_database(seeded, db):
    """The handler checks this too. The trigger is what makes a replay fail even
    if the handler's check were wrong or raced — two callbacks arriving together
    is the ordinary case for a double-clicked consent screen."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("UPDATE connector_oauth_states SET consumed_at=NOW() WHERE state=%s",
                (seeded["a"]["state"],))
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE connector_oauth_states SET consumed_at=NOW() "
                    "WHERE state=%s", (seeded["a"]["state"],))
    assert "already been consumed" in str(exc.value)


def test_a_states_binding_cannot_be_changed(seeded, db):
    """Rebinding would let a callback redirect a flow to another user."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "UPDATE connector_oauth_states SET requested_scopes=%s, "
            "consumed_at=NOW() WHERE state=%s",
            (["https://mail.google.com/"], seeded["a"]["state"]),
        )
    assert "immutable" in str(exc.value)


def test_a_state_must_carry_an_expiry_in_the_future(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "INSERT INTO connector_oauth_states (state,tenant_id,user_id,"
            "connector,verifier_ciphertext,encryption_key_id,verifier_nonce,"
            "code_challenge,expires_at) VALUES (%s,%s,%s,'gmail',%s,'k1',%s,'c',"
            "NOW()-interval '1 min')",
            ("y" * 40, str(TENANT_A), str(seeded["a"]["user_id"]),
             psycopg2.Binary(b"\xca\xfe"), psycopg2.Binary(b"\x03" * 12)),
        )
    assert "oauth_state_expires_after_creation" in str(exc.value)


def test_only_s256_pkce_is_storable(seeded, db):
    """`plain` offers nothing here, and the CHECK means a downgrade is not
    expressible rather than merely discouraged."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO connector_oauth_states (state,tenant_id,user_id,"
            "connector,verifier_ciphertext,encryption_key_id,verifier_nonce,"
            "code_challenge,code_challenge_method,expires_at) "
            "VALUES (%s,%s,%s,'gmail',%s,'k1',%s,'c','plain',NOW()+interval '5 min')",
            ("z" * 40, str(TENANT_A), str(seeded["a"]["user_id"]),
             psycopg2.Binary(b"\xca\xfe"), psycopg2.Binary(b"\x04" * 12)),
        )


def test_a_free_form_redirect_target_cannot_be_stored(seeded, db):
    """An attacker-supplied redirect target is an open redirect, and this value
    is reachable from a callback."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO connector_oauth_states (state,tenant_id,user_id,"
            "connector,verifier_ciphertext,encryption_key_id,verifier_nonce,"
            "code_challenge,return_to,expires_at) "
            "VALUES (%s,%s,%s,'gmail',%s,'k1',%s,'c',"
            "'https://attacker.example',NOW()+interval '5 min')",
            ("w" * 40, str(TENANT_A), str(seeded["a"]["user_id"]),
             psycopg2.Binary(b"\xca\xfe"), psycopg2.Binary(b"\x05" * 12)),
        )


# ── Receipts are evidence ────────────────────────────────────────────────────


def test_a_receipt_cannot_be_deleted(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("DELETE FROM action_receipts WHERE id=%s",
                    (str(seeded["a"]["receipt_id"]),))
    assert "append-only" in str(exc.value)


def test_a_receipt_cannot_be_edited(seeded, db):
    """An actor that can rewrite the record of what it did is not audited."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE action_receipts SET purpose='something innocent' "
                    "WHERE id=%s", (str(seeded["a"]["receipt_id"]),))
    assert "only permitted UPDATE is a redaction" in str(exc.value)


def test_a_redaction_that_keeps_the_content_is_refused(seeded, db):
    """Stamping the column while leaving the content produces a row that claims
    to be redacted and is not — the worst possible outcome for an erasure
    report."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE action_receipts SET redacted_at=NOW() WHERE id=%s",
                    (str(seeded["a"]["receipt_id"]),))
    assert "must clear purpose" in str(exc.value)


def test_a_redaction_may_not_change_what_happened(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            "UPDATE action_receipts SET redacted_at=NOW(), purpose='', "
            "data_accessed='{}', sent_externally='{}', influence_sources='{}', "
            "operation='send' WHERE id=%s",
            (str(seeded["a"]["receipt_id"]),),
        )
    assert "may not change what happened" in str(exc.value)


def test_a_proper_redaction_keeps_the_facts_and_drops_the_content(seeded, db):
    """What survives is deliberate: that a gmail read happened, when, and
    whether it was approved. What goes is what it was about."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "UPDATE action_receipts SET redacted_at=NOW(), purpose='', "
        "data_accessed='{}', sent_externally='{}', influence_sources='{}' "
        "WHERE id=%s", (str(seeded["a"]["receipt_id"]),),
    )
    assert cur.rowcount == 1
    cur.execute(
        "SELECT connector, operation, purpose, data_accessed, "
        "influenced_by_external, redacted_at FROM action_receipts WHERE id=%s",
        (str(seeded["a"]["receipt_id"]),),
    )
    connector, operation, purpose, accessed, tainted, redacted = cur.fetchone()
    assert (connector, operation) == ("gmail", "read_selected")
    assert purpose == "" and accessed == []
    assert tainted is True, "that external content was involved is a fact worth keeping"
    assert redacted is not None


def test_a_receipt_cannot_be_redacted_twice(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    blank = ("UPDATE action_receipts SET redacted_at=NOW(), purpose='', "
             "data_accessed='{}', sent_externally='{}', influence_sources='{}' "
             "WHERE id=%s")
    cur.execute(blank, (str(seeded["a"]["receipt_id"]),))
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(blank, (str(seeded["a"]["receipt_id"]),))
    assert "already been redacted" in str(exc.value)


# ── The scope catalogue ──────────────────────────────────────────────────────


def test_the_scope_catalogue_is_readable_by_every_tenant(seeded, db):
    """Global reference data, deliberately without RLS: a fixed product
    vocabulary identical for every tenant, holding nothing about anybody."""
    for tenant in (TENANT_A, TENANT_B):
        cur = _as_tenant(db, tenant)
        cur.execute("SELECT count(*) FROM connector_scopes")
        assert cur.fetchone()[0] >= 5


def test_every_offered_scope_can_be_explained_to_a_user(db):
    """A scope nobody can explain is a scope nobody should be asked to grant."""
    cur = db.cursor()
    cur.execute("SELECT connector, scope, human_summary FROM connector_scopes")
    for connector, scope, summary in cur.fetchall():
        assert summary and summary.strip(), f"{connector} {scope} has no summary"
        assert len(summary) > 30, f"{connector} {scope} has a uselessly short summary"


def test_no_write_scope_is_offered_to_cloud_in_this_phase(db):
    """The edition gate (ADR-0001). A scope granting write access to a mailbox is
    not offered to consumer cloud in Phase 7."""
    cur = db.cursor()
    cur.execute(
        "SELECT connector, scope FROM connector_scopes "
        "WHERE access_kind = 'write' AND offered_in_cloud IS TRUE"
    )
    assert cur.fetchall() == []


def test_no_offered_scope_grants_sending(db):
    """The scope Phase 7 must not hold, checked against the catalogue the consent
    screen reads from rather than only against the Python refusal list."""
    cur = db.cursor()
    cur.execute("SELECT scope FROM connector_scopes")
    for (scope,) in cur.fetchall():
        assert "gmail.send" not in scope
        assert scope != "https://mail.google.com/"
        assert "gmail.modify" not in scope
