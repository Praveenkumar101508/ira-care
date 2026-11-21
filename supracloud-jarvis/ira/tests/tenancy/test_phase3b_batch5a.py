"""Phase 3B Batch 5A — verified workspace context.

Batch 5A adds no migration and forces no workspace RLS. What it adds is the
mechanism by which a *verified* workspace reaches a database transaction, and
the property under test throughout is the one that decides whether Batch 5C is
safe:

    **A missing workspace fails. It never resolves to a default.**

Almost every test below is a way of asking that question — of the selection
service, of the connection helper, of the cache keys, of the job payloads. The
failure mode this suite exists to prevent is not an attacker forging a
workspace id; it is an ordinary `or default_workspace` written by somebody in a
hurry, which turns every unscoped call site into a silent write into a
workspace nobody chose.

Runs against a real PostgreSQL as `ira_app` — non-superuser, NOBYPASSRLS,
owning nothing — because the membership tables the selection service reads are
themselves under forced RLS, and a superuser harness would prove nothing about
what the application role can actually see.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
if not DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN not set — Batch 5A tests skipped",
                allow_module_level=True)

from core.identity.background import (  # noqa: E402
    JobContext,
    MissingJobContext,
    job_context,
    preserve_for_retry,
)
from core.identity.db_context import MissingWorkspaceContext  # noqa: E402
from core.identity.namespacing import (  # noqa: E402
    redis_workspace_key,
    workspace_object_path,
    workspace_prefix,
)
from core.identity.principal import Principal  # noqa: E402
from core.identity.workspace import (  # noqa: E402
    WorkspaceSelectionError,
    require_capability,
    select_default_workspace,
    select_workspace,
    workspace_capabilities,
)
from core.editions import EDITION_CLOUD  # noqa: E402

NOW = "2026-08-05T00:00:00Z"


def _asyncpg_dsn(dsn: str) -> str:
    return dsn


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — a tenant with two workspaces and four users in different states
# ─────────────────────────────────────────────────────────────────────────────


class World:
    """Identifiers for one fixture world. Plain attributes, no magic."""


@pytest.fixture
def world():
    """Build a tenant, two workspaces and users in every membership state.

    Everything is created through a superuser connection because creating the
    fixture is not the thing under test — reading it back as `ira_app` is. The
    rows are removed on teardown in dependency order.
    """
    admin_dsn = os.getenv("IRA_TEST_ADMIN_DSN", "")
    if not admin_dsn:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")

    w = World()
    w.tenant = uuid.uuid4()
    w.other_tenant = uuid.uuid4()
    w.ws_default = uuid.uuid4()
    w.ws_second = uuid.uuid4()
    w.ws_other_tenant = uuid.uuid4()
    w.ws_archived = uuid.uuid4()
    w.member = uuid.uuid4()          # active everywhere
    w.non_member = uuid.uuid4()      # tenant member, no workspace membership
    w.removed = uuid.uuid4()         # workspace membership removed
    w.suspended_tenant = uuid.uuid4()  # tenant membership suspended
    w.viewer = uuid.uuid4()          # active, role=viewer

    async def build():
        conn = await asyncpg.connect(admin_dsn)
        try:
            for t, name in ((w.tenant, "b5a"), (w.other_tenant, "b5a-other")):
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, $2)", t, f"{name}-{t.hex[:8]}"
                )
            for u in (w.member, w.non_member, w.removed, w.suspended_tenant, w.viewer):
                await conn.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)", u, f"{u.hex[:12]}@example.test"
                )
            # The default workspace comes from the substrate function rather
            # than a hand-written INSERT: it creates the organisation too
            # (`workspaces.organisation_id` is NOT NULL), and using the real
            # provisioning path means the fixture cannot drift from what a
            # genuinely provisioned tenant looks like.
            w.ws_default = await conn.fetchval(
                "SELECT provision_tenant_substrate($1)", w.tenant
            )
            w.ws_other_tenant = await conn.fetchval(
                "SELECT provision_tenant_substrate($1)", w.other_tenant
            )
            org = await conn.fetchval(
                "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant
            )
            await conn.execute(
                # `archived_at` is required by the `workspaces_archived_at_agrees`
                # lifecycle CHECK from `039` — an archived workspace that cannot
                # say when it was archived is not a state the schema allows.
                """INSERT INTO workspaces
                     (id, tenant_id, organisation_id, name, slug, is_default,
                      status, archived_at)
                   VALUES ($1,$2,$3,'Second','second',false,'active',NULL),
                          ($4,$2,$3,'Archived','archived',false,'archived',NOW())""",
                w.ws_second, w.tenant, org, w.ws_archived,
            )
            for u, status in (
                (w.member, "active"), (w.non_member, "active"), (w.removed, "active"),
                (w.viewer, "active"), (w.suspended_tenant, "suspended"),
            ):
                await conn.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                       VALUES ($1,$2,'member',$3)""",
                    w.tenant, u, status,
                )
            for u, ws, role, status in (
                (w.member, w.ws_default, "member", "active"),
                (w.member, w.ws_second, "workspace_admin", "active"),
                (w.viewer, w.ws_default, "viewer", "active"),
                (w.removed, w.ws_default, "member", "removed"),
                (w.suspended_tenant, w.ws_default, "member", "active"),
            ):
                # `removed_at` must agree with `status` — the
                # `workspace_memberships_status_removed_agree` CHECK from `040`
                # refuses a membership that claims to be removed without saying
                # when, and one that carries a removal timestamp while active.
                await conn.execute(
                    """INSERT INTO workspace_memberships
                         (tenant_id, workspace_id, user_id, role, status, removed_at)
                       VALUES ($1,$2,$3,$4,$5,
                               CASE WHEN $5 = 'removed' THEN NOW() ELSE NULL END)""",
                    w.tenant, ws, u, role, status,
                )
        finally:
            await conn.close()

    async def teardown():
        conn = await asyncpg.connect(admin_dsn)
        try:
            for t in (w.tenant, w.other_tenant):
                await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", t)
                # The one-default constraint trigger is DEFERRABLE, so the
                # workspaces go before the organisation and the tenant last.
                await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenants WHERE id=$1", t)
            for u in (w.member, w.non_member, w.removed, w.suspended_tenant, w.viewer):
                await conn.execute("DELETE FROM users WHERE id=$1", u)
        finally:
            await conn.close()

    _run(build())
    yield w
    _run(teardown())


def _run(coro):
    """Run a coroutine on a fresh loop — these tests are sync fixtures around
    an async data layer, and borrowing pytest-asyncio's loop from a sync fixture
    is more fragile than owning one."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def principal_for(user_id: uuid.UUID, tenant_id: uuid.UUID, **kw) -> Principal:
    return Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=uuid.uuid4(),
        edition=kw.pop("edition", EDITION_CLOUD),
        **kw,
    )


async def _bound(conn, principal):
    """Bind the tenant GUC the way `tenant_connection` would."""
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id))
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(principal.user_id))


def with_tenant_conn(principal, fn):
    """Run `fn(conn)` inside a transaction bound to the principal's tenant."""

    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                await _bound(conn, principal)
                return await fn(conn)
            finally:
                await tx.rollback()
        finally:
            await conn.close()

    return _run(go())


# ─────────────────────────────────────────────────────────────────────────────
# 1. Selection
# ─────────────────────────────────────────────────────────────────────────────


def test_valid_workspace_selection_succeeds(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    assert ctx.workspace_id == world.ws_default
    assert ctx.tenant_id == world.tenant
    assert ctx.workspace_role == "member"
    assert ctx.is_default is True
    assert ctx.workspace_membership_id is not None
    assert ctx.membership_id is not None


def test_selection_applied_to_principal_carries_the_workspace(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_second))
    scoped = ctx.apply_to(p)
    assert scoped.workspace_id == world.ws_second
    assert scoped.workspace_role == "workspace_admin"
    assert scoped.has_workspace
    # The original is untouched — Principal is frozen, so a selection cannot
    # retroactively widen a principal another coroutine is already holding.
    assert p.workspace_id is None


def test_cross_tenant_workspace_selection_fails(world):
    """The headline case: a real workspace id, belonging to somebody else."""
    p = principal_for(world.member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_other_tenant))
    assert e.value.reason == "workspace_not_in_tenant"


def test_a_cross_tenant_id_is_indistinguishable_from_a_nonexistent_one(world):
    """Otherwise the error message is a tenant-enumeration oracle."""
    p = principal_for(world.member, world.tenant)

    def attempt(ws):
        with pytest.raises(WorkspaceSelectionError) as e:
            with_tenant_conn(p, lambda c: select_workspace(c, p, ws))
        return str(e.value), e.value.reason

    cross_msg, cross_reason = attempt(world.ws_other_tenant)
    absent_msg, absent_reason = attempt(uuid.uuid4())
    assert cross_msg == absent_msg, "public messages must not distinguish the two"
    assert cross_reason == absent_reason == "workspace_not_in_tenant"


def test_missing_workspace_membership_fails(world):
    p = principal_for(world.non_member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    assert e.value.reason == "no_workspace_membership"


def test_removed_workspace_membership_fails(world):
    p = principal_for(world.removed, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    assert e.value.reason == "workspace_membership_inactive"


def test_suspended_tenant_membership_fails_even_with_an_active_workspace_row(world):
    """Revocation cascades down. `040`'s generated column resolves it in the
    database, so an active workspace membership under a suspended tenant
    membership grants nothing — and this asserts the cascade, not the Python."""
    p = principal_for(world.suspended_tenant, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    assert e.value.reason in ("tenant_membership_inactive", "workspace_membership_inactive")


def test_a_user_with_no_tenant_membership_fails(world):
    stranger = principal_for(uuid.uuid4(), world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(stranger, lambda c: select_workspace(c, stranger, world.ws_default))
    assert e.value.reason == "no_tenant_membership"


def test_archived_workspace_cannot_be_selected(world):
    p = principal_for(world.member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_archived))
    # Either arm is correct: no membership was created for the archived
    # workspace, and its status is not selectable. Both are refusals.
    assert e.value.reason in ("workspace_not_selectable", "no_workspace_membership")


@pytest.mark.parametrize(
    "bad", ["", "not-a-uuid", "../../etc/passwd", None, 12345, "' OR '1'='1"]
)
def test_invalid_workspace_identifiers_fail_safely(world, bad):
    """No stack trace, no 500, no SQL — the same refusal as any other."""
    p = principal_for(world.member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_workspace(c, p, bad))
    assert e.value.reason == "invalid_workspace_id"


def test_there_is_no_first_workspace_fallback(world):
    """The tenant has two active workspaces and `non_member` is a member of
    neither. If any code path picked "the first one", this would succeed."""
    p = principal_for(world.non_member, world.tenant)
    for ws in (world.ws_default, world.ws_second):
        with pytest.raises(WorkspaceSelectionError):
            with_tenant_conn(p, lambda c: select_workspace(c, p, ws))


def test_default_selection_must_be_asked_for(world):
    p = principal_for(world.member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_default_workspace(c, p, allow_default=False))
    assert e.value.reason == "default_selection_not_permitted"


def test_default_selection_still_verifies_membership(world):
    """`allow_default=True` is permission to *try* the default, not to have it."""
    p = principal_for(world.non_member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: select_default_workspace(c, p, allow_default=True))
    assert e.value.reason == "no_workspace_membership"


def test_default_selection_succeeds_for_a_member(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_default_workspace(c, p, allow_default=True))
    assert ctx.workspace_id == world.ws_default
    assert ctx.is_default is True


def test_a_context_cannot_be_applied_to_a_different_user(world):
    """A verification performed for one user must not be attachable to another."""
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    other = principal_for(world.viewer, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        ctx.apply_to(other)
    assert e.value.reason == "context_user_mismatch"


def test_a_context_cannot_be_applied_across_tenants(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    moved = principal_for(world.member, world.other_tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        ctx.apply_to(moved)
    assert e.value.reason == "context_tenant_mismatch"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Capabilities
# ─────────────────────────────────────────────────────────────────────────────


def test_capabilities_come_from_the_reference_table(world):
    p = principal_for(world.member, world.tenant)
    caps = with_tenant_conn(p, lambda c: workspace_capabilities(c, "viewer"))
    assert caps == {"workspace.read"}
    admin = with_tenant_conn(p, lambda c: workspace_capabilities(c, "workspace_admin"))
    assert "workspace.manage_members" in admin


def test_a_viewer_cannot_write(world):
    p = principal_for(world.viewer, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    scoped = ctx.apply_to(p)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(scoped, lambda c: require_capability(c, scoped, "workspace.write"))
    assert e.value.reason == "capability_denied"


def test_a_member_cannot_manage_members(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    scoped = ctx.apply_to(p)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(
            scoped, lambda c: require_capability(c, scoped, "workspace.manage_members")
        )
    assert e.value.reason == "capability_denied"


def test_a_workspace_admin_can_manage_members(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_second))
    scoped = ctx.apply_to(p)
    with_tenant_conn(scoped, lambda c: require_capability(c, scoped, "workspace.manage_members"))


def test_a_tenant_role_does_not_confer_workspace_capability(world):
    """The tenant owner is not implicitly a workspace administrator."""
    p = principal_for(world.viewer, world.tenant, roles=frozenset({"owner"}), is_owner=True)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    scoped = ctx.apply_to(p)
    assert scoped.is_owner and "owner" in scoped.roles
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(
            scoped, lambda c: require_capability(c, scoped, "workspace.manage_settings")
        )
    assert e.value.reason == "capability_denied"


def test_capability_without_a_workspace_denies(world):
    p = principal_for(world.member, world.tenant)
    with pytest.raises(WorkspaceSelectionError) as e:
        with_tenant_conn(p, lambda c: require_capability(c, p, "workspace.read"))
    assert e.value.reason == "capability_without_workspace"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Database transaction context
# ─────────────────────────────────────────────────────────────────────────────


def test_the_workspace_guc_is_transaction_local(world):
    """`SET LOCAL` semantics, asserted rather than assumed. A workspace that
    survived its transaction would be inherited by the next borrower of the
    pooled connection."""

    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(world.ws_default)
            )
            inside = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
            await tx.rollback()
            after = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
            return inside, after
        finally:
            await conn.close()

    inside, after = _run(go())
    assert inside == str(world.ws_default)
    assert after in ("", None), "the workspace outlived its transaction"


def test_a_failed_transaction_leaves_no_workspace_behind(world):
    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(world.ws_default)
            )
            try:
                await conn.execute("SELECT 1 / 0")
            except Exception:
                pass
            await tx.rollback()
            return await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
        finally:
            await conn.close()

    assert _run(go()) in ("", None)


def test_connection_reuse_does_not_leak_a_workspace(world):
    """One physical connection, two sequential transactions. The second must not
    see the first's workspace."""

    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(world.ws_default)
            )
            await tx.rollback()
            tx2 = conn.transaction()
            await tx2.start()
            seen = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
            await tx2.rollback()
            return seen
        finally:
            await conn.close()

    assert _run(go()) in ("", None)


def test_concurrent_transactions_hold_separate_workspaces(world):
    """Two connections, two workspaces, interleaved. Each must read its own."""

    async def hold(ws, barrier):
        conn = await asyncpg.connect(DSN)
        try:
            tx = conn.transaction()
            await tx.start()
            await conn.execute("SELECT set_config('app.workspace_id', $1, true)", str(ws))
            await barrier.wait()
            seen = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
            await tx.rollback()
            return seen
        finally:
            await conn.close()

    async def go():
        barrier = asyncio.Barrier(2)
        return await asyncio.gather(
            hold(world.ws_default, barrier), hold(world.ws_second, barrier)
        )

    a, b = _run(go())
    assert a == str(world.ws_default)
    assert b == str(world.ws_second)
    assert a != b


def test_require_workspace_raises_rather_than_defaulting(world):
    p = principal_for(world.member, world.tenant)
    assert not p.has_workspace
    with pytest.raises(MissingWorkspaceContext):
        p.require_workspace()


def test_without_workspace_narrows(world):
    p = principal_for(world.member, world.tenant)
    ctx = with_tenant_conn(p, lambda c: select_workspace(c, p, world.ws_default))
    scoped = ctx.apply_to(p)
    narrowed = scoped.without_workspace()
    assert scoped.has_workspace and not narrowed.has_workspace
    assert narrowed.workspace_role is None
    assert narrowed.tenant_id == scoped.tenant_id


def test_the_application_role_is_not_superuser_and_owns_nothing():
    """Every isolation claim above is worthless under a superuser."""

    async def go():
        conn = await asyncpg.connect(DSN)
        try:
            row = await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
            owned = await conn.fetchval(
                """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND pg_get_userbyid(c.relowner)=current_user"""
            )
            return row["rolsuper"], row["rolbypassrls"], owned
        finally:
            await conn.close()

    is_super, bypass, owned = _run(go())
    assert not is_super, "the harness is a superuser; RLS results prove nothing"
    assert not bypass
    assert owned == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cache and object storage
# ─────────────────────────────────────────────────────────────────────────────


def _scoped(tenant, workspace, user=None):
    return Principal(
        user_id=user or uuid.uuid4(),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
        workspace_id=workspace,
    )


def test_the_same_logical_key_differs_across_workspaces():
    tenant = uuid.uuid4()
    a = redis_workspace_key(_scoped(tenant, uuid.uuid4()), "summary")
    b = redis_workspace_key(_scoped(tenant, uuid.uuid4()), "summary")
    assert a != b


def test_the_same_workspace_name_in_two_tenants_differs():
    """Workspace ids are UUIDs, so this is really asserting that the tenant is
    in the key too — which is what keeps tenant deletion a single prefix scan."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    t_a, t_b = uuid.uuid4(), uuid.uuid4()
    assert redis_workspace_key(_scoped(t_a, ws_a), "notes") != redis_workspace_key(
        _scoped(t_b, ws_b), "notes"
    )


def test_a_workspace_key_sits_under_the_tenant_prefix():
    from core.identity.namespacing import tenant_prefix

    tenant, ws = uuid.uuid4(), uuid.uuid4()
    p = _scoped(tenant, ws)
    assert redis_workspace_key(p, "x").startswith(tenant_prefix(p)[:-1])
    assert workspace_prefix(p).startswith(tenant_prefix(p)[:-1])


def test_a_cache_key_without_a_workspace_fails():
    p = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
    )
    with pytest.raises(MissingWorkspaceContext):
        redis_workspace_key(p, "summary")
    with pytest.raises(MissingWorkspaceContext):
        workspace_object_path(p, "file.txt")


def test_no_username_or_email_appears_in_a_generated_key():
    p = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), session_id=uuid.uuid4(),
        edition=EDITION_CLOUD, workspace_id=uuid.uuid4(),
        username="alice@example.com",
    )
    key = redis_workspace_key(p, "summary")
    path = workspace_object_path(p, "notes", "a.txt")
    for produced in (key, path, workspace_prefix(p)):
        assert "alice" not in produced
        assert "@" not in produced


def test_an_object_path_cannot_escape_its_workspace():
    p = _scoped(uuid.uuid4(), uuid.uuid4())
    for attack in ("..", "../..", "../../other"):
        with pytest.raises(ValueError):
            workspace_object_path(p, attack, "x.txt")


def test_an_object_path_is_nested_under_tenant_and_workspace():
    tenant, ws = uuid.uuid4(), uuid.uuid4()
    path = workspace_object_path(_scoped(tenant, ws), "docs", "a.txt")
    assert path == f"tenants/{tenant}/workspaces/{ws}/docs/a.txt"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Background jobs
# ─────────────────────────────────────────────────────────────────────────────


def test_a_workspace_job_records_tenant_and_workspace():
    tenant, ws, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = job_context(
        _scoped(tenant, ws, user), job="reindex", workspace_scoped=True, now=NOW
    )
    assert ctx.tenant_id == tenant
    assert ctx.workspace_id == ws
    assert ctx.requested_by == user
    payload = ctx.to_payload()
    assert payload["tenant_id"] == str(tenant)
    assert payload["workspace_id"] == str(ws)


def test_a_workspace_scoped_job_without_a_workspace_is_rejected():
    p = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
    )
    with pytest.raises(MissingJobContext):
        job_context(p, job="reindex", workspace_scoped=True, now=NOW)


def test_a_tenant_wide_job_may_omit_the_workspace():
    p = _scoped(uuid.uuid4(), uuid.uuid4())
    ctx = job_context(p, job="billing-rollup", workspace_scoped=False, now=NOW)
    assert ctx.workspace_id is None


def test_a_retry_preserves_the_original_context():
    """The property that makes the same job write to the same place twice."""
    ctx = job_context(
        _scoped(uuid.uuid4(), uuid.uuid4()), job="reindex", workspace_scoped=True, now=NOW
    )
    again = preserve_for_retry(ctx)
    assert again == ctx
    assert again.job_id == ctx.job_id
    assert again.workspace_id == ctx.workspace_id
    # And a round trip through the queue changes nothing.
    assert JobContext.from_payload(ctx.to_payload()) == ctx


def test_a_worker_does_not_infer_a_workspace_at_execution_time():
    """A payload with no workspace produces a principal with no workspace —
    not the default one, and not the enqueuing user's current one."""
    ctx = job_context(
        _scoped(uuid.uuid4(), uuid.uuid4()), job="rollup", workspace_scoped=False, now=NOW
    )
    restored = JobContext.from_payload(ctx.to_payload())
    principal = restored.principal()
    assert principal.workspace_id is None
    with pytest.raises(MissingWorkspaceContext):
        principal.require_workspace()


def test_a_payload_without_a_tenant_is_refused_on_the_way_out_of_the_queue():
    with pytest.raises(MissingJobContext):
        JobContext.from_payload({"job": "reindex", "workspace_id": str(uuid.uuid4())})


def test_a_job_principal_is_never_the_owner():
    ctx = job_context(
        _scoped(uuid.uuid4(), uuid.uuid4()), job="reindex", workspace_scoped=True, now=NOW
    )
    p = ctx.principal()
    assert not p.is_owner
    assert p.roles == frozenset()
    assert p.scopes == frozenset()


def test_a_job_payload_carries_identifiers_only():
    """No name, no email, no customer content — a payload is logged, queued and
    kept in a dead-letter record."""
    p = Principal(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), session_id=uuid.uuid4(),
        edition=EDITION_CLOUD, workspace_id=uuid.uuid4(),
        username="alice@example.com",
    )
    payload = job_context(p, job="reindex", workspace_scoped=True, now=NOW).to_payload()
    assert set(payload) == {
        "tenant_id", "workspace_id", "requested_by", "job", "job_id", "created_at",
        # Batch 5C. `authorisation` records what the enqueuing principal held
        # (a capability string, e.g. "workspace.write") for the audit trail —
        # it is NOT consulted when the job runs, because a revoked user's
        # queued work must stop; see `background.authorise()`.
        # `correlation_id` ties the job to the request that created it.
        # Both are identifiers or fixed vocabulary, so the claim this test
        # makes is unchanged.
        "authorisation", "correlation_id",
    }
    assert "alice" not in repr(payload)
