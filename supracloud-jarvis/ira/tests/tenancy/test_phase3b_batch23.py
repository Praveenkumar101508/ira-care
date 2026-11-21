"""Phase 3B Batches 2 and 3 — workspace substrate, memberships and grants.

Two tests here matter more than the rest.

`test_removing_a_tenant_membership_revokes_workspace_access` covers the hole
Batch 0 left: `workspace_memberships` had nothing tying it to
`tenant_memberships`, so removing somebody from a tenant left every workspace
membership intact — and workspace membership is what Batch 5's content policies
will read. "Removed from the organisation but still in Engineering" is not a
stale row, it is continued access after revocation.

`test_the_status_view_reports_a_planted_row` covers a defect this batch found in
Batch 1: a view runs as its OWNER, and the owner is subject to FORCE ROW LEVEL
SECURITY. `totp_migration_status` therefore reported `contract_ready = true` on
a table holding a plaintext secret, to every caller including a superuser. The
guard in `migrate_secrets.py` did not catch it, because it checked ownership —
which is exactly the property that does not help.

Everything runs against a real PostgreSQL under a **non-superuser** role, and
the suite asserts that before making any isolation claim.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — real-database Batch 2/3 tests skipped",
                allow_module_level=True)

NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


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


def set_context(cur, *, tenant=None, user=None, workspace=None):
    cur.execute("SELECT set_config('app.tenant_id',    %s, true)", (str(tenant) if tenant else "",))
    cur.execute("SELECT set_config('app.user_id',      %s, true)", (str(user) if user else "",))
    cur.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace) if workspace else "",))


@pytest.fixture
def two_tenants(admin_db):
    """Two provisioned tenants, each with an owner who is in the default workspace."""
    made = {}
    with admin_db.cursor() as cur:
        for label in ("a", "b"):
            tid = uuid.uuid4()
            uid = uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name, edition, status) "
                        "VALUES (%s, %s, 'cloud', 'active')", (str(tid), f"B23 {label}"))
            cur.execute("INSERT INTO users (id, email) VALUES (%s, %s)",
                        (str(uid), f"b23-{label}-{tid.hex[:8]}@example.com"))
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


# ── The harness must be honest before anything is claimed ────────────────────

def test_app_role_is_not_superuser(db):
    with db.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        rolsuper, bypass = cur.fetchone()
    assert not rolsuper, "the test role is a superuser — it bypasses RLS and proves nothing"
    assert not bypass, "the test role holds BYPASSRLS — same problem"


# ── Batch 2 — workspace substrate ────────────────────────────────────────────

def test_the_migrated_tenants_have_an_organisation_and_a_default_workspace(admin_db):
    """The invariant Batch 5 depends on: a tenant without a default workspace is
    a tenant whose content has nowhere to land.

    Asserted over the tenants the **migration chain** produced, not over every
    row in the table. Provisioning is a platform operation — deliberately, see
    `039` section 3 — so any code path that creates a tenant without calling
    `provision_tenant_substrate` leaves one behind, and a dozen other test
    modules in this suite create bare tenants directly. A global assertion here
    would fail depending on which tests ran first, which is a flaky test rather
    than a real guarantee.

    The globally true parts are asserted below, because those two ARE structural.
    """
    with admin_db.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM tenants t
             WHERE t.id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid
               AND (NOT EXISTS (SELECT 1 FROM organisations o WHERE o.tenant_id = t.id)
                 OR NOT EXISTS (SELECT 1 FROM workspaces w
                                 WHERE w.tenant_id = t.id AND w.is_default
                                   AND w.status = 'active'))
        """)
        assert cur.fetchone()[0] == 0, (
            "the tenant the migration chain seeds has no organisation or no active "
            "default workspace — the 039 backfill did not run, or ran against a "
            "row-level-security-filtered view of the table and matched nothing.")


def test_the_structural_substrate_invariants_hold_globally(admin_db):
    """The two parts no code path can break, because a constraint enforces them:
    at most one organisation per tenant (unique index) and no archived default
    (check constraint). Unlike "every tenant is provisioned", these do not depend
    on anybody remembering to call a function."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT tenants_with_multiple_organisations, archived_defaults "
                    "FROM workspace_substrate_status")
        multiple, archived = cur.fetchone()
    assert multiple == 0, "a tenant has more than one organisation; D-15 says 1:1"
    assert archived == 0, "an archived workspace is still marked default"


def test_the_substrate_view_is_not_blinded_by_row_level_security(admin_db):
    """**The defect this batch found, one level up.**

    A view runs as its OWNER by default, and the owner is subject to FORCE ROW
    LEVEL SECURITY. Without `security_invoker`, this view evaluated with the
    migration role's unbound context and reported every tenant as missing
    substrate that plainly existed — to every caller, superuser included.

    Plant a tenant with no substrate and require the count to move by exactly
    one. A view that can see nothing reports a constant, so the delta is what
    catches it regardless of what else is in the table.
    """
    with admin_db.cursor() as cur:
        cur.execute("SELECT tenants_without_organisation FROM workspace_substrate_status")
        before = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (id, name, edition, status) "
                    "VALUES (%s, 'no-substrate', 'cloud', 'active')", (str(uuid.uuid4()),))
        cur.execute("SELECT tenants_without_organisation FROM workspace_substrate_status")
        after = cur.fetchone()[0]
    assert after == before + 1, (
        f"planting a tenant with no organisation moved the count from {before} to "
        f"{after}. The view is reporting what its owner can see rather than what "
        f"is there — see security_invoker in 039.")
    admin_db.rollback()


def test_provisioning_is_idempotent_and_deterministic(admin_db):
    """Re-running must return the same workspace, not make a second one — the
    partial unique index would refuse it, and an error on the second boot is a
    failure mode nobody sees in development."""
    tid = uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name, edition, status) "
                    "VALUES (%s, 'idem', 'cloud', 'active')", (str(tid),))
        cur.execute("SELECT provision_tenant_substrate(%s)", (str(tid),))
        first = cur.fetchone()[0]
        cur.execute("SELECT provision_tenant_substrate(%s)", (str(tid),))
        second = cur.fetchone()[0]
        assert first == second
        assert str(first) == str(uuid.uuid5(NAMESPACE, f"workspace:default:{tid}")), \
            "the workspace id is not the deterministic UUIDv5 the backfill derives"
        cur.execute("SELECT count(*) FROM organisations WHERE tenant_id = %s", (str(tid),))
        assert cur.fetchone()[0] == 1
    admin_db.rollback()


def test_provisioning_refuses_an_unknown_tenant(admin_db):
    """A typo in a tenant id must not manufacture a tenant nobody asked for."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("SELECT provision_tenant_substrate(%s)", (str(uuid.uuid4()),))
    admin_db.rollback()


def test_a_tenant_cannot_be_left_without_a_default_workspace(admin_db, two_tenants):
    """The deferred constraint trigger. Archiving the only default must fail at
    COMMIT rather than silently leaving content with nowhere to go."""
    tid = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        cur.execute("UPDATE workspaces SET is_default = FALSE, status = 'archived', "
                    "archived_at = NOW() WHERE tenant_id = %s", (str(tid),))
        with pytest.raises(psycopg2.errors.RaiseException):
            admin_db.commit()
    admin_db.rollback()


def test_the_default_may_be_swapped_in_one_transaction(admin_db, two_tenants):
    """Deferred, not immediate — otherwise promoting a replacement would be
    impossible and the only way to change defaults would be to break the rule."""
    tid = two_tenants["a"]["tenant"]
    org = None
    with admin_db.cursor() as cur:
        cur.execute("SELECT id FROM organisations WHERE tenant_id = %s", (str(tid),))
        org = cur.fetchone()[0]
        cur.execute("INSERT INTO workspaces (tenant_id, organisation_id, name, slug, is_default) "
                    "VALUES (%s, %s, 'Second', 'second', FALSE)", (str(tid), str(org)))
        cur.execute("UPDATE workspaces SET is_default = FALSE WHERE tenant_id = %s AND is_default",
                    (str(tid),))
        cur.execute("UPDATE workspaces SET is_default = TRUE WHERE tenant_id = %s AND slug = 'second'",
                    (str(tid),))
    admin_db.commit()
    with admin_db.cursor() as cur:
        cur.execute("SELECT slug FROM workspaces WHERE tenant_id = %s AND is_default", (str(tid),))
        assert cur.fetchone()[0] == "second"
    admin_db.rollback()


def test_an_archived_workspace_cannot_be_the_default(admin_db, two_tenants):
    tid = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE workspaces SET status = 'archived', archived_at = NOW() "
                        "WHERE tenant_id = %s AND is_default", (str(tid),))
    admin_db.rollback()


def test_archived_at_and_status_must_agree(admin_db, two_tenants):
    tid = two_tenants["a"]["tenant"]
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE workspaces SET archived_at = NOW() WHERE tenant_id = %s", (str(tid),))
    admin_db.rollback()


def test_a_workspace_cannot_be_read_across_tenants(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
        cur.execute("SELECT count(*) FROM workspaces WHERE id = %s",
                    (str(two_tenants["b"]["workspace"]),))
        assert cur.fetchone()[0] == 0
    db.rollback()


# ── Batch 3 — memberships and grants ─────────────────────────────────────────

def test_removing_a_tenant_membership_revokes_workspace_access(admin_db, two_tenants):
    """**The hole Batch 0 left.**

    `workspace_memberships` had nothing tying it to `tenant_memberships`, so
    removing somebody from a tenant left their workspace membership granting
    access — and workspace membership is what Batch 5's content policies read.
    """
    tid, uid = two_tenants["a"]["tenant"], two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("SELECT workspace_grants_access FROM workspace_memberships "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        assert cur.fetchone()[0] is True, "fixture did not grant workspace access"

        cur.execute("UPDATE tenant_memberships SET status = 'removed', revoked_at = NOW() "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))

        cur.execute("SELECT status, workspace_grants_access FROM workspace_memberships "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        status, grants = cur.fetchone()
    assert status == "removed", "workspace membership survived tenant removal"
    assert grants is False, (
        "the workspace membership still grants access after the tenant membership "
        "was revoked. That is continued access after revocation.")
    admin_db.rollback()


def test_suspension_also_revokes_workspace_access(admin_db, two_tenants):
    """Suspension is not deletion, and it must not be a way to keep access."""
    tid, uid = two_tenants["a"]["tenant"], two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("UPDATE tenant_memberships SET status = 'suspended', suspended_at = NOW() "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        cur.execute("SELECT workspace_grants_access FROM workspace_memberships "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        assert cur.fetchone()[0] is False
    admin_db.rollback()


def test_restoring_a_membership_does_not_restore_workspace_access(admin_db, two_tenants):
    """Deliberately asymmetric. Which workspaces somebody should rejoin is a
    decision; silently returning them to every workspace they were once in is
    how a re-hired contractor regains access to a project they were removed
    from for cause."""
    tid, uid = two_tenants["a"]["tenant"], two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        cur.execute("UPDATE tenant_memberships SET status = 'suspended', suspended_at = NOW() "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        cur.execute("UPDATE tenant_memberships SET status = 'active', suspended_at = NULL "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        cur.execute("SELECT workspace_grants_access FROM workspace_memberships "
                    "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
        assert cur.fetchone()[0] is False, (
            "workspace access came back on its own. Re-granting it is a decision "
            "somebody has to make.")
    admin_db.rollback()


def test_a_workspace_membership_cannot_exist_without_a_tenant_membership(admin_db, two_tenants):
    """The composite key. The trigger cannot cover this case: it never fires for
    a row inserted with no parent."""
    tid, wid = two_tenants["a"]["tenant"], two_tenants["a"]["workspace"]
    stranger = uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO users (id, email) VALUES (%s, %s)",
                    (str(stranger), f"stranger-{stranger.hex[:8]}@example.com"))
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute("INSERT INTO workspace_memberships "
                        "(tenant_id, workspace_id, user_id, role, status) "
                        "VALUES (%s, %s, %s, 'member', 'active')",
                        (str(tid), str(wid), str(stranger)))
    admin_db.rollback()


def test_workspace_status_and_removed_at_must_agree(admin_db, two_tenants):
    tid, uid = two_tenants["a"]["tenant"], two_tenants["a"]["user"]
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE workspace_memberships SET removed_at = NOW() "
                        "WHERE tenant_id = %s AND user_id = %s", (str(tid), str(uid)))
    admin_db.rollback()


# ── Workspace grants ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,capability,allowed", [
    ("viewer",          "workspace.read",             True),
    ("viewer",          "workspace.write",            False),
    ("viewer",          "workspace.delete_content",   False),
    ("member",          "workspace.write",            True),
    ("member",          "workspace.delete_content",   False),
    ("member",          "workspace.manage_members",   False),
    ("workspace_admin", "workspace.manage_members",   True),
    ("workspace_admin", "workspace.delete_content",   True),
])
def test_workspace_role_capabilities(admin_db, role, capability, allowed):
    """`member` deliberately cannot delete content: the difference between
    "I can add" and "I can remove what you added" is the one customers complain
    about."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT workspace_role_allows(%s, %s)", (role, capability))
        assert cur.fetchone()[0] is allowed


@pytest.mark.parametrize("role,capability", [
    ("nonexistent_role", "workspace.read"),
    ("workspace_admin",  "workspace.invent_a_capability"),
])
def test_unknown_roles_and_capabilities_fail_closed(admin_db, role, capability):
    """A missing row is not permission."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT workspace_role_allows(%s, %s)", (role, capability))
        assert cur.fetchone()[0] is False


def test_a_tenant_cannot_grant_itself_a_capability(db, two_tenants):
    """`workspace_role_capabilities` is REFERENCE_DATA with a read-only policy.
    A tenant able to add capabilities to its own roles is a tenant able to grant
    itself privilege."""
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
        cur.execute("SELECT count(*) FROM workspace_role_capabilities")
        assert cur.fetchone()[0] > 0, "reference data is not readable — it should be"
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO workspace_role_capabilities (role, capability) "
                        "VALUES ('viewer', 'workspace.delete_content')")
    db.rollback()


# ── Membership visibility ────────────────────────────────────────────────────

def test_a_person_can_list_their_own_memberships_before_binding_a_tenant(db, two_tenants):
    """The sign-in tenant picker. The question has to be asked BEFORE a tenant is
    bound, and a tenant-only predicate returns nothing when none is."""
    uid = two_tenants["a"]["user"]
    with db.cursor() as cur:
        set_context(cur, user=uid)          # no tenant bound at all
        cur.execute("SELECT count(*) FROM tenant_memberships WHERE user_id = %s", (str(uid),))
        assert cur.fetchone()[0] == 1, (
            "a user cannot see their own membership without a tenant bound, so "
            "there is no way to answer 'which tenants am I in?' at sign-in")
    db.rollback()


def test_the_tenant_directory_shows_only_your_own_tenants(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, user=two_tenants["a"]["user"])
        cur.execute("SELECT tenant_id FROM tenant_directory")
        seen = {str(r[0]) for r in cur.fetchall()}
    assert str(two_tenants["a"]["tenant"]) in seen
    assert str(two_tenants["b"]["tenant"]) not in seen, "the directory leaked another tenant"
    db.rollback()


def test_the_subject_arm_is_read_only(db, two_tenants):
    """Reading your own membership is a right; editing it is not. A member who
    could update their own row could set their role to `owner`."""
    uid = two_tenants["a"]["user"]
    with db.cursor() as cur:
        set_context(cur, user=uid)          # subject arm only, no tenant bound
        cur.execute("UPDATE tenant_memberships SET role = 'owner' WHERE user_id = %s", (str(uid),))
        assert cur.rowcount == 0, (
            "a user updated their own membership with no tenant bound. The subject "
            "arm is on the SELECT policy only, precisely so this cannot happen.")
    db.rollback()


def test_a_tenant_still_cannot_read_another_tenants_memberships(db, two_tenants):
    with db.cursor() as cur:
        set_context(cur, tenant=two_tenants["a"]["tenant"], user=two_tenants["a"]["user"])
        cur.execute("SELECT count(*) FROM tenant_memberships WHERE tenant_id = %s",
                    (str(two_tenants["b"]["tenant"]),))
        assert cur.fetchone()[0] == 0
    db.rollback()


# ── delegated_users is closed ────────────────────────────────────────────────

def test_no_new_delegated_user_may_be_created(admin_db):
    """A COMMENT saying "legacy" does not stop a call site adding row 51 next
    week, and every row added after the backfill is a principal that exists in
    the old model and not the new one."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("INSERT INTO delegated_users (username, password_hash, role) "
                        "VALUES ('newguy', 'x', 'trusted_user')")
    admin_db.rollback()


def test_existing_delegated_users_can_still_be_deactivated(admin_db, two_tenants):
    """UPDATE stays open: existing rows still need to be deactivated during the
    transition, and closing that off would strand them.

    The row is planted with the trigger disabled, which is the only honest way
    to simulate a row that predates the migration — the whole point of the
    trigger is that this insert is otherwise impossible. Skipping when the table
    is empty would have made this test silently absent on every clean database,
    which is every database this suite runs against.
    """
    with admin_db.cursor() as cur:
        cur.execute("ALTER TABLE delegated_users DISABLE TRIGGER delegated_users_closed")
        try:
            # `id` carries no default on this table — a legacy shape, and one
            # more reason it is closed rather than extended.
            cur.execute(
                "INSERT INTO delegated_users (id, username, password_hash, role, "
                "tenant_id, user_id, active) "
                "VALUES (%s, 'legacy-probe', 'x', 'trusted_user', %s, %s, TRUE)",
                (str(uuid.uuid4()), str(two_tenants["a"]["tenant"]),
                 str(two_tenants["a"]["user"])))
        finally:
            cur.execute("ALTER TABLE delegated_users ENABLE TRIGGER delegated_users_closed")

        cur.execute("UPDATE delegated_users SET active = FALSE WHERE username = 'legacy-probe'")
        assert cur.rowcount == 1, "an existing delegated user could not be deactivated"

        cur.execute("SELECT total_rows, resolved FROM delegated_user_migration_status")
        total, resolved = cur.fetchone()
        assert total >= 1 and resolved >= 1
    admin_db.rollback()


def test_the_retirement_gate_reports_unresolved_rows(admin_db):
    """`ready_to_retire` gates the Batch 10 drop. Dropping the table while a row
    has an unresolved owner discards a principal nobody has mapped."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT total_rows, unresolved, ready_to_retire "
                    "FROM delegated_user_migration_status")
        total, unresolved, ready = cur.fetchone()
    assert ready == (unresolved == 0), "ready_to_retire disagrees with the unresolved count"


# ── The view-blindness defect, held closed ───────────────────────────────────

def test_the_status_view_reports_a_planted_row(admin_db):
    """**The Batch 1 defect this batch found.**

    `totp_migration_status` is a view, and a view runs as its OWNER, who is
    subject to FORCE ROW LEVEL SECURITY. Without `security_invoker` it evaluated
    with the migration role's unbound context and reported `contract_ready =
    true` on a table holding a plaintext secret — to every caller, superuser
    included, because the caller's privileges never entered into it.

    Plant a plaintext row and require the view to see it.
    """
    uid = uuid.uuid4()
    with admin_db.cursor() as cur:
        cur.execute("INSERT INTO users (id, email) VALUES (%s, %s)",
                    (str(uid), f"planted-{uid.hex[:8]}@example.com"))
        # Never a real secret value, even in a test fixture.
        cur.execute("INSERT INTO totp_secrets (username, secret, user_id, tenant_id) "
                    "VALUES (%s, %s, %s, 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c')", (f"planted-{uid.hex[:8]}", "not-a-real-secret", str(uid)))

        cur.execute("SELECT total_rows, still_plaintext, contract_ready FROM totp_migration_status")
        total, plaintext, ready = cur.fetchone()

    assert total >= 1, (
        "the view cannot see a row that exists. It is reporting what its owner "
        "can see rather than what is there — see security_invoker in 036.")
    assert plaintext >= 1
    assert ready is False, (
        "contract_ready is true while a plaintext secret exists. That is the "
        "flag that gates dropping the plaintext column.")
    admin_db.rollback()


def test_no_table_is_left_with_force_row_level_security_off(admin_db):
    """The migrations lift FORCE to backfill and restore it at the end of the
    file. A migration that forgot to restore it would leave the owner able to
    bypass every policy on that table, silently.

    `legacy_identity_map` is the one intended exception: RLS on, FORCE off, no
    policy — so the owner can run the backfills and the application role still
    reads nothing (CLOUD-ADR-0011).
    """
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "   AND c.relrowsecurity AND NOT c.relforcerowsecurity")
        unforced = sorted(r[0] for r in cur.fetchall())
    assert unforced == ["legacy_identity_map"], (
        f"tables with RLS enabled but not forced: {unforced}. Every one of these "
        f"lets the table owner — which is the migration role, and in this "
        f"deployment the application role — bypass its own policies.")


# ── Batch 1 follow-up: biometric claim telemetry (041) ───────────────────────

def test_a_pre_tenant_failure_can_record_a_claim_without_naming_anybody(admin_db):
    """**Closes R-49.**

    `038` forbade a row that names a person with no tenant, because such a row
    cannot be redacted — so a match failing before a tenant was resolved could no
    longer record who was being claimed. This is the replacement: a salted hash
    is a correlation token, not a user reference, so it answers "the same
    identity was claimed N times" without there being anything for an erasure
    request to reach.
    """
    claim = "a" * 64
    with admin_db.cursor() as cur:
        cur.execute(
            "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
            "tenant_id, user_id, claim_hash, claim_hash_version, failure_reason) "
            "VALUES ('pre-tenant', 0.2, 0.8, FALSE, NULL, NULL, %s, 1, 'below_threshold') "
            "RETURNING id", (claim,))
        assert cur.fetchone()[0] is not None
    admin_db.rollback()


def test_the_claim_column_refuses_a_raw_username(admin_db):
    """Structural, not a convention. A raw username cannot match 64 hex
    characters, so "we put the username in the hash column" fails at write time
    instead of being found in a review."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "claim_hash, claim_hash_version) "
                "VALUES ('raw', 0.2, 0.8, FALSE, 'alice', 1)")
    admin_db.rollback()


def test_a_claim_hash_must_carry_its_pepper_version(admin_db):
    """A hash whose pepper version is unknown stops correlating silently after a
    rotation, and an attack that stops correlating looks like an attack that
    stopped."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "claim_hash) VALUES ('noversion', 0.2, 0.8, FALSE, %s)", ("b" * 64,))
    admin_db.rollback()


def test_a_successful_match_has_no_failure_reason(admin_db):
    """Keeps the column from drifting into a free-text note field, which is
    where somebody eventually writes the username."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "failure_reason) VALUES ('ok', 0.9, 0.8, TRUE, 'below_threshold')")
    admin_db.rollback()


def test_the_claim_hash_is_evidential_and_cannot_be_rewritten(admin_db):
    """`038`'s trigger lists evidential columns explicitly, so a column added
    later is unprotected unless the trigger is updated with it. A new column that
    looks protected and is not is worse than one that obviously is not."""
    claim = "c" * 64
    with admin_db.cursor() as cur:
        cur.execute(
            "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
            "claim_hash, claim_hash_version) VALUES ('immutable', 0.2, 0.8, FALSE, %s, 1) "
            "RETURNING id", (claim,))
        rid = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.RaiseException):
            cur.execute("UPDATE biometric_audit SET claim_hash = %s WHERE id = %s",
                        ("d" * 64, rid))
    admin_db.rollback()


def test_the_attribution_constraint_still_holds(admin_db, two_tenants):
    """041 must not have reopened what 038 closed: a row naming a person still
    requires a tenant, or the erasure guarantee goes with it."""
    with admin_db.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO biometric_audit (session_id, similarity, threshold, result, "
                "tenant_id, user_id, claim_hash, claim_hash_version) "
                "VALUES ('still-closed', 0.2, 0.8, FALSE, NULL, %s, %s, 1)",
                (str(two_tenants["a"]["user"]), "e" * 64))
    admin_db.rollback()


def test_biometric_audit_still_holds_no_biometric_material(admin_db):
    """041 added columns; none of them may be a place to put a template."""
    with admin_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'biometric_audit' "
                    "AND column_name IN ('embedding', 'template', 'template_ciphertext', "
                    "'audio', 'sample', 'raw_audio', 'claimed_username')")
        assert cur.fetchone()[0] == 0
