"""Phase 3B Batch 0/1 — the substrate, and the three security-critical tables.

The test that matters most is `test_two_tenants_can_each_have_a_user_called_alice`
and its TOTP counterpart. Before this migration, `totp_secrets` had
`PRIMARY KEY (username)`: two tenants each onboarding an `alice` shared one row,
so tenant B's alice could enrol over — or authenticate against — tenant A's
alice's second factor. That is an authentication bypass, and it is why these
tables went first.

Everything here runs against a real PostgreSQL under a **non-superuser** role.
A superuser bypasses RLS entirely and a role with `BYPASSRLS` does the same;
either would make every isolation assertion below pass while proving nothing,
so the suite asserts neither is true before making a claim.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — real-database Batch 0/1 tests skipped",
                allow_module_level=True)

NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")
PRIVATE_TENANT = uuid.UUID("e91aceeb-89fb-5ad2-b43c-4eee2bad079c")
PRIVATE_ORG = uuid.UUID("53925952-13ec-585d-9ab0-7016b647d6e8")
PRIVATE_WORKSPACE = uuid.UUID("38cdd893-d172-5afd-bd08-005902c30089")


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def admin_db():
    """A privileged connection, for arranging fixtures RLS would otherwise hide."""
    dsn = os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not dsn:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def set_context(cur, *, tenant=None, user=None, workspace=None):
    """Bind transaction-local context exactly as core/identity/db_context.py does."""
    cur.execute("SELECT set_config('app.tenant_id',    %s, true)", (str(tenant) if tenant else "",))
    cur.execute("SELECT set_config('app.user_id',      %s, true)", (str(user) if user else "",))
    cur.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace) if workspace else "",))


# ── The role itself ──────────────────────────────────────────────────────────

def test_application_role_is_not_a_superuser(db):
    with db.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0] is False, \
            "a superuser bypasses RLS entirely; every assertion below would be vacuous"


def test_application_role_does_not_hold_bypassrls(db):
    """Distinct from superuser, and equally fatal to every isolation claim."""
    with db.cursor() as cur:
        cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0] is False


# ── Batch 0A — substrate ─────────────────────────────────────────────────────

@pytest.mark.parametrize("table", ["organisations", "workspaces", "workspace_memberships",
                                   "tenant_mfa_policy", "legacy_identity_map"])
def test_new_tables_exist(db, table):
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        assert cur.fetchone()[0] is not None, f"{table} was not created"


@pytest.mark.parametrize("table", [
    # `tenants` is deliberately absent: Phase 3A classifies it PLATFORM_GLOBAL,
    # protected by grants rather than RLS. See 033's header and R-46.
    "organisations", "workspaces", "workspace_memberships",
    "totp_secrets", "voice_profiles", "biometric_audit", "delegated_users",
    "tenant_mfa_policy",
])
def test_rls_is_enabled_and_forced(db, table):
    """FORCE matters as much as ENABLE: without it the table owner is exempt."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s", (table,))
        enabled, forced = cur.fetchone()
        assert enabled, f"{table} does not have RLS enabled"
        assert forced, f"{table} does not FORCE RLS — the table owner would bypass it"


def test_exactly_one_default_organisation_and_workspace(admin_db):
    """The migration is re-applied by CI and by the boot-time runner. Creating a
    second default on a re-run is the failure that only appears in production."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM organisations WHERE tenant_id = %s", (str(PRIVATE_TENANT),))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM workspaces WHERE tenant_id = %s AND is_default",
                    (str(PRIVATE_TENANT),))
        assert cur.fetchone()[0] == 1


def test_one_organisation_per_tenant_is_enforced(admin_db):
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO organisations (tenant_id, name) VALUES (%s, 'Second')",
                (str(PRIVATE_TENANT),))


def test_only_one_active_default_workspace_per_tenant(admin_db):
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO workspaces (tenant_id, organisation_id, name, slug, is_default) "
                "VALUES (%s, %s, 'Another', 'another', TRUE)",
                (str(PRIVATE_TENANT), str(PRIVATE_ORG)))


def test_a_workspace_cannot_belong_to_another_tenants_organisation(admin_db):
    """Enforced by a composite FK, so a cross-tenant workspace is impossible to
    write rather than something a later query has to filter out."""
    other = uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name, edition, status) "
                    "VALUES (%s, %s, 'cloud', 'active')", (str(other), f"T-{other.hex[:8]}"))
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO workspaces (tenant_id, organisation_id, name, slug) "
                "VALUES (%s, %s, 'Sneaky', 'sneaky')", (str(other), str(PRIVATE_ORG)))


def test_two_tenants_may_use_the_same_workspace_slug(admin_db):
    """Global uniqueness on a tenant-created name is a denial-of-service by
    naming: the first tenant to use `main` takes it from everyone."""
    other_tenant, other_org = uuid.uuid4(), uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name, edition, status) "
                    "VALUES (%s, %s, 'cloud', 'active')", (str(other_tenant), f"T-{other_tenant.hex[:8]}"))
        cur.execute("INSERT INTO organisations (id, tenant_id, name) VALUES (%s, %s, 'Other')",
                    (str(other_org), str(other_tenant)))
        # 'default' already exists for the private tenant.
        cur.execute(
            "INSERT INTO workspaces (tenant_id, organisation_id, name, slug) "
            "VALUES (%s, %s, 'Default', 'default')", (str(other_tenant), str(other_org)))
        cur.execute("SELECT count(*) FROM workspaces WHERE slug = 'default'")
        assert cur.fetchone()[0] >= 2


# ── Batch 0A — cross-tenant isolation ────────────────────────────────────────

@pytest.fixture
def two_tenants(admin_db):
    """Two fully-formed tenants, created privileged so RLS does not hide setup."""
    a, b = uuid.uuid4(), uuid.uuid4()
    made = {}
    with admin_db.cursor() as cur:
        for label, tid in (("a", a), ("b", b)):
            oid, wid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name, edition, status) "
                        "VALUES (%s, %s, 'cloud', 'active')", (str(tid), f"T-{tid.hex[:8]}"))
            cur.execute("INSERT INTO organisations (id, tenant_id, name) VALUES (%s, %s, %s)",
                        (str(oid), str(tid), f"Org-{label}"))
            cur.execute("INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default) "
                        "VALUES (%s, %s, %s, 'Default', 'default', TRUE)",
                        (str(wid), str(tid), str(oid)))
            # Same display name in both tenants — the collision this phase fixes.
            cur.execute("INSERT INTO users (id, display_name, auth_provider, status) "
                        "VALUES (%s, 'alice', 'local', 'active')", (str(uid),))
            cur.execute("INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at) "
                        "VALUES (%s, %s, 'member', 'active', NOW())", (str(tid), str(uid)))
            made[label] = {"tenant": tid, "org": oid, "workspace": wid, "user": uid}
    admin_db.commit()
    yield made
    # A test that raised leaves the connection in an aborted transaction, so the
    # rollback comes first — otherwise every cleanup statement fails and the
    # failure is reported as a teardown error that hides the real one.
    admin_db.rollback()
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            cur.execute("DELETE FROM voice_profiles WHERE user_id = %s", (str(made[label]["user"]),))
            cur.execute("DELETE FROM totp_secrets   WHERE user_id = %s", (str(made[label]["user"]),))
            cur.execute("UPDATE biometric_audit SET user_id = NULL WHERE user_id = %s",
                        (str(made[label]["user"]),))
            cur.execute("DELETE FROM tenants WHERE id = %s", (str(made[label]["tenant"]),))
            cur.execute("DELETE FROM users   WHERE id = %s", (str(made[label]["user"]),))
    admin_db.commit()


def test_tenant_a_cannot_read_tenant_b_organisation(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM organisations WHERE id = %s",
                    (str(two_tenants["b"]["org"]),))
        assert cur.fetchone()[0] == 0


def test_tenant_a_cannot_read_tenant_b_workspace(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM workspaces WHERE id = %s",
                    (str(two_tenants["b"]["workspace"]),))
        assert cur.fetchone()[0] == 0


def test_tenant_a_cannot_read_tenant_b_membership(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM tenant_memberships WHERE user_id = %s",
                    (str(two_tenants["b"]["user"]),))
        assert cur.fetchone()[0] == 0


def test_the_tenants_registry_is_still_enumerable_and_that_is_recorded(db, two_tenants):
    """R-46, asserted rather than assumed.

    `tenants` carries no RLS — Phase 3A classifies it PLATFORM_GLOBAL, protected
    by grants — so the application role can still read every customer's name.
    That is a real gap and it is NOT closed by Batch 0. This test exists so the
    gap is visible in the suite instead of being an unstated assumption, and so
    that closing it (a membership-filtered view) is a deliberate change with a
    failing test to update.
    """
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM tenants WHERE id = %s",
                    (str(two_tenants["b"]["tenant"]),))
        assert cur.fetchone()[0] == 1, (
            "if this now returns 0, registry enumeration has been closed — "
            "update R-46 and this test together")


def test_missing_tenant_context_returns_nothing(db, two_tenants):
    """Fail-closed by construction: unset GUC -> NULL -> no rows."""
    with db.cursor() as cur:
        set_context(cur)
        for table in ("organisations", "workspaces"):
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0, f"{table} leaked rows with no tenant context"


def test_context_does_not_survive_the_transaction(db, two_tenants):
    """`SET LOCAL`/`set_config(..., true)` is discarded at COMMIT/ROLLBACK, so a
    pooled connection carries no tenant to its next borrower."""
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM organisations")
        assert cur.fetchone()[0] == 1
    db.rollback()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM organisations")
        assert cur.fetchone()[0] == 0, "tenant context survived the transaction"


# ── Batch 0B — identity ──────────────────────────────────────────────────────

def test_the_identity_map_is_unreadable_by_the_application_role(db, admin_db):
    """`legacy_identity_map` is a directory of who exists. No API exposes it and
    no authorisation decision reads it, so the application role must not reach
    it — even though the standard provisioning step grants it everything.

    034 first expressed this as ``REVOKE ALL ... FROM PUBLIC``. That is not
    durable: every documented setup for this schema ends with

        GRANT ALL ON ALL TABLES IN SCHEMA public TO ira_app;

    which runs after the migrations and hands the grant straight back. The
    assertion below therefore checks the OUTCOME under exactly that condition —
    the grant is confirmed present first, so a future change that drops the
    grant cannot make this test pass for the wrong reason.
    """
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE table_name = 'legacy_identity_map' AND grantee = 'ira_app'")
        assert cur.fetchone()[0] > 0, (
            "the harness no longer grants ira_app access to legacy_identity_map, "
            "so this test would pass without RLS doing anything")

        cur.execute("SELECT count(*) FROM legacy_identity_map")
        seeded = cur.fetchone()[0]
    assert seeded > 0, "no mappings seeded — the read below would be vacuous"

    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM legacy_identity_map")
        assert cur.fetchone()[0] == 0, (
            "the application role can read the legacy identity map. RLS is "
            "enabled with no policy precisely so that grants cannot reopen it.")


def test_the_identity_map_has_rls_on_and_no_policy(admin_db):
    """The mechanism is the ABSENCE of a policy, which is easy to undo by
    accident: adding any permissive policy to this table opens it to whoever
    that policy admits. Asserting the absence makes the deletion visible."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'legacy_identity_map'")
        assert cur.fetchone()[0] is True, "RLS is not enabled on legacy_identity_map"

        cur.execute("SELECT policyname FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = 'legacy_identity_map'")
        policies = [r[0] for r in cur.fetchall()]
        assert policies == [], (
            f"legacy_identity_map has policies {policies}. Deny-all depends on "
            "there being none; any permissive policy reopens the table.")


def test_the_migrations_can_still_read_the_identity_map(admin_db):
    """FORCE is deliberately not set, because 035/036/037 backfill ownership by
    reading this table as the owner. With FORCE they would resolve zero rows and
    every `user_id` would silently stay NULL — the total failure of the thing
    the table exists for, with no error to notice."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'legacy_identity_map'")
        assert cur.fetchone()[0] is False, (
            "FORCE ROW LEVEL SECURITY on legacy_identity_map would hide it from "
            "the migration runner and break every ownership backfill")


def test_users_has_no_tenant_column(admin_db):
    """IDENTITY_GLOBAL (CLOUD-ADR-0006): one person, many tenants."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'tenant_id'")
        assert cur.fetchone()[0] == 0


def test_the_owner_maps_to_the_uuid_principal_py_derives(admin_db):
    """SQL and Python must agree, or every backfill lands on a different id."""
    expected = uuid.uuid5(NAMESPACE, "user:private:admin")
    with admin_db.cursor() as cur:
        cur.execute("SELECT user_id FROM legacy_identity_map WHERE legacy_username = 'admin'")
        row = cur.fetchone()
        assert row is not None, "the seeded owner was not mapped"
        assert str(row[0]) == str(expected)


def test_a_mapping_marked_resolved_must_carry_a_user(admin_db):
    """Makes "MAPPED with no user" unrepresentable rather than something every
    downstream query has to remember to check."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO legacy_identity_map (legacy_username, user_id, source_table, resolution) "
                "VALUES ('ghost', NULL, 'test', 'MAPPED')")


@pytest.mark.parametrize("raw,expected", [
    ("  User@Example.COM  ", "User@example.com"),   # domain lowered, local preserved
    ("a.b@gmail.com",        "a.b@gmail.com"),      # dots kept — Gmail-only behaviour
    ("user+tag@example.com", "user+tag@example.com"),  # plus-tag kept
    ("MixedCase@DOMAIN.org", "MixedCase@domain.org"),
])
def test_email_normalisation_is_conservative(admin_db, raw, expected):
    """Aggressive normalisation merges distinct mailboxes; the failure mode is
    one person receiving another's password reset."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT normalise_email(%s)", (raw,))
        assert cur.fetchone()[0] == expected


def test_only_a_verified_email_is_globally_unique(admin_db):
    """An unverified claim on someone else's address must not block or seize it."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO users (id, email, display_name, auth_provider) "
                    "VALUES (%s, 'dup@example.com', 'A', 'local')", (str(a),))
        # Second unverified row with the same address: allowed.
        cur.execute("INSERT INTO users (id, email, display_name, auth_provider) "
                    "VALUES (%s, 'dup@example.com', 'B', 'local')", (str(b),))
        cur.execute("UPDATE users SET email_verified_at = NOW() WHERE id = %s", (str(a),))
        # Verifying the second is refused — that is where uniqueness bites.
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("UPDATE users SET email_verified_at = NOW() WHERE id = %s", (str(b),))


# ── Batch 0C — memberships ───────────────────────────────────────────────────

def test_membership_access_is_a_generated_column(admin_db):
    """One place answers "does this grant access?", so no call site can forget
    half of the condition."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT is_generated FROM information_schema.columns "
                    "WHERE table_name = 'tenant_memberships' "
                    "AND column_name = 'membership_grants_access'")
        assert cur.fetchone()[0] == "ALWAYS"


@pytest.mark.parametrize("status,expected", [
    ("active", True), ("invited", False), ("suspended", False),
])
def test_only_an_active_membership_grants_access(admin_db, two_tenants, status, expected):
    with admin_db.cursor() as cur:
        cur.execute("UPDATE tenant_memberships SET status = %s WHERE user_id = %s",
                    (status, str(two_tenants["a"]["user"])))
        cur.execute("SELECT membership_grants_access FROM tenant_memberships WHERE user_id = %s",
                    (str(two_tenants["a"]["user"]),))
        assert cur.fetchone()[0] is expected


def test_a_suspended_member_is_distinguishable_from_a_removed_one(admin_db, two_tenants):
    """`revoked_at IS NULL` cannot express suspension; the two have different
    reinstatement semantics."""
    with admin_db.cursor() as cur:
        cur.execute("UPDATE tenant_memberships SET status = 'suspended', suspended_at = NOW() "
                    "WHERE user_id = %s", (str(two_tenants["a"]["user"]),))
        cur.execute("SELECT status, revoked_at FROM tenant_memberships WHERE user_id = %s",
                    (str(two_tenants["a"]["user"]),))
        status, revoked = cur.fetchone()
        assert status == "suspended" and revoked is None


def test_an_unrecognised_role_is_refused(admin_db, two_tenants):
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE tenant_memberships SET role = 'superadmin' WHERE user_id = %s",
                        (str(two_tenants["a"]["user"]),))


def test_status_and_revoked_at_cannot_disagree(admin_db, two_tenants):
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE tenant_memberships SET status = 'removed' WHERE user_id = %s",
                        (str(two_tenants["a"]["user"]),))


def test_delegated_users_is_retained_not_deleted(admin_db):
    """Legacy compatibility (CLOUD-ADR-0006), migrated in Batch 10 — not now."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT to_regclass('delegated_users')")
        assert cur.fetchone()[0] is not None


# ── Batch 1A — TOTP: the collision this phase exists to close ────────────────

def test_totp_secrets_is_keyed_on_an_immutable_user_id(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'totp_secrets' AND column_name = 'user_id'")
        assert cur.fetchone()[0] == "uuid"


def test_two_tenants_can_each_have_a_user_called_alice_with_their_own_totp(admin_db, two_tenants):
    """THE regression test for R-26.

    Before this migration `totp_secrets` had `PRIMARY KEY (username)`: one row
    for `alice`, shared. Tenant B's alice could enrol over, or authenticate
    against, tenant A's alice's second factor.
    """
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            # The SAME username in both tenants. Using different names here
            # would pass without proving anything — the collision is the
            # shared string, and that is exactly what has to be exercised.
            cur.execute(
                "INSERT INTO totp_secrets (username, secret, user_id, enabled, tenant_id) "
                "VALUES (%s, %s, %s, TRUE, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')",
                ("alice", f"placeholder-not-a-real-secret-{label}",
                 str(two_tenants[label]["user"])))
        cur.execute("SELECT count(DISTINCT user_id) FROM totp_secrets WHERE user_id IN (%s, %s)",
                    (str(two_tenants["a"]["user"]), str(two_tenants["b"]["user"])))
        assert cur.fetchone()[0] == 2, "the two alices must have separate secrets"


def test_a_user_may_hold_only_one_active_totp_enrolment(admin_db, two_tenants):
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) VALUES ('x1', 'p1', %s, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')", (uid,))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) VALUES ('x2', 'p2', %s, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')", (uid,))


def test_the_username_primary_key_is_retired(admin_db):
    """Adding user_id is not enough on its own: while PRIMARY KEY (username)
    stood, the second tenant's `alice` was refused by a GLOBAL key on a display
    string. This is the step that actually closes R-26."""
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'totp_secrets' AND c.contype = 'p'")
        assert cur.fetchone()[0] == "PRIMARY KEY (id)"


def test_voice_profiles_no_longer_globally_unique_on_owner_name(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE tablename = 'voice_profiles' "
                    "AND indexname = 'voice_profiles_owner_name_key'")
        assert cur.fetchone()[0] == 0


def test_a_tenant_admin_cannot_read_another_users_totp_secret(db, admin_db, two_tenants):
    """The policy is on `app.user_id`, not `app.tenant_id`. A tenant-only
    predicate would let any admin read every member's second factor — the same
    shape of bug, one level up."""
    a_user = two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        # 052: the row's tenant must be the tenant this test later binds, or the
        # positive control at the end reads nothing and the test passes for the
        # wrong reason. Seeded on admin_db, which bypasses RLS.
        cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) VALUES ('a-alice', 'p', %s, %s)",
                    (str(a_user), str(two_tenants["a"]["tenant"])))
    admin_db.commit()
    try:
        with db.cursor() as cur:
            # An admin of the SAME tenant, but a different user.
            set_context(cur, tenant=two_tenants["a"]["tenant"], user=uuid.uuid4())
            cur.execute("SELECT count(*) FROM totp_secrets")
            assert cur.fetchone()[0] == 0, "a tenant admin read another user's TOTP secret"
            # The owner sees their own.
            set_context(cur, tenant=two_tenants["a"]["tenant"], user=a_user)
            cur.execute("SELECT count(*) FROM totp_secrets")
            assert cur.fetchone()[0] == 1
    finally:
        with admin_db.cursor() as cur:
            cur.execute("DELETE FROM totp_secrets WHERE user_id = %s", (str(a_user),))
        admin_db.commit()


def test_an_unresolved_totp_row_is_visible_to_nobody(db, admin_db):
    """`NULL = <uuid>` is NULL, not true — so a row whose owner did not resolve
    is inert rather than public. The fail-closed direction."""
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) VALUES ('orphan', 'p', NULL, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')")
    admin_db.commit()
    try:
        with db.cursor() as cur:
            set_context(cur, tenant=PRIVATE_TENANT, user=uuid.uuid4())
            cur.execute("SELECT count(*) FROM totp_secrets WHERE username = 'orphan'")
            assert cur.fetchone()[0] == 0
    finally:
        with admin_db.cursor() as cur:
            cur.execute("DELETE FROM totp_secrets WHERE username = 'orphan'")
        admin_db.commit()


def test_encrypted_status_requires_all_three_encryption_fields(admin_db, two_tenants):
    """Otherwise "encrypted" is a label a bug can set."""
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO totp_secrets (username, secret, user_id, migration_status, tenant_id) "
                "VALUES ('claims', 'p', %s, 'ENCRYPTED', 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')", (uid,))


def test_a_new_row_starts_as_plaintext_and_says_so(admin_db, two_tenants):
    """The migration does not encrypt. `migration_status` tells the truth about
    that rather than implying otherwise."""
    uid = str(two_tenants["b"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) VALUES ('fresh', 'p', %s, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')", (uid,))
        cur.execute("SELECT migration_status FROM totp_secrets WHERE username = 'fresh'")
        assert cur.fetchone()[0] == "PLAINTEXT"


def test_tenant_mfa_policy_is_separate_from_the_secret(admin_db):
    """A tenant may require MFA. It must never be able to read the secrets."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT to_regclass('tenant_mfa_policy')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'tenant_mfa_policy' "
                    "AND column_name IN ('secret', 'secret_ciphertext')")
        assert cur.fetchone()[0] == 0


def test_the_totp_status_view_exposes_no_secret(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT * FROM totp_migration_status")
        columns = [d[0] for d in cur.description]
    for forbidden in ("secret", "secret_ciphertext", "username"):
        assert forbidden not in columns


# ── Batch 1B — voice profiles ────────────────────────────────────────────────

def test_voice_profile_is_owned_by_an_immutable_user_id(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'voice_profiles' AND column_name = 'user_id'")
        assert cur.fetchone()[0] == "uuid"


def test_two_users_named_alike_do_not_share_a_voiceprint(admin_db, two_tenants):
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            # Same display name, different users — the collision UNIQUE
            # (owner_name) used to make impossible.
            cur.execute(
                "INSERT INTO voice_profiles (owner_name, embedding, user_id, consent_granted_at) "
                "VALUES ('alice', %s, %s, NOW())",
                (f"placeholder-template-{label}", str(two_tenants[label]["user"])))
        cur.execute("SELECT count(DISTINCT user_id) FROM voice_profiles WHERE user_id IN (%s, %s)",
                    (str(two_tenants["a"]["user"]), str(two_tenants["b"]["user"])))
        assert cur.fetchone()[0] == 2


def test_re_enrolment_is_versioned_not_an_overwrite(admin_db, two_tenants):
    """Overwriting destroys the previous template, so a compromised enrolment
    cannot be revoked — there is nothing left to revoke."""
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id, enrollment_version) "
                    "VALUES ('v', 'p1', %s, 1)", (uid,))
        # Revoking first is required by the one-active-profile index — which is
        # the intended workflow: you cannot hold two live enrolments.
        cur.execute("UPDATE voice_profiles SET revoked_at = NOW(), revocation_reason = 're-enrolled' "
                    "WHERE user_id = %s AND enrollment_version = 1", (uid,))
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id, enrollment_version) "
                    "VALUES ('v', 'p2', %s, 2)", (uid,))
        cur.execute("SELECT count(*) FROM voice_profiles WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] == 2, "the previous enrolment must survive revocation"


def test_only_one_active_voice_profile_per_user(admin_db, two_tenants):
    uid = str(two_tenants["b"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id, enrollment_version) "
                    "VALUES ('w', 'p1', %s, 1)", (uid,))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id, enrollment_version) "
                        "VALUES ('w', 'p2', %s, 2)", (uid,))


def test_a_revocation_must_state_a_reason(admin_db, two_tenants):
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id) VALUES ('r', 'p', %s)", (uid,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE voice_profiles SET revoked_at = NOW() WHERE user_id = %s", (uid,))


def test_redaction_is_not_a_valid_deletion_state(admin_db, two_tenants):
    """A redacted biometric template is still a biometric template."""
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id) VALUES ('d', 'p', %s)", (uid,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE voice_profiles SET deletion_status = 'redacted' WHERE user_id = %s", (uid,))


def test_a_tenant_admin_cannot_read_another_users_voiceprint(db, admin_db, two_tenants):
    a_user = two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id) VALUES ('a', 'p', %s)",
                    (str(a_user),))
    admin_db.commit()
    try:
        with db.cursor() as cur:
            set_context(cur, tenant=two_tenants["a"]["tenant"], user=uuid.uuid4())
            cur.execute("SELECT count(*) FROM voice_profiles")
            assert cur.fetchone()[0] == 0
    finally:
        with admin_db.cursor() as cur:
            cur.execute("DELETE FROM voice_profiles WHERE user_id = %s", (str(a_user),))
        admin_db.commit()


def test_the_voice_status_view_exposes_no_template(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT * FROM voice_migration_status")
        columns = [d[0] for d in cur.description]
    for forbidden in ("embedding", "template_ciphertext", "owner_name"):
        assert forbidden not in columns


def test_the_system_does_not_yet_claim_voiceprints_are_encrypted(admin_db, two_tenants):
    """Guards the honesty of the claim: while any live row is PLAINTEXT,
    `contract_ready` must be false."""
    uid = str(two_tenants["a"]["user"])
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO voice_profiles (owner_name, embedding, user_id) VALUES ('e', 'p', %s)", (uid,))
        cur.execute("SELECT still_plaintext, contract_ready FROM voice_migration_status")
        plaintext, ready = cur.fetchone()
        assert plaintext >= 1
        assert ready is False, "contract_ready must be false while a template is plaintext"


# ── Batch 1C — biometric audit ───────────────────────────────────────────────

def test_biometric_audit_refuses_update(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result) "
                    "VALUES ('s1', 0.9, 0.8, TRUE) RETURNING id")
        rid = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("UPDATE biometric_audit SET result = FALSE WHERE id = %s", (rid,))


def test_biometric_audit_refuses_delete(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result) "
                    "VALUES ('s2', 0.9, 0.8, TRUE) RETURNING id")
        rid = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM biometric_audit WHERE id = %s", (rid,))


def test_a_platform_event_may_have_no_tenant(admin_db):
    """A failed authentication happens before a tenant is resolved. Forcing NOT
    NULL would mean inventing one or discarding the event."""
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, tenant_id) "
                    "VALUES ('platform', 0.1, 0.8, FALSE, NULL) RETURNING id")
        assert cur.fetchone()[0] is not None


def test_a_platform_event_is_invisible_to_every_tenant(db, admin_db, two_tenants):
    """`NULL = <uuid>` is NULL, not true — so nullable tenant is a security
    property here rather than a hole."""
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, tenant_id) "
                    "VALUES ('platform-scope', 0.1, 0.8, FALSE, NULL)")
    admin_db.commit()
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"])
        cur.execute("SELECT count(*) FROM biometric_audit WHERE session_id = 'platform-scope'")
        assert cur.fetchone()[0] == 0


def test_a_workspace_context_implies_a_tenant_context(admin_db, two_tenants):
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "tenant_id, workspace_id) VALUES ('w', 0.9, 0.8, TRUE, NULL, %s)",
                (str(two_tenants["a"]["workspace"]),))


def test_a_row_naming_a_person_must_carry_a_tenant(admin_db, two_tenants):
    """**The constraint that makes the erasure promise keepable.**

    A row with a `user_id` and no `tenant_id` cannot be redacted. PostgreSQL
    requires an UPDATE's resulting row to still satisfy the SELECT policy, and a
    row with both attribution columns cleared satisfies no arm of it — so the
    update is refused and the row naming the person outlives their deletion
    request. Making that shape unrepresentable is what closes it.

    `tenant_id` alone stays nullable: a match that failed before any tenant was
    resolved is still recorded, it simply names nobody.
    """
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "tenant_id, user_id) VALUES ('orphan', 0.1, 0.8, FALSE, NULL, %s)",
                (str(two_tenants["a"]["user"]),))


def test_the_tenant_can_redact_a_row_and_the_evidence_survives(db, admin_db, two_tenants):
    """What `core/privacy/erasure.erase_audit` actually does, run for real.

    Three gates must all admit it: the append-only trigger has to accept
    clearing attribution, the UPDATE policy's WITH CHECK has to accept the
    result, and — the one that is easy to miss — the resulting row has to remain
    visible under the SELECT policy, or PostgreSQL refuses the update with a
    message that blames WITH CHECK.
    """
    uid = two_tenants["a"]["user"]
    tid = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('redact-me', 0.42, 0.8, FALSE, %s, %s)",
                    (str(tid), str(uid)))
    admin_db.commit()

    with db.cursor() as cur:
        set_context(cur, tenant=tid, user=uid)
        cur.execute("UPDATE biometric_audit SET user_id = NULL "
                    "WHERE user_id = %s AND tenant_id = %s", (str(uid), str(tid)))
        assert cur.rowcount >= 1, "the redaction matched no rows"
    db.commit()

    with admin_db.cursor() as cur:
        cur.execute("SELECT user_id, tenant_id, similarity, threshold, result "
                    "FROM biometric_audit WHERE session_id = 'redact-me'")
        row = cur.fetchone()
    assert row is not None, "the row was deleted rather than redacted"
    assert row[0] is None, "attribution survived the redaction"
    assert str(row[1]) == str(tid), (
        "the row lost its tenant. It must stay where it belongs — a row redacted "
        "out of every tenant's sight is one nobody can audit.")
    assert (float(row[2]), float(row[3]), row[4]) == (0.42, 0.8, False), (
        "the match statistics changed. Redaction removes who it was about; the "
        "evidence of what happened must be byte-identical.")


def test_redaction_cannot_move_a_row_out_of_its_tenant(db, admin_db, two_tenants):
    """The UPDATE policy is tenant-scoped on both sides. Clearing `tenant_id`
    would hide the row from the tenant that owns the evidence, which is a way of
    destroying it without deleting it."""
    uid = two_tenants["a"]["user"]
    tid = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('keep-tenant', 0.5, 0.8, TRUE, %s, %s)",
                    (str(tid), str(uid)))
    admin_db.commit()

    with db.cursor() as cur:
        set_context(cur, tenant=tid, user=uid)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("UPDATE biometric_audit SET tenant_id = NULL, user_id = NULL "
                        "WHERE session_id = 'keep-tenant'")
    db.rollback()


def test_a_tenant_cannot_redact_another_tenants_row(db, admin_db, two_tenants):
    """Erasure in one tenant must not reach another tenant's audit evidence."""
    other_uid = two_tenants["b"]["user"]
    other_tid = two_tenants["b"]["tenant"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('not-yours', 0.9, 0.8, TRUE, %s, %s)",
                    (str(other_tid), str(other_uid)))
    admin_db.commit()

    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
        cur.execute("UPDATE biometric_audit SET user_id = NULL WHERE session_id = 'not-yours'")
        assert cur.rowcount == 0, "a tenant redacted another tenant's audit row"
    db.rollback()

    with admin_db.cursor() as cur:
        cur.execute("SELECT user_id FROM biometric_audit WHERE session_id = 'not-yours'")
        assert cur.fetchone()[0] is not None


def test_the_subject_sees_their_own_rows_across_tenant_context(db, admin_db, two_tenants):
    """The SELECT policy's subject arm. A person may read records about
    themselves without being bound to the tenant that holds them — a read right,
    deliberately wider than the UPDATE policy, which is tenant-scoped."""
    uid = two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('mine', 0.7, 0.8, FALSE, %s, %s)",
                    (str(two_tenants["a"]["tenant"]), str(uid)))
    admin_db.commit()

    with db.cursor() as cur:
        # Bound to the OTHER tenant, but as the subject of the row.
        set_context(cur, tenant=two_tenants["b"]["tenant"], user=uid)
        cur.execute("SELECT count(*) FROM biometric_audit WHERE session_id = 'mine'")
        assert cur.fetchone()[0] == 1
    db.rollback()

    with db.cursor() as cur:
        # Bound to the other tenant as somebody else: invisible.
        set_context(cur, tenant=two_tenants["b"]["tenant"], user=two_tenants["b"]["user"])
        cur.execute("SELECT count(*) FROM biometric_audit WHERE session_id = 'mine'")
        assert cur.fetchone()[0] == 0
    db.rollback()


def test_deleting_a_tenant_takes_its_audit_trail_and_nothing_else(admin_db, two_tenants):
    """The one DELETE the append-only rule admits, and its boundary.

    `tenant_id` CASCADEs because `SET NULL` would leave an attributed row with
    no tenant, which the attribution constraint forbids — so tenant offboarding
    would fail. The trigger therefore permits a DELETE only when the row's
    tenant is already gone, which is true exactly during that cascade.
    """
    tid_a = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('goes-with-tenant', 0.3, 0.8, FALSE, %s, %s)",
                    (str(tid_a), str(two_tenants["a"]["user"])))
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('stays', 0.3, 0.8, FALSE, %s, %s)",
                    (str(two_tenants["b"]["tenant"]), str(two_tenants["b"]["user"])))

        # An ordinary delete is still refused, tenant alive.
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM biometric_audit WHERE session_id = 'stays'")
    admin_db.rollback()

    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('goes-with-tenant', 0.3, 0.8, FALSE, %s, %s)",
                    (str(tid_a), str(two_tenants["a"]["user"])))
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('stays', 0.3, 0.8, FALSE, %s, %s)",
                    (str(two_tenants["b"]["tenant"]), str(two_tenants["b"]["user"])))
        cur.execute("DELETE FROM tenant_memberships WHERE tenant_id = %s", (str(tid_a),))
        cur.execute("DELETE FROM tenants WHERE id = %s", (str(tid_a),))

        cur.execute("SELECT count(*) FROM biometric_audit WHERE session_id = 'goes-with-tenant'")
        assert cur.fetchone()[0] == 0, "the tenant's audit rows outlived the tenant"
        cur.execute("SELECT count(*) FROM biometric_audit WHERE session_id = 'stays'")
        assert cur.fetchone()[0] == 1, "another tenant's audit rows were taken too"
    admin_db.rollback()


def test_a_platform_scope_row_cannot_be_deleted_by_the_cascade_carve_out(admin_db):
    """`OLD.tenant_id IS NOT NULL` in the trigger. Without it, `NOT EXISTS` is
    trivially true for an untenanted row and the rows with the least oversight
    — the ones no tenant can see — become the freely deletable ones."""
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('platform-del', 0.1, 0.8, FALSE, NULL, NULL)")
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("DELETE FROM biometric_audit WHERE session_id = 'platform-del'")
    admin_db.rollback()


def test_the_policy_refuses_an_unattributed_row_aimed_at_another_tenant(db, two_tenants):
    """The INSERT exemption is `tenant_id IS NULL AND user_id IS NULL`, not the
    looser `user_id IS NULL`, which would let a caller write an unattributed row
    into somebody else's tenant."""
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "tenant_id, user_id) VALUES ('cross', 0.9, 0.8, TRUE, %s, NULL)",
                (str(two_tenants["b"]["tenant"]),))
    db.rollback()


def test_biometric_audit_holds_no_template_column(admin_db):
    """Copying a template into the audit trail would defeat 037's encryption by
    leaving a plaintext copy under different access rules."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'biometric_audit' "
                    "AND column_name IN ('embedding', 'template', 'template_ciphertext', 'audio')")
        assert cur.fetchone()[0] == 0


def test_deleting_a_user_keeps_the_audit_row(admin_db, two_tenants):
    """Evidence that disappears when its subject is deleted is not evidence."""
    uid = two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                    "tenant_id, user_id) VALUES ('keepme', 0.9, 0.8, TRUE, %s, %s)",
                    (str(two_tenants["a"]["tenant"]), str(uid)))
        cur.execute("DELETE FROM tenant_memberships WHERE user_id = %s", (str(uid),))
        cur.execute("DELETE FROM users WHERE id = %s", (str(uid),))
        cur.execute("SELECT user_id FROM biometric_audit WHERE session_id = 'keepme'")
        row = cur.fetchone()
        assert row is not None, "the audit row was cascade-deleted with the user"
        assert row[0] is None, "the identifying column should be cleared, not the row removed"
