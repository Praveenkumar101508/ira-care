"""Phase 3B-3.5 — the runtime role cannot disable or bypass its own controls.

This suite is the evidence for D-18 and R-50.

Until `042_role_ownership_separation.sql`, the runtime role owned every table,
because `utils/migrations.py` applied the chain through the ordinary connection
pool and DDL requires ownership. So the role serving requests could
`ALTER TABLE … NO FORCE ROW LEVEL SECURITY`, drop a policy, or hand itself
ownership of anything — and `FORCE ROW LEVEL SECURITY` constrained the
application's *queries*, not the application's *role*.

Every isolation claim on this branch rested on that distinction without stating
it. These tests assert the distinction is gone: each statement below is one a
compromised application process would attempt, and each must fail.

**A passing DDL-denial test is only meaningful if the statement would otherwise
have worked.** `test_the_denial_suite_is_not_vacuous` asserts the tables exist
and the role can perform ordinary DML on them, so a suite that failed because
the table was missing could not be mistaken for a suite that passed because the
privilege was absent.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — role-separation tests skipped",
                allow_module_level=True)

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

#: A representative protected table. `tenant_memberships` is the one that
#: decides who belongs to which tenant, so it is the one worth naming.
PROTECTED = "tenant_memberships"

#: SQLSTATEs PostgreSQL uses for "you are not the owner" / "permission denied".
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
    if not ADMIN_DSN:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def _role_exists(conn, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        return cur.fetchone()[0]


# ── The harness itself ───────────────────────────────────────────────────────

def test_the_runtime_role_is_the_separated_one(db):
    """If the suite runs as something other than a runtime role, every denial
    below might be denied for the wrong reason."""
    with db.cursor() as cur:
        cur.execute("SELECT current_user, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
        role, is_super, bypass = cur.fetchone()
    assert not is_super, f"{role} is a superuser — nothing below proves anything"
    assert not bypass, f"{role} holds BYPASSRLS — nothing below proves anything"


def test_the_denial_suite_is_not_vacuous(db):
    """The role must be able to do ordinary work on the very tables it is about
    to be refused DDL on. Otherwise a missing table or a missing grant would
    look exactly like a successful security control."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = %s", (PROTECTED,))
        assert cur.fetchone()[0] == 1, f"{PROTECTED} does not exist"

        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            cur.execute("SELECT has_table_privilege(current_user, %s, %s)",
                        (PROTECTED, privilege))
            assert cur.fetchone()[0], (
                f"the runtime role has no {privilege} on {PROTECTED}; the denials "
                f"below would pass for the wrong reason")

        # And a real read, not just a catalogue lookup.
        cur.execute(f"SELECT count(*) FROM {PROTECTED}")
        assert cur.fetchone()[0] >= 0


def test_the_runtime_owns_nothing(db):
    """The property everything else follows from."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','S','p') "
            "   AND pg_get_userbyid(c.relowner) = current_user")
        owned = cur.fetchone()[0]
    assert owned == 0, (
        f"the runtime role owns {owned} object(s). An owner can disable forced "
        f"row-level security and drop policies, so every isolation control here "
        f"is advisory (D-18, R-50).")


# ── DDL denial ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("statement,what", [
    (f"ALTER TABLE {PROTECTED} NO FORCE ROW LEVEL SECURITY",
     "exempt itself from the policies while leaving RLS apparently enabled"),
    (f"ALTER TABLE {PROTECTED} DISABLE ROW LEVEL SECURITY",
     "turn tenant isolation off outright"),
    (f"ALTER TABLE {PROTECTED} OWNER TO ira_app",
     "take ownership and acquire every power above"),
    (f"DROP POLICY tenant_scoped_update ON {PROTECTED}",
     "remove a policy"),
    (f"CREATE POLICY escape_hatch ON {PROTECTED} USING (true)",
     "add a policy that admits every row"),
    (f"DROP TABLE {PROTECTED}",
     "destroy the table"),
    ("CREATE TABLE public.unauthorised_table (id int)",
     "introduce a table outside the migration chain"),
    (f"TRUNCATE TABLE {PROTECTED}",
     "empty the table — TRUNCATE is not filtered by row-level security, so a "
     "tenant-scoped DELETE is contained and a TRUNCATE is not"),
    (f"ALTER TABLE {PROTECTED} ADD COLUMN smuggled text",
     "change the shape of a protected table"),
    (f"ALTER TABLE {PROTECTED} DROP CONSTRAINT tenant_memberships_role_check",
     "remove a constraint that validates roles"),
])
def test_the_runtime_cannot(db, statement, what):
    """Each of these is something a compromised application process would try."""
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute(statement)
    assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE, (
        f"`{statement}` failed with SQLSTATE {exc.value.pgcode} rather than "
        f"{INSUFFICIENT_PRIVILEGE} (insufficient privilege). It must be refused "
        f"because the role lacks authority, not because of a typo or a missing "
        f"object — otherwise the runtime could still {what}.")
    db.rollback()


def test_the_runtime_cannot_widen_its_own_privileges(db):
    """`GRANT` is the one case that does NOT raise.

    PostgreSQL treats a GRANT issued by a role holding no grant option as a
    no-op with a warning, not an error — so asserting an exception here would
    have been asserting the wrong thing, and a test that expected one would have
    failed while the system was behaving correctly.

    What matters is the effect. TRUNCATE is the privilege to check: it is not
    filtered by row-level security, so a role that acquired it could empty a
    tenant table outright.
    """
    with db.cursor() as cur:
        cur.execute("SELECT has_table_privilege(current_user, %s, 'TRUNCATE')", (PROTECTED,))
        assert cur.fetchone()[0] is False, "precondition: the role already holds TRUNCATE"

        cur.execute(f"GRANT ALL ON {PROTECTED} TO ira_app")   # warns, grants nothing

        cur.execute("SELECT has_table_privilege(current_user, %s, 'TRUNCATE')", (PROTECTED,))
        assert cur.fetchone()[0] is False, (
            "the runtime granted itself TRUNCATE. A GRANT that silently succeeds "
            "and does nothing is safe; one that succeeds and works is D-18 "
            "reopened by one statement.")
    db.rollback()


@pytest.mark.parametrize("role", ["ira_schema_owner", "ira_migrator"])
def test_the_runtime_cannot_become_a_privileged_role(db, role):
    """`SET ROLE` is the shortest path from "compromised runtime" to "owner".
    Only `ira_migrator` is a member of `ira_schema_owner`, and nothing is a
    member of `ira_migrator`."""
    if not _role_exists(db, role):
        pytest.skip(f"{role} not provisioned on this database")
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute(f"SET ROLE {role}")
    assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE
    db.rollback()


def test_the_runtime_cannot_grant_itself_membership(db):
    if not _role_exists(db, "ira_schema_owner"):
        pytest.skip("ira_schema_owner not provisioned")
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("GRANT ira_schema_owner TO ira_app")
    db.rollback()


def test_the_runtime_cannot_alter_its_own_role_attributes(db):
    """`ALTER ROLE … BYPASSRLS` would end every guarantee in one statement."""
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("ALTER ROLE ira_app BYPASSRLS")
    db.rollback()
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("ALTER ROLE ira_app SUPERUSER")
    db.rollback()


def test_the_runtime_cannot_create_a_role(db):
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("CREATE ROLE smuggled_role LOGIN")
    db.rollback()


def test_the_runtime_cannot_create_a_schema(db):
    """A schema it owned would be a place to put objects the migration chain
    never sees — including a function shadowing one the schema calls."""
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute("CREATE SCHEMA smuggled")
    assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE
    db.rollback()


def test_the_runtime_cannot_create_an_extension(db):
    """Most extensions install C functions that run as the server process."""
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    db.rollback()


# ── Migration history ────────────────────────────────────────────────────────

def test_the_runtime_cannot_modify_migration_history(db):
    """A runtime that could write here could mark an unapplied migration as
    applied, and the readiness check that exists to catch an un-migrated
    database would then pass on one."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='schema_migrations'")
        if cur.fetchone()[0] == 0:
            pytest.skip("schema_migrations not present (psql-applied database)")

    for statement in (
        "INSERT INTO schema_migrations (name) VALUES ('999_not_real.sql')",
        "UPDATE schema_migrations SET name = 'tampered'",
        "DELETE FROM schema_migrations",
    ):
        with db.cursor() as cur:
            with pytest.raises(psycopg2.Error) as exc:
                cur.execute(statement)
        assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE, statement
        db.rollback()


def test_the_runtime_can_read_migration_history(db):
    """Readiness has to be able to tell whether the schema is behind."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='schema_migrations'")
        if cur.fetchone()[0] == 0:
            pytest.skip("schema_migrations not present")
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] >= 0


# ── Platform functions ───────────────────────────────────────────────────────

def test_the_runtime_cannot_provision_a_tenant(db):
    """`provision_tenant_substrate` writes organisations and workspaces for an
    arbitrary tenant id. The runtime holding it would make "which tenant am I
    acting for" a parameter rather than a context."""
    with db.cursor() as cur:
        with pytest.raises(psycopg2.Error) as exc:
            cur.execute("SELECT provision_tenant_substrate(%s)", (str(uuid.uuid4()),))
    assert exc.value.pgcode == INSUFFICIENT_PRIVILEGE
    db.rollback()


def test_the_runtime_can_call_the_functions_its_writes_need(db):
    """Constraint and trigger functions execute as the CALLING role, so a
    runtime without EXECUTE cannot insert a row at all. Granting them by hand
    was tried and broke ordinary INSERTs — this asserts the derived grant
    covers them."""
    with db.cursor() as cur:
        cur.execute("SELECT workspace_role_allows('member', 'workspace.write')")
        assert cur.fetchone()[0] is True


# ── Public privileges ────────────────────────────────────────────────────────

def test_public_holds_no_table_privileges(db):
    """Ambient authority applies to roles nobody thought about, including roles
    created after the migration ran."""
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND grantee = 'PUBLIC'")
        assert cur.fetchone()[0] == 0


def test_public_cannot_create_in_the_application_schema(db):
    with db.cursor() as cur:
        cur.execute("SELECT has_schema_privilege('public', 'public', 'CREATE')")
        assert cur.fetchone()[0] is False


def test_the_runtime_has_no_ddl_adjacent_table_grants(db):
    """TRUNCATE bypasses row-level security. REFERENCES lets a role pin rows it
    cannot read. TRIGGER lets it attach code to a table it does not own."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
            " WHERE table_schema = 'public' AND grantee = current_user "
            "   AND privilege_type IN ('TRUNCATE','REFERENCES','TRIGGER')")
        found = sorted(r[0] for r in cur.fetchall())
    assert found == [], f"the runtime role holds {found}"


# ── The verification view ────────────────────────────────────────────────────

def test_role_separation_status_reads_all_clear(admin_db):
    with admin_db.cursor() as cur:
        cur.execute("SELECT objects_owned_by_runtime, tables_not_owned_by_schema_owner, "
                    "public_table_grants, runtime_roles_that_bypass, "
                    "runtime_roles_with_ddl_adjacent_grants FROM role_separation_status")
        row = cur.fetchone()
    assert row == (0, 0, 0, 0, 0), f"role_separation_status is not all zeros: {row}"


def test_the_schema_owner_cannot_log_in(admin_db):
    """NOLOGIN is what puts the owner out of reach of a stolen credential: it is
    reachable only by a role explicitly granted membership in it."""
    if not _role_exists(admin_db, "ira_schema_owner"):
        pytest.skip("ira_schema_owner not provisioned")
    with admin_db.cursor() as cur:
        cur.execute("SELECT rolcanlogin, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = 'ira_schema_owner'")
        canlogin, is_super, bypass = cur.fetchone()
    assert canlogin is False, "ira_schema_owner can log in — a stolen credential reaches the owner"
    assert is_super is False
    assert bypass is False


def test_only_the_migrator_may_become_the_schema_owner(admin_db):
    """The membership graph, asserted as a whole rather than one edge at a time.
    A second member would be a second path to ownership."""
    if not _role_exists(admin_db, "ira_schema_owner"):
        pytest.skip("ira_schema_owner not provisioned")
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT m.rolname FROM pg_auth_members am "
            "  JOIN pg_roles r ON r.oid = am.roleid "
            "  JOIN pg_roles m ON m.oid = am.member "
            " WHERE r.rolname = 'ira_schema_owner' ORDER BY 1")
        members = [r[0] for r in cur.fetchall()]
    assert members == ["ira_migrator"], (
        f"members of ira_schema_owner are {members}; only ira_migrator may be one")


def test_the_migrator_does_not_inherit_ownership_automatically(admin_db):
    """NOINHERIT makes `SET ROLE` an explicit act. With INHERIT every migrator
    connection would silently carry the owner's privileges from its first
    statement, and nothing in an audit log would show when it mattered."""
    if not _role_exists(admin_db, "ira_migrator"):
        pytest.skip("ira_migrator not provisioned")
    with admin_db.cursor() as cur:
        cur.execute("SELECT rolinherit, rolsuper, rolbypassrls, rolcreaterole "
                    "FROM pg_roles WHERE rolname = 'ira_migrator'")
        inherit, is_super, bypass, createrole = cur.fetchone()
    assert inherit is False, "ira_migrator inherits ira_schema_owner ambiently"
    assert is_super is False
    assert bypass is False
    assert createrole is False, (
        "ira_migrator can create roles, so it could grant ira_app membership in "
        "ira_schema_owner and undo this entire phase")


# ── Security-definer and search_path ─────────────────────────────────────────

def test_there_are_no_security_definer_functions(admin_db):
    """A SECURITY DEFINER function runs with its owner's privileges, and the
    owner here is `ira_schema_owner` — so one would be a standing route for the
    runtime to act as the owner.

    There are none. This asserts that stays true, because the first one added
    needs a decision recorded in `PHASE_3B_SECURITY_DEFINER_REVIEW.md`, not a
    reviewer noticing the keyword in a diff.
    """
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'public' AND p.prosecdef ORDER BY 1")
        secdef = [r[0] for r in cur.fetchall()]
    assert secdef == [], (
        f"SECURITY DEFINER function(s) added: {secdef}. Each needs an entry in "
        f"the security-definer review — owner, callers, search_path, whether it "
        f"can bypass RLS, and whether it is still necessary.")


def test_every_function_pins_its_search_path(admin_db):
    """An unpinned `search_path` lets a caller who can create objects earlier in
    the path shadow a table or function these call. `042` removed CREATE on
    `public` from PUBLIC, which closes the obvious route; pinning closes it
    independently, so neither control has to hold alone."""
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'public' AND p.prokind IN ('f','p') "
            "   AND (p.proconfig IS NULL OR NOT EXISTS ("
            "        SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%')) "
            "   AND NOT EXISTS (SELECT 1 FROM pg_depend d "
            "                    WHERE d.classid = 'pg_proc'::regclass "
            "                      AND d.objid = p.oid AND d.deptype = 'e') "
            " ORDER BY 1")
        unpinned = [r[0] for r in cur.fetchall()]
    assert unpinned == [], f"function(s) with an unpinned search_path: {unpinned}"


# ── View security ────────────────────────────────────────────────────────────

def test_every_view_declares_security_invoker(admin_db):
    """A view runs as its OWNER by default, and the owner is subject to FORCE
    ROW LEVEL SECURITY — so a default view evaluates with the schema owner's
    unbound context and reports what IT can see, which is nothing.

    That is not hypothetical: `totp_migration_status` reported
    `contract_ready = true` on a table holding a plaintext secret, to every
    caller including a superuser, until Batch 2/3 found it.
    """
    with admin_db.cursor() as cur:
        cur.execute(
            "SELECT c.relname, "
            "       coalesce((SELECT option_value FROM pg_options_to_table(c.reloptions) "
            "                  WHERE option_name = 'security_invoker'), 'false') "
            "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = 'public' AND c.relkind = 'v' ORDER BY 1")
        rows = cur.fetchall()
    assert rows, "no views found — the check would pass vacuously"
    definer = sorted(name for name, invoker in rows if invoker != "true")
    assert definer == [], (
        f"view(s) running as their owner rather than the caller: {definer}. "
        f"Classify each in PHASE_3B_VIEW_SECURITY_REVIEW.md and set "
        f"security_invoker unless there is a stated reason not to.")
