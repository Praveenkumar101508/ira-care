"""D3 items 8, 9 and 11 — the three properties nothing had looked at.

The P12-01 audit measured which callers bind. That is a question about one
statement at one moment. These are the three ways a binding can be correct at
every call site and still be wrong in production:

  8.  a scheduled job inherits a workspace from whatever ran before it;
  9.  a pooled connection carries `app.workspace_id` to its next borrower;
  11. a read the policy emptied is mistaken for a read that found nothing.

Item 11 is the one with teeth, because it is not a bug in any single place. It
is a property of the whole design: `049` makes an unbound read return zero rows
WITHOUT erroring, so every "no results" in the system is ambiguous between "you
have no data" and "nobody bound your workspace". The tests below make that
ambiguity visible and prove where the system resolves it.

Run as `ira_app` — non-superuser, NOBYPASSRLS. A superuser bypasses RLS
entirely, so proving any of this on one would prove nothing (D3 item 14).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — workspace hazard tests skipped")

_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


def _workspace_of(tenant: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant}")


@pytest.fixture
async def pool():
    import utils.db as db_module

    # max_size=1 is the whole design of the pool-reuse test: with one
    # connection, the second borrower is GUARANTEED to get the first
    # borrower's. A larger pool would let the test pass by luck.
    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=1)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        try:
            await asyncio.wait_for(created.close(), timeout=10.0)
        except Exception:  # noqa: BLE001
            created.terminate()


async def _make_tenant(conn, label: str):
    """One tenant, one organisation, one default workspace, one member."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws = _workspace_of(tenant_id)
    slug = f"p1201h-{tenant_id.hex[:10]}"
    await conn.execute(
        "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
        tenant_id, f"P12-01 {label} {slug}", slug)
    await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
    await conn.execute("SELECT set_config('app.user_id',$1,false)", str(user_id))
    org = await conn.fetchval(
        "INSERT INTO organisations (tenant_id,name) VALUES ($1,'P12-01') RETURNING id",
        tenant_id)
    await conn.execute(
        "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default,status) "
        "VALUES ($1,$2,$3,'Default','default',TRUE,'active')", ws, tenant_id, org)
    await conn.execute(
        "INSERT INTO users (id,email,display_name,status) "
        "VALUES ($1,$2,'P12-01 hazard','active')",
        user_id, f"p1201h-{user_id.hex[:12]}@example.invalid")
    await conn.execute(
        "INSERT INTO tenant_memberships (tenant_id,user_id,role,status,joined_at) "
        "VALUES ($1,$2,'owner','active',now())", tenant_id, user_id)
    await conn.execute(
        "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role,status) "
        "VALUES ($1,$2,$3,'workspace_admin','active')", tenant_id, ws, user_id)
    await conn.execute("SELECT set_config('app.tenant_id','',false)")
    await conn.execute("SELECT set_config('app.user_id','',false)")
    return {"tenant_id": tenant_id, "user_id": user_id, "workspace_id": ws}


def _principal(t, workspace_id=None):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=t["user_id"], tenant_id=t["tenant_id"], session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
        workspace_id=workspace_id if workspace_id is not None else t["workspace_id"],
        workspace_role="workspace_admin")


@pytest.fixture
async def two_tenants(pool):
    async with pool.acquire() as conn:
        a = await _make_tenant(conn, "A")
        b = await _make_tenant(conn, "B")
    yield a, b

    # See `test_zz_no_fixture_residue.py`: a fixture tenant left holding
    # memberships does not fail this suite, it fails the rollback job three
    # stages later with an error naming `035_memberships_down.sql` and nothing
    # pointing back here.
    async with pool.acquire() as conn:
        for t in (a, b):
            await conn.execute("SELECT set_config('app.tenant_id',$1,false)",
                               str(t["tenant_id"]))
            await conn.execute(
                "DELETE FROM workspace_memberships WHERE tenant_id = $1",
                t["tenant_id"])
            await conn.execute(
                "DELETE FROM tenant_memberships WHERE tenant_id = $1",
                t["tenant_id"])
        await conn.execute("SELECT set_config('app.tenant_id','',false)")


# ── D3 item 9 — a pooled connection must not carry a workspace forward ──────


async def test_a_reused_connection_carries_no_workspace_from_the_previous_holder(
    pool, two_tenants
):
    """Workspace A's transaction, released, then reacquired with nothing bound.

    `workspace_connection` sets all three GUCs with `set_config(..., true)` —
    transaction-local — so they are discarded at COMMIT rather than persisting
    on the connection. This is the test that the third argument is really
    `true`, asked of the connection rather than of the source.

    The pool holds exactly ONE connection, so the second block below is not
    merely likely to reuse the first block's connection; it must.
    """
    from core.identity.db_context import workspace_connection
    from utils.db import acquire

    a, _ = two_tenants

    async with workspace_connection(_principal(a)) as conn:
        bound = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
        assert bound == str(a["workspace_id"])
        # The backend PID, not `id(conn)`: asyncpg hands out a fresh
        # `PoolConnectionProxy` object on every acquire, so object identity
        # says nothing about whether the underlying socket was reused. The
        # server process is the thing that either carried state forward or did
        # not.
        first = await conn.fetchval("SELECT pg_backend_pid()")

    async with acquire() as conn:
        assert await conn.fetchval("SELECT pg_backend_pid()") == first, (
            "the pool handed out a different connection, so this test did not "
            "exercise reuse and proves nothing about leakage"
        )
        leaked_ws = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
        leaked_tenant = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")

    assert not leaked_ws, (
        f"a connection returned to the pool still carried "
        f"app.workspace_id={leaked_ws!r}. The next borrower would read and "
        f"write another workspace's data while believing itself unbound — and "
        f"under `049` it would look like it was working."
    )
    assert not leaked_tenant, (
        f"the same connection still carried app.tenant_id={leaked_tenant!r}."
    )


async def test_workspace_b_does_not_inherit_workspace_a_through_the_pool(
    pool, two_tenants
):
    """The sharper form: A, then B, on the same physical connection.

    An empty GUC is one way to be safe. The way that matters is that B's
    transaction sees B's workspace and only B's — a stale value that happened
    to be overwritten in the right order would pass the test above.
    """
    from core.identity.db_context import workspace_connection

    a, b = two_tenants

    async with workspace_connection(_principal(a)) as conn:
        await conn.execute(
            "INSERT INTO tasks (title, tenant_id, workspace_id) "
            "VALUES ('A only', "
            "        NULLIF(current_setting('app.tenant_id', true), '')::uuid, "
            "        NULLIF(current_setting('app.workspace_id', true), '')::uuid)")

    async with workspace_connection(_principal(b)) as conn:
        seen = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
        assert seen == str(b["workspace_id"]), (
            f"B's transaction is bound to {seen!r}, not to B's workspace "
            f"{b['workspace_id']}."
        )
        rows = await conn.fetch("SELECT title FROM tasks WHERE title = 'A only'")

    assert rows == [], (
        "workspace B read a row written by workspace A on the same pooled "
        "connection. This is cross-workspace disclosure within one tenant "
        "boundary, which is precisely what 049 exists to prevent."
    )


# ── D3 item 8 — a scheduled job must not inherit a stale workspace ──────────


async def test_a_background_job_derives_its_workspace_rather_than_inheriting_one(
    pool, two_tenants
):
    """The scheduler path, run immediately after another workspace's work.

    `default_workspace_job_principal` reads the tenant's canonical default
    workspace through a tenant-bound connection. The hazard it must not have is
    picking up whatever the previous request left on the connection — which,
    with a single-connection pool and a request immediately before it, is the
    strongest form of that hazard available.
    """
    from core.identity.background import default_workspace_job_principal
    from core.identity.db_context import workspace_connection

    a, b = two_tenants

    # A request for tenant A runs first and releases its connection.
    async with workspace_connection(_principal(a)) as conn:
        await conn.fetchval("SELECT current_setting('app.workspace_id', true)")

    # The job for tenant B now runs on that same connection.
    job = await default_workspace_job_principal(b["tenant_id"], job="p1201-probe")

    assert job.workspace_id == b["workspace_id"], (
        f"the job resolved workspace {job.workspace_id}, not tenant B's default "
        f"{b['workspace_id']}. A job that inherits the previous request's "
        f"workspace writes the tenant's housekeeping into somebody else's."
    )
    assert job.tenant_id == b["tenant_id"]


async def test_a_job_refuses_rather_than_falling_back_when_there_is_no_default(
    pool, two_tenants
):
    """No canonical workspace means no work, not a guess.

    This is the other half of item 8. A job that inherits nothing but silently
    picks *a* workspace when its own is missing has the same failure with an
    extra step.
    """
    from core.identity.background import MissingJobContext, default_workspace_job_principal

    # A tenant with NO workspace at all, rather than one whose default flag was
    # cleared: `033`'s one-default constraint is DEFERRED to COMMIT, so
    # `UPDATE workspaces SET is_default = FALSE` raises "0 active default
    # workspaces; exactly one is required" and the test would be asserting that
    # the database refused the setup, not that the job refused the work.
    tenant_id = uuid.uuid4()
    slug = f"p1201h-nows-{tenant_id.hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            tenant_id, f"P12-01 no-workspace {slug}", slug)

    with pytest.raises(MissingJobContext):
        await default_workspace_job_principal(tenant_id, job="p1201-probe")


# ── D3 item 11 — an RLS-emptied result must not read as success ─────────────


async def test_an_unbound_read_returns_zero_rows_without_erroring(pool, two_tenants):
    """The hazard itself, stated as a fact rather than as a warning.

    Everything else in this file exists because of this one behaviour, so it is
    asserted directly: the same SELECT, over the same rows, returns them when
    bound and returns nothing when not — and never raises. Any caller that
    treats `[]` as "no data" is, on this schema, also treating "no workspace
    bound" as "no data".
    """
    from core.identity.db_context import workspace_connection
    from utils.db import acquire

    a, _ = two_tenants
    title = f"P12-01 {uuid.uuid4().hex[:8]}"

    async with workspace_connection(_principal(a)) as conn:
        await conn.execute(
            "INSERT INTO tasks (title, tenant_id, workspace_id) "
            "VALUES ($1, "
            "        NULLIF(current_setting('app.tenant_id', true), '')::uuid, "
            "        NULLIF(current_setting('app.workspace_id', true), '')::uuid)",
            title)
        bound_rows = await conn.fetch("SELECT id FROM tasks WHERE title = $1", title)

    assert len(bound_rows) == 1

    async with acquire() as conn:
        # No exception, no warning, no indication of any kind.
        unbound_rows = await conn.fetch("SELECT id FROM tasks WHERE title = $1", title)

    assert unbound_rows == [], (
        "an unbound connection read a workspace-scoped row, so 049's predicate "
        "is not in force and every other test in this file is measuring "
        "nothing."
    )


async def test_an_unbound_write_fails_loudly_rather_than_silently(pool, two_tenants):
    """Reads fail silently; writes must not.

    `049`'s header says so and this is the executable form. It matters because
    it is the asymmetry the whole design leans on: a caller that only reads can
    be wrong for a long time, and a caller that writes finds out at once.
    """
    from utils.db import acquire

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (title, tenant_id, workspace_id) "
                "VALUES ('unbound', NULL, NULL)")


async def test_erasure_removes_workspace_scoped_notifications(pool, two_tenants):
    """The gap P12-01 found: a tenant erasure that leaves rows and says done.

    `notifications` carries `workspace_id` with the policy

        tenant_id = app.tenant_id
        AND (workspace_id IS NULL OR workspace_id = app.workspace_id)

    so a tenant-bound DELETE removes only the NULL-workspace rows. Before this
    batch, `erase_postgres` deleted it in the tenant pass alone, and a
    workspace-scoped notification survived a full account erasure while the
    result reported success.

    Two rows are written — one tenant-wide, one workspace-scoped — and BOTH
    must be gone. Counted directly rather than trusted from the returned
    number, because the returned number was already right about the rows it
    knew about.

    Both are the account's own (`owner_user_id`, set as the notifier sets it):
    since 095 (ADR-0010), erasure deletes only the rows a person owns, and keeps
    a row with no owner.
    """
    from core.identity.db_context import workspace_connection
    from core.privacy.erasure import erase_postgres

    a, _ = two_tenants
    p = _principal(a)

    async with workspace_connection(p) as conn:
        await conn.execute(
            "INSERT INTO notifications (id,tenant_id,workspace_id,category,title,body,"
            "                           priority,owner_user_id) "
            "VALUES ($1, NULLIF(current_setting('app.tenant_id', true), '')::uuid, "
            "        NULLIF(current_setting('app.workspace_id', true), '')::uuid, "
            "        'system','P12-01 scoped','body','info', "
            "        NULLIF(current_setting('app.user_id', true), '')::uuid)", uuid.uuid4())
        await conn.execute(
            "INSERT INTO notifications (id,tenant_id,workspace_id,category,title,body,"
            "                           priority,owner_user_id) "
            "VALUES ($1, NULLIF(current_setting('app.tenant_id', true), '')::uuid, "
            "        NULL, 'system','P12-01 tenant-wide','body','info', "
            "        NULLIF(current_setting('app.user_id', true), '')::uuid)", uuid.uuid4())

    result = await erase_postgres(p, scope="account")
    assert result.ok, f"erasure reported failure: {result.error}"

    # Counted on a connection bound to the workspace, because an unbound count
    # cannot see a workspace-scoped row — and would therefore report the
    # leftover row as absent, reproducing the very confusion under test.
    async with workspace_connection(p) as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM notifications WHERE tenant_id = $1", a["tenant_id"])

    assert remaining == 0, (
        f"{remaining} notification(s) survived an account erasure that reported "
        f"success. A deletion request that leaves data behind and says it "
        f"succeeded is worse than one that fails, because nobody looks again."
    )


async def test_the_workspace_optional_set_matches_the_live_policies(pool):
    """`WORKSPACE_OPTIONAL_TABLES` is derived from the schema, not remembered.

    The bug above existed because a table's policy shape and erasure's model of
    it disagreed, silently. This asks `pg_policies` which tables actually have
    the `workspace_id IS NULL OR …` shape and holds the constant to it, so the
    next table to adopt that shape cannot join the schema and miss a pass.
    """
    from core.privacy.erasure import WORKSPACE_OPTIONAL_TABLES, WORKSPACE_SCOPED_TABLES

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT tablename FROM pg_policies "
            " WHERE schemaname = 'public' "
            "   AND (qual LIKE '%workspace_id IS NULL%' "
            "     OR with_check LIKE '%workspace_id IS NULL%')")
    optional = {r["tablename"] for r in rows}

    assert optional == set(WORKSPACE_OPTIONAL_TABLES), (
        f"the schema's workspace-optional tables are {sorted(optional)}, and "
        f"erasure believes they are {sorted(WORKSPACE_OPTIONAL_TABLES)}. A "
        f"table in the first set and not the second keeps its workspace-scoped "
        f"rows through an account erasure."
    )
    assert not (optional & set(WORKSPACE_SCOPED_TABLES)), (
        "a table cannot be both strictly workspace-scoped and workspace-optional"
    )
