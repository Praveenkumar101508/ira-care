"""The three-principal matrix every Phase 3C suite is written against.

    Tenant A / Workspace A / User A
    Tenant A / Workspace B / User B
    Tenant B / Workspace C / User C

Three, not two, and the reason is the middle row. A two-principal fixture can
only ask "can a stranger reach my data", and the answer to that has been yes-it-
is-refused since `023`. The interesting question is the one only three can ask:

    **A and B are the same tenant.** They share a company, an owner, a billing
    relationship and a database row. Everything that scopes by tenant lets B
    read A. Only the workspace boundary separates them, so B is the principal
    that finds a workspace check somebody forgot.

    **C is a different tenant**, and is here to catch the opposite mistake — a
    fix that scopes by workspace and quietly drops the tenant arm. A workspace
    id is a UUIDv4 and therefore unguessable, which is an argument about
    difficulty rather than authorisation; C is what turns that into a test.

── What this module deliberately does not do ───────────────────────────────

It does not create content. Each suite writes what it needs, because a shared
fixture that pre-populated every table would make every test depend on rows it
did not write and could not see the point of.

It does not clean up with `TRUNCATE`. Teardown deletes by tenant id, child
tables first, so a suite that leaves a foreign key dangling fails loudly here
rather than corrupting the next run.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")


@dataclass(frozen=True, slots=True)
class Actor:
    """One (tenant, workspace, user) triple and the principal it produces."""

    label: str
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    username: str

    def principal(self, *, edition: str | None = None):
        from core.editions import EDITION_CLOUD
        from core.identity.principal import Principal

        return Principal(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            session_id=uuid.uuid4(),
            edition=edition or EDITION_CLOUD,
            workspace_id=self.workspace_id,
        )

    def without_workspace(self):
        """The same identity with no workspace bound.

        Used wherever a test needs to prove that tenant context alone is
        insufficient — which is a different claim from "an unauthenticated
        caller is refused" and needs its own principal to make.
        """
        return replace(self.principal(), workspace_id=None)

    def with_workspace(self, workspace_id):
        """This identity, pointed at a workspace it may not be a member of.

        The shape of a bug that skipped verification: the principal is real, the
        workspace is real, and nothing checked that they belong together. The
        database is what must refuse.
        """
        return replace(self.principal(), workspace_id=workspace_id)


@dataclass(frozen=True, slots=True)
class Matrix:
    a: Actor  # tenant A, workspace A
    b: Actor  # tenant A, workspace B — the same tenant as `a`
    c: Actor  # tenant B, workspace C — a different tenant entirely

    @property
    def actors(self) -> tuple[Actor, Actor, Actor]:
        return (self.a, self.b, self.c)

    @property
    def tenants(self) -> tuple[uuid.UUID, uuid.UUID]:
        return (self.a.tenant_id, self.c.tenant_id)


async def build(conn) -> Matrix:
    """Create the matrix on `conn`, which must be able to see every tenant.

    A superuser connection, deliberately: constructing the fixture is not the
    thing under test, and reading it back as `ira_app` is. A fixture built
    through the boundary it is meant to probe would be assuming its own
    conclusion.
    """
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    run = uuid.uuid4().hex[:8]

    for tenant, name in ((tenant_a, f"adv-a-{run}"), (tenant_b, f"adv-b-{run}")):
        await conn.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
            tenant, name, name)

    ws_a = await conn.fetchval("SELECT provision_tenant_substrate($1)", tenant_a)
    ws_c = await conn.fetchval("SELECT provision_tenant_substrate($1)", tenant_b)
    org_a = await conn.fetchval(
        "SELECT id FROM organisations WHERE tenant_id = $1", tenant_a)
    ws_b = uuid.uuid4()
    await conn.execute(
        "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug,"
        "                        is_default, status) "
        "VALUES ($1,$2,$3,'Second',$4,false,'active')",
        ws_b, tenant_a, org_a, f"adv-second-{run}")

    actors = []
    for label, tenant, workspace in (
        ("A", tenant_a, ws_a), ("B", tenant_a, ws_b), ("C", tenant_b, ws_c),
    ):
        user = uuid.uuid4()
        # The SAME username in both tenants, on purpose. Usernames are
        # per-tenant, so anything keyed on one — a cache key, a storage path —
        # collides across the boundary, and a fixture with unique usernames
        # would never catch it.
        username = f"alice-{run}"
        await conn.execute(
            "INSERT INTO users (id, email) VALUES ($1, $2)",
            user, f"{user.hex[:12]}@example.test")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", tenant, user)
        await conn.execute(
            "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id,"
            "                                   role, status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", tenant, workspace, user)
        actors.append(Actor(label, tenant, workspace, user, username))

    return Matrix(*actors)


async def teardown(conn, matrix: Matrix) -> None:
    """Remove everything the matrix owns, children before parents.

    No TRUNCATE: a suite that left a foreign key dangling should fail here,
    loudly, rather than have the evidence removed from under it.
    """
    content = (
        "messages", "edges", "entities", "commitments", "lifeos_events",
        "decisions", "reminders", "tasks", "projects", "people", "files",
        "companion_versions", "companions", "calendar_events",
        "memory_embeddings", "agents", "conversations",
        # 051 gave `notifications` a tenant and a workspace FK with
        # ON DELETE RESTRICT, so a leftover row blocks the workspace delete
        # below. Listed here rather than cascaded deliberately: a suite that
        # leaves one should fail loudly, not have the evidence swept away.
        "notifications",
        # IRA CARE (099): `care_access_log` is audit evidence with RESTRICT
        # keys, so it must go explicitly (the privileged role may purge it);
        # `care_people` cascades to every other care table.
        "care_access_log", "care_people",
    )
    for tenant in matrix.tenants:
        for table in content:
            await conn.execute(f"DELETE FROM {table} WHERE tenant_id=$1", tenant)  # noqa: S608
        await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", tenant)
        await conn.execute("DELETE FROM tenants WHERE id=$1", tenant)
    for actor in matrix.actors:
        await conn.execute("DELETE FROM users WHERE id=$1", actor.user_id)


@pytest.fixture
async def matrix():
    """The three-principal matrix, created and removed around one test."""
    if not ADMIN_DSN:
        pytest.skip("IRA_TEST_ADMIN_DSN not set")
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        built = await build(conn)
        try:
            yield built
        finally:
            await teardown(conn, built)
    finally:
        await conn.close()


@pytest.fixture
async def app_pool():
    """Point `utils.db.acquire()` at the test database."""
    if not DSN:
        pytest.skip("IRA_TEST_DATABASE_DSN not set")
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


__all__ = ["Actor", "Matrix", "build", "teardown", "matrix", "app_pool"]
