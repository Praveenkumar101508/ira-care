"""A queued job carries its context, not its permission.

`JobContext` fixes WHERE work happens — decided when the user authorised it,
so a retry three hours later writes to the same place as the first attempt.
That is `test_phase3b_batch5a.py`'s subject and it stays true.

This file is about the other half, which Batch 5C adds: whether the work may
still happen at all. Between enqueue and execution the user can be removed
from the tenant, removed from the workspace, suspended, or have the workspace
archived beneath them. A queue that trusted the enqueue-time decision would
keep serving somebody who was revoked — for as long as the backlog is deep,
which is precisely when it matters.

So `authorise()` re-asks the database at execution time, and the tests below
revoke each thing in turn and assert the job stops.

Housekeeping is the deliberate exception and is tested as such: no user asked
for it, so there is no membership to revalidate. It gets the tenant's
CANONICAL default workspace — the `is_default` marker `039` sets — and skips
when that is absent, ambiguous or inactive. Never "the first workspace".

Real PostgreSQL as usual: a revoked membership is a row, and the question is
what the database says about it now.
"""
from __future__ import annotations

import contextlib
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.identity.background import (  # noqa: E402
    JobAuthorisationRevoked,
    JobContext,
    MissingJobContext,
    authorise,
    default_workspace_job_principal,
)

NOW = "2026-08-06T00:00:00Z"


class World:
    pass


@pytest.fixture(autouse=True)
async def app_pool():
    """`authorise` and `default_workspace_job_principal` use the application
    pool, which is the right shape for production and means a test supplies it."""
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


@pytest.fixture
async def world():
    w = World()
    w.tenant = uuid.uuid4()
    w.user = uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$2)",
            w.tenant, f"job-{w.tenant.hex[:8]}")
        await conn.execute(
            "INSERT INTO users (id,email) VALUES ($1,$2)",
            w.user, f"{w.user.hex[:12]}@example.test")
        org = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name) VALUES ($1,'J') RETURNING id",
            w.tenant)
        w.workspace = await conn.fetchval(
            """INSERT INTO workspaces (tenant_id,organisation_id,name,slug,
                                       is_default,status)
               VALUES ($1,$2,'Default',$3,TRUE,'active') RETURNING id""",
            w.tenant, org, f"j-{w.tenant.hex[:8]}")
        await conn.execute(
            """INSERT INTO tenant_memberships (tenant_id,user_id,role,status)
               VALUES ($1,$2,'member','active')""", w.tenant, w.user)
        await conn.execute(
            """INSERT INTO workspace_memberships
                 (tenant_id,workspace_id,user_id,role,status)
               VALUES ($1,$2,$3,'member','active')""",
            w.tenant, w.workspace, w.user)
        w.conn = conn
        yield w
    finally:
        await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", w.tenant)
        await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", w.tenant)
        await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", w.tenant)
        await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", w.tenant)
        await conn.execute("DELETE FROM tenants WHERE id=$1", w.tenant)
        await conn.execute("DELETE FROM users WHERE id=$1", w.user)
        await conn.close()


@contextlib.asynccontextmanager
async def _uncommitted(conn):
    """A transaction that is ALWAYS rolled back.

    Several states this file needs — no default workspace, two defaults, an
    archived-only tenant — are refused by `033`'s DEFERRED one-default trigger
    at COMMIT. They exist mid-transaction and nowhere else, so the tests reach
    them here and leave nothing behind.

    Also the standing rule after `465ea85`: a test that puts the database into
    a state the schema forbids must not be able to leave it there.
    """
    tx = conn.transaction()
    await tx.start()
    try:
        yield
    finally:
        await tx.rollback()


def _queued(world, **over) -> JobContext:
    """A job as it would sit in the queue — identifiers only, no live objects."""
    fields = dict(
        tenant_id=world.tenant, workspace_id=world.workspace,
        requested_by=world.user, job="export", job_id=uuid.uuid4(),
        created_at=NOW, authorisation="workspace.read",
        correlation_id=uuid.uuid4(),
    )
    fields.update(over)
    return JobContext(**fields)


# ─────────────────────────────────────────────────────────────────────────────
# The happy path, so the refusals below mean something
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_still_authorised_job_runs_as_the_initiating_user(world):
    principal = await authorise(_queued(world))
    assert principal.user_id == world.user, (
        "the job must run as the person who asked for it — rows it writes are "
        "attributable to them, and `app.user_id` drives the USER_PRIVATE policies"
    )
    assert principal.workspace_id == world.workspace
    assert principal.workspace_role == "member"


async def test_the_payload_round_trips_the_audit_fields(world):
    """Tenant, workspace, initiating user, authorisation and correlation id all
    survive the queue. Identifiers only — a payload is logged and kept in a
    dead-letter record, which is no place for customer content."""
    original = _queued(world)
    restored = JobContext.from_payload(original.to_payload())
    assert restored == original
    assert "authorisation" in original.to_payload()
    assert "correlation_id" in original.to_payload()


# ─────────────────────────────────────────────────────────────────────────────
# Revocation between enqueue and execution
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_removed_tenant_membership_stops_the_job(world):
    await world.conn.execute(
        "DELETE FROM tenant_memberships WHERE tenant_id=$1 AND user_id=$2",
        world.tenant, world.user)
    with pytest.raises(JobAuthorisationRevoked) as e:
        await authorise(_queued(world))
    assert "no longer holds an active membership" in str(e.value)


async def test_a_suspended_tenant_membership_stops_the_job(world):
    """Suspension is not removal, and both have to bite. `membership_grants_access`
    is the generated column, so Python cannot disagree with the policies about
    what "active" means."""
    await world.conn.execute(
        "UPDATE tenant_memberships SET status='suspended' WHERE tenant_id=$1",
        world.tenant)
    with pytest.raises(JobAuthorisationRevoked):
        await authorise(_queued(world))


async def test_a_removed_workspace_membership_stops_the_job(world):
    """The user is still in the tenant — only the workspace access is gone.
    A check that stopped at the tenant would let this through."""
    await world.conn.execute(
        "UPDATE workspace_memberships SET status='removed', removed_at=NOW() "
        " WHERE workspace_id=$1", world.workspace)
    with pytest.raises(JobAuthorisationRevoked) as e:
        await authorise(_queued(world))
    assert "no longer has access to workspace" in str(e.value)


async def test_an_archived_workspace_stops_the_job(world):
    """Nobody was revoked; the workspace itself was retired underneath them.

    Run inside a transaction that is always rolled back. `033` carries a
    DEFERRED trigger requiring exactly one ACTIVE default workspace per tenant,
    so a tenant whose only workspace is archived cannot exist at COMMIT — the
    state is reachable mid-transaction and nowhere else. That is the schema
    being stricter than this test first assumed, which is the right direction.
    """
    org = await world.conn.fetchval(
        "SELECT organisation_id FROM workspaces WHERE id=$1", world.workspace)
    second = await world.conn.fetchval(
        """INSERT INTO workspaces (tenant_id,organisation_id,name,slug,
                                   is_default,status,archived_at)
           VALUES ($1,$2,'Retired','retired-job',FALSE,'archived',NOW())
           RETURNING id""", world.tenant, org)
    await world.conn.execute(
        """INSERT INTO workspace_memberships
             (tenant_id,workspace_id,user_id,role,status)
           VALUES ($1,$2,$3,'member','active')""",
        world.tenant, second, world.user)

    with pytest.raises(JobAuthorisationRevoked) as e:
        await authorise(_queued(world, workspace_id=second))
    assert "not active" in str(e.value)


async def test_a_deleted_workspace_stops_the_job(world):
    await world.conn.execute(
        "DELETE FROM workspace_memberships WHERE workspace_id=$1", world.workspace)
    await world.conn.execute("DELETE FROM workspaces WHERE id=$1", world.workspace)
    with pytest.raises(JobAuthorisationRevoked) as e:
        await authorise(_queued(world))
    assert "is gone or not in tenant" in str(e.value)


async def test_a_workspace_from_another_tenant_stops_the_job(world):
    """A payload naming a workspace outside its own tenant is either corruption
    or tampering. It reports the same way as a missing one — the distinction is
    not something a queue payload gets to establish."""
    with pytest.raises(JobAuthorisationRevoked):
        await authorise(_queued(world, workspace_id=uuid.uuid4()))


async def test_stale_enqueue_time_authorisation_is_not_trusted(world):
    """The point of the whole file, stated directly.

    The payload says the user held `workspace.delete_content`. They have since
    been removed from the tenant. The recorded authorisation is audit
    information, not a decision — if it were consulted, this job would run.
    """
    await world.conn.execute(
        "DELETE FROM tenant_memberships WHERE tenant_id=$1", world.tenant)
    with pytest.raises(JobAuthorisationRevoked):
        await authorise(_queued(world, authorisation="workspace.delete_content"))


# ─────────────────────────────────────────────────────────────────────────────
# Housekeeping — no user, so no membership to revalidate
# ─────────────────────────────────────────────────────────────────────────────


async def test_housekeeping_cannot_go_through_the_user_path(world):
    """A context with no initiating user is not "authorised by default"."""
    with pytest.raises(JobAuthorisationRevoked) as e:
        await authorise(_queued(world, requested_by=None))
    assert "no initiating user" in str(e.value)


async def test_housekeeping_gets_the_canonical_default_workspace(world):
    principal = await default_workspace_job_principal(world.tenant, job="retention")
    assert principal.workspace_id == world.workspace
    assert principal.user_id != world.user, (
        "housekeeping runs as a job identity, not as some user who happens to "
        "be in the tenant"
    )


async def test_a_tenant_with_no_workspace_at_all_is_skipped(world):
    """The reachable half of "no canonical workspace".

    A tenant that was never provisioned has no workspaces, so there is nothing
    to mark default. Housekeeping refuses rather than reaching for another
    tenant's — the failure mode a "first workspace" fallback would produce.
    """
    bare = uuid.uuid4()
    await world.conn.execute(
        "INSERT INTO tenants (id,name,slug) VALUES ($1,$2,$2)", bare, f"bare-{bare.hex[:8]}")
    try:
        with pytest.raises(MissingJobContext) as e:
            await default_workspace_job_principal(bare, job="retention")
        assert "no workspace marked" in str(e.value)
    finally:
        await world.conn.execute("DELETE FROM tenants WHERE id=$1", bare)


async def test_the_schema_makes_a_missing_canonical_workspace_unrepresentable(world):
    """`033`'s deferred trigger requires EXACTLY ONE active default per tenant.

    So "a provisioned tenant with no default" and "a tenant with two defaults"
    cannot be committed at all — which is a stronger guarantee than anything
    `default_workspace_job_principal` could check, and is why its own branches
    for those cases are defence in depth rather than the primary control.

    Asserting the guarantee here means that if a future migration relaxes the
    trigger, this fails and the defence-in-depth branches stop being theoretical.
    """
    tx = world.conn.transaction()
    await tx.start()
    try:
        with pytest.raises(asyncpg.exceptions.RaiseError) as e:
            await world.conn.execute(
                "UPDATE workspaces SET is_default=FALSE WHERE tenant_id=$1",
                world.tenant)
            await tx.commit()
        assert "exactly one is required" in str(e.value)
    finally:
        try:
            await tx.rollback()
        except asyncpg.exceptions.InterfaceError:
            pass  # already finished by the failed commit


async def test_the_schema_makes_two_canonical_workspaces_unrepresentable(world):
    """The other half of the same trigger."""
    org = await world.conn.fetchval(
        "SELECT organisation_id FROM workspaces WHERE id=$1", world.workspace)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await world.conn.execute(
            """INSERT INTO workspaces (tenant_id,organisation_id,name,slug,
                                       is_default,status)
               VALUES ($1,$2,'Second','second-dup',TRUE,'active')""",
            world.tenant, org)


async def test_the_schema_refuses_an_archived_default(world):
    """`workspaces_archived_is_not_default`: an archived workspace is not a
    canonical one, so the state `default_workspace_job_principal` checks for
    cannot be reached from the other direction either."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await world.conn.execute(
            "UPDATE workspaces SET status='archived', archived_at=NOW() WHERE id=$1",
            world.workspace)
