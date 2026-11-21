"""P2-05 — `totp_secrets` gets a tenant, and the application can actually reach it.

Two findings, and the second is the one that mattered.

── The recorded finding ────────────────────────────────────────────────────

`totp_secrets` had `user_id`, RLS and FORCE RLS from 036, but no tenant. 052
adds one, and — critically — **ANDs** the tenant predicate onto the user
predicate rather than adding a second permissive policy beside it. PostgreSQL
combines permissive policies with OR, so a separate `tenant_isolation` policy
would make a row visible when EITHER predicate held, handing every tenant admin
a read on every member's second factor. That is the exact bug 036's header says
it exists to prevent, and `test_a_tenant_admin_cannot_read_a_members_secret`
below is the assertion that keeps it prevented.

── The finding underneath it ───────────────────────────────────────────────

Every production caller reached the table through a bare `acquire()`. Under
FORCE RLS with policies on `app.user_id`, an unbound connection leaves the GUC
NULL, `user_id = NULL` is NULL rather than true, and the query matches **zero
rows** — silently, with no error.

So `auth/routes.py::login` did this:

    _totp_row = await conn.fetchrow(
        "SELECT secret FROM totp_secrets WHERE username=$1 AND enabled=TRUE", ...)
    if _totp_row:
        ...demand a code...

`_totp_row` was always None. **The second factor was never demanded.** Anyone
who had enrolled TOTP was signing in on a password alone, and the symptom of a
fail-closed database control reached through an unbound connection is not an
error anybody notices — it is a feature that quietly does nothing.

`/auth/totp/enroll` was broken in the same family: it wrote through a bare
connection (refused by the INSERT's WITH CHECK) and used
`ON CONFLICT (username)`, naming a constraint 036 had already dropped.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

TA = uuid.UUID("11111111-1111-1111-1111-1111111111a5")
TB = uuid.UUID("22222222-2222-2222-2222-2222222222a5")
UA = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000a5a1")
UB = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000a5b1")
UADM = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000a5ad")


@pytest.fixture
async def seeded():
    """Two tenants, each with a user called `alice`, each with an enrolment.

    Seeded on the ADMIN dsn because a superuser bypasses RLS — the fixture has
    to be able to create the very rows the policies are about to hide.
    """
    admin = await asyncpg.connect(ADMIN_DSN)
    app = await asyncpg.connect(DSN)
    try:
        await admin.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,'A','p205-a'),($2,'B','p205-b')"
            " ON CONFLICT DO NOTHING", TA, TB)
        # `legacy_username` is left NULL: it carries a GLOBAL unique constraint,
        # so two users cannot share one. That is a real limitation of the legacy
        # mapping column, documented rather than redesigned here — and it does
        # not affect what this file proves, because `totp_secrets.username` is
        # separately non-unique (036 dropped its PRIMARY KEY precisely so two
        # tenants could each hold an `alice`).
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status)"
            " VALUES ($1,'A','local','active'),($2,'B','local','active'),"
            "        ($3,'Adm','local','active') ON CONFLICT DO NOTHING",
            UA, UB, UADM)
        await admin.execute("DELETE FROM totp_secrets WHERE username='p205-alice'")
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id)"
            " VALUES ('p205-alice','SECRET-A',TRUE,$1,$2),"
            "        ('p205-alice','SECRET-B',TRUE,$3,$4)", UA, TA, UB, TB)
        yield app
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username='p205-alice'")
        await admin.close()
        await app.close()


async def _as(conn, tenant, user, sql, *args):
    """Run `sql` with the GUCs `tenant_connection()` binds, then discard them."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant))
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(user))
        return await conn.fetch(sql, *args)


# ─────────────────────────────────────────────────────────────────────────────
# The boundary
# ─────────────────────────────────────────────────────────────────────────────


async def test_each_tenants_alice_reads_only_her_own_secret(seeded):
    """Positive control and the boundary in one. A fix that hid everything
    would satisfy every negative assertion below."""
    a = await _as(seeded, TA, UA, "SELECT secret FROM totp_secrets WHERE username='p205-alice'")
    b = await _as(seeded, TB, UB, "SELECT secret FROM totp_secrets WHERE username='p205-alice'")
    assert [r["secret"] for r in a] == ["SECRET-A"]
    assert [r["secret"] for r in b] == ["SECRET-B"]


@pytest.mark.parametrize("tenant,user", [(TB, UA), (TA, UB)])
async def test_cross_tenant_select_is_denied(seeded, tenant, user):
    rows = await _as(seeded, tenant, user,
                     "SELECT secret FROM totp_secrets WHERE username='p205-alice'")
    assert rows == [], "a user reached a second factor across the tenant boundary"


async def test_a_tenant_admin_cannot_read_a_members_secret(seeded):
    """The property 036 exists for, and the one an OR-combined tenant policy
    would destroy.

    This is why 052's predicates are ANDed. A permissive `tenant_isolation`
    policy beside `user_private` — the shape `delegated_users` and
    `user_sessions` use, and the obvious thing to copy — makes this assertion
    fail immediately.
    """
    rows = await _as(seeded, TA, UADM,
                     "SELECT secret FROM totp_secrets WHERE username='p205-alice'")
    assert rows == [], "a tenant admin read another member's TOTP secret"


async def test_the_same_username_in_another_tenant_does_not_cross(seeded):
    """Both rows are `p205-alice`. The name is not the boundary; the owner is."""
    rows = await _as(seeded, TA, UA, "SELECT secret FROM totp_secrets")
    assert len(rows) == 1 and rows[0]["secret"] == "SECRET-A"


# ─────────────────────────────────────────────────────────────────────────────
# Missing context fails closed
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_unbound_connection_sees_nothing(seeded):
    """Exactly what a bare `acquire()` gives, and exactly why the login path
    silently stopped demanding a second factor."""
    rows = await seeded.fetch("SELECT secret FROM totp_secrets")
    assert rows == []


@pytest.mark.parametrize("guc,value", [("app.tenant_id", TA), ("app.user_id", UA)])
async def test_half_the_context_is_not_enough(seeded, guc, value):
    async with seeded.transaction():
        await seeded.execute(f"SELECT set_config('{guc}',$1,true)", str(value))
        rows = await seeded.fetch("SELECT secret FROM totp_secrets")
    assert rows == [], f"{guc} alone was enough to reach a second factor"


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────


async def test_cross_tenant_update_changes_nothing(seeded):
    await _as(seeded, TB, UA,
              "UPDATE totp_secrets SET secret='STOLEN' WHERE username='p205-alice'")
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        got = await admin.fetchval(
            "SELECT secret FROM totp_secrets WHERE tenant_id=$1", TB)
    finally:
        await admin.close()
    assert got == "SECRET-B"


async def test_cross_tenant_delete_removes_nothing(seeded):
    await _as(seeded, TB, UA,
              "DELETE FROM totp_secrets WHERE username='p205-alice'")
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        n = await admin.fetchval(
            "SELECT count(*) FROM totp_secrets WHERE username='p205-alice'")
    finally:
        await admin.close()
    assert n == 2


async def test_a_request_supplied_tenant_id_cannot_create_authority(seeded):
    """`tenant_id` in the row body is not authority — the WITH CHECK is.

    The application derives both owner columns from the resolved Principal, but
    the database must refuse a forged one regardless, because "the application
    always passes the right value" is a claim about code rather than a control.
    """
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await _as(seeded, TA, UA,
                  "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id)"
                  " VALUES ('p205-mallory','X',TRUE,$1,$2)", UB, TB)


async def test_own_tenant_write_still_works(seeded):
    """Positive control for the three above."""
    await _as(seeded, TA, UA,
              "UPDATE totp_secrets SET secret='ROTATED' WHERE username='p205-alice'")
    rows = await _as(seeded, TA, UA,
                     "SELECT secret FROM totp_secrets WHERE username='p205-alice'")
    assert [r["secret"] for r in rows] == ["ROTATED"]


# ─────────────────────────────────────────────────────────────────────────────
# The schema matches what 052 declares
# ─────────────────────────────────────────────────────────────────────────────


async def test_rls_is_enabled_and_forced(seeded):
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await admin.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE oid='public.totp_secrets'::regclass")
    finally:
        await admin.close()
    assert row["relrowsecurity"] and row["relforcerowsecurity"]


async def test_there_are_exactly_four_policies_and_each_names_both_gucs():
    """Five is the dangerous count, not three.

    Permissive policies combine with OR, so an extra policy can only WIDEN
    access — a fifth naming only `app.tenant_id` is precisely the tenant-admin
    read this migration exists to keep closed.
    """
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await admin.fetch(
            "SELECT polname,"
            " coalesce(pg_get_expr(polqual,polrelid),'')"
            " || coalesce(pg_get_expr(polwithcheck,polrelid),'') AS expr"
            " FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass")
    finally:
        await admin.close()
    assert len(rows) == 4, f"expected 4 policies, found {[r['polname'] for r in rows]}"
    for r in rows:
        assert "app.user_id" in r["expr"], f"{r['polname']} does not constrain app.user_id"
        assert "app.tenant_id" in r["expr"], f"{r['polname']} does not constrain app.tenant_id"


async def test_tenant_id_is_not_null_with_a_foreign_key():
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        nullable = await admin.fetchval(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name='totp_secrets' AND column_name='tenant_id'")
        fk = await admin.fetchval(
            "SELECT count(*) FROM pg_constraint c JOIN pg_class r ON r.oid=c.confrelid"
            " WHERE c.conrelid='public.totp_secrets'::regclass AND r.relname='tenants'")
    finally:
        await admin.close()
    assert nullable == "NO"
    assert fk == 1


# ─────────────────────────────────────────────────────────────────────────────
# Static guard — the next caller inherits the rule
# ─────────────────────────────────────────────────────────────────────────────

#: Modules allowed to name `totp_secrets` in SQL. Every one of them must reach
#: it through a bound connection; the guard below checks that, not this list.
TOTP_SQL_MODULES = {
    "auth/routes.py", "api/routes/totp.py", "security/totp.py",
}


def test_no_module_reaches_totp_secrets_through_an_unbound_connection():
    """The defect, as a rule rather than as three fixed call sites.

    `acquire()` binds no GUCs. Against a table with FORCE RLS that is not an
    error — it is zero rows, silently, which is how a second factor stopped
    being demanded without anything appearing in a log.

    Checked **per `async with` block**, not per module. The first version of
    this guard asked whether the file mentioned `tenant_connection` anywhere,
    and a planted regression walked straight past it: `security/totp.py` has two
    functions, so one correctly-bound call site masked an unbound one in the
    same file. A module-level answer to a call-site question is not a guard.
    """
    import ast
    import pathlib

    ira = pathlib.Path(__file__).resolve().parents[2]
    offenders = []

    def _opens_unbound(node) -> bool:
        """Is this `async with acquire() [as ...]`?"""
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call):
                fn = call.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "acquire":
                    return True
        return False

    for path in sorted(ira.rglob("*.py")):
        rel = path.relative_to(ira).as_posix()
        if rel.startswith(("tests/", "scripts/", ".venv/")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "totp_secrets" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncWith, ast.With)):
                continue
            if not _opens_unbound(node):
                continue
            # Does the body of this unbound block name the table? Only string
            # constants count — a comment inside the block is not a query.
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and "totp_secrets" in sub.value:
                    offenders.append(f"{rel}:{node.lineno}")
                    break

    assert not offenders, (
        f"these call sites query totp_secrets on an unbound connection: "
        f"{offenders}. Under FORCE RLS that matches zero rows silently — it "
        f"does not raise. Use tenant_connection(principal), which binds "
        f"app.user_id and app.tenant_id.")


def test_the_guard_can_see_the_shape_it_forbids():
    """A guard that matched nothing would be permanently green."""

    import ast

    planted = ast.parse(
        'async def f():\n'
        '    async with acquire() as conn:\n'
        '        await conn.fetch("SELECT secret FROM totp_secrets")\n')
    hit = False
    for node in ast.walk(planted):
        if isinstance(node, ast.AsyncWith) and any(
                getattr(i.context_expr, "func", None) is not None
                and getattr(i.context_expr.func, "id", None) == "acquire"
                for i in node.items):
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and "totp_secrets" in sub.value:
                    hit = True
    assert hit, "the guard cannot see an unbound acquire() querying totp_secrets"
