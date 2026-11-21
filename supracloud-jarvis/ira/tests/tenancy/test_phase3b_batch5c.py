"""Phase 3B Batch 5C — a request reaches content through a verified workspace.

Batch 5A built the verification and the binding. Batch 5B added the columns.
Neither wired anything: no route called `workspace_connection`, so the whole
apparatus was correct and inert. This batch connects it, and these are the
tests of the connection.

The question each one asks is some form of:

    **Where did this request's workspace come from, and what proved it?**

Three answers are acceptable — an `X-Workspace-ID` the database verified, the
legacy owner's default workspace in the private edition, or a refusal. Every
other answer, and in particular "the first one we found" or "the default",
is the failure this suite exists to catch.

── What IS claimed here, now that 049 has landed ────────────────────────────

Workspace isolation **is** enforced. `049` gives every one of the seventeen
workspace-scoped tables four per-command policies requiring both
`app.tenant_id` and `app.workspace_id`, and section 3b below is the proof:
read, insert, update, workspace-reassignment, delete, upsert, foreign-key
reference, and an unbound connection.

This file used to say the opposite. `test_cross_workspace_read_is_not_refused_yet`
asserted that workspace A could read workspace B — true at the time, and written
to FAIL the moment the policies landed so nobody had to remember to come back.
It did fail, on the first run against a database carrying 049, and section 3b
is what replaced it.

Two things are still NOT claimed. `workspace_id` remains NULLABLE until `050`,
so the composite foreign keys are still vacuous for any row that predates the
policies — covered, because no connection can write such a row any more, but not
filled. And nothing here is a claim about the frontend, which is proved
separately by the Vitest and Playwright suites.

Runs against a real PostgreSQL as `ira_app` — non-superuser, NOBYPASSRLS,
owning nothing.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip(
        "IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set — Batch 5C tests skipped",
        allow_module_level=True,
    )

from fastapi import HTTPException  # noqa: E402

from api.middleware.workspace import (  # noqa: E402
    WORKSPACE_HEADER,
    require_capability_or_403,
    resolve_workspace_principal,
)
from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402
from core.identity.db_context import workspace_connection  # noqa: E402
from core.identity.principal import (  # noqa: E402
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
)
from core.identity.workspace import WorkspaceSelectionError  # noqa: E402

VAULT_MARKER = "owner"


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def app_pool():
    """Point `utils.db.acquire()` at the test database.

    `workspace_connection` and `owner_connection` go through the application's
    own pool rather than taking a connection argument, which is the right shape
    for production and means a test has to supply the pool. Assigning the module
    global is honest about that: there is no second code path here, the helpers
    under test are the ones the routes use.

    An async fixture, so the pool is created on the loop the test bodies run
    on. A pool built on a throwaway loop hands out connections whose timers
    belong to a loop that is already closed.
    """
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class World:
    pass


@pytest.fixture
def world():
    """One tenant, two workspaces, three users in different roles.

    Built through a superuser connection: constructing the fixture is not the
    thing under test, reading it back as `ira_app` is.
    """
    w = World()
    w.tenant = uuid.uuid4()
    w.other_tenant = uuid.uuid4()
    w.admin = uuid.uuid4()      # workspace_admin of ws_a
    w.viewer = uuid.uuid4()     # viewer of ws_a — may read, may not write
    w.outsider = uuid.uuid4()   # tenant member, no workspace membership

    async def build():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            for t, n in ((w.tenant, "b5c"), (w.other_tenant, "b5c-other")):
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, $2)", t, f"{n}-{t.hex[:8]}"
                )
            for u in (w.admin, w.viewer, w.outsider):
                await conn.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)",
                    u, f"{u.hex[:12]}@example.test",
                )
            w.ws_a = await conn.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
            w.ws_other = await conn.fetchval(
                "SELECT provision_tenant_substrate($1)", w.other_tenant
            )
            org = await conn.fetchval(
                "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant
            )
            w.ws_b = uuid.uuid4()
            await conn.execute(
                """INSERT INTO workspaces
                     (id, tenant_id, organisation_id, name, slug, is_default, status)
                   VALUES ($1,$2,$3,'Second','second',false,'active')""",
                w.ws_b, w.tenant, org,
            )
            for u in (w.admin, w.viewer, w.outsider):
                await conn.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                       VALUES ($1,$2,'member','active')""",
                    w.tenant, u,
                )
            for u, ws, role in (
                (w.admin, w.ws_a, "workspace_admin"),
                (w.admin, w.ws_b, "workspace_admin"),
                (w.viewer, w.ws_a, "viewer"),
            ):
                await conn.execute(
                    """INSERT INTO workspace_memberships
                         (tenant_id, workspace_id, user_id, role, status)
                       VALUES ($1,$2,$3,$4,'active')""",
                    w.tenant, ws, u, role,
                )
        finally:
            await conn.close()

    async def teardown():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            for t in (w.tenant, w.other_tenant):
                # Children first. The denial tests write entities and edges, and
                # `entities_workspace_id_fkey` is ON DELETE RESTRICT — deleting a
                # workspace with entities still pointing at it fails, which is
                # the carry-forward rule working and a teardown that has to
                # respect it.
                await conn.execute("DELETE FROM edges WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM entities WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenants WHERE id=$1", t)
            for u in (w.admin, w.viewer, w.outsider):
                await conn.execute("DELETE FROM users WHERE id=$1", u)
        finally:
            await conn.close()

    _run(build())
    yield w
    _run(teardown())


def principal_for(user_id, tenant_id, *, edition=EDITION_CLOUD, **kw) -> Principal:
    return Principal(
        user_id=user_id, tenant_id=tenant_id, session_id=uuid.uuid4(),
        edition=edition, **kw,
    )


def _patch_resolver(monkeypatch, principal: Principal) -> None:
    """Stand in for cloud identity, which Phase 2 has not built.

    `resolve_principal` raises `CloudIdentityNotAvailable` in cloud, so the
    workspace branches below are unreachable through the real resolver today.
    Patching it is what lets the code that WILL run be tested now, rather than
    shipped untested behind a feature that has not arrived.
    """
    async def fake(username, **kw):
        return principal

    monkeypatch.setattr("api.middleware.workspace.resolve_principal", fake)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Where the workspace comes from
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_verified_header_selects_that_workspace(world, monkeypatch):
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    bound = await resolve_workspace_principal("someone", str(world.ws_b))

    assert bound.workspace_id == world.ws_b
    assert bound.workspace_role == "workspace_admin"
    assert bound.tenant_id == world.tenant


async def test_a_workspace_in_another_tenant_is_refused(world, monkeypatch):
    """And refused with the SAME message as one that does not exist.

    Telling those apart would let a caller enumerate which workspace ids are
    real in tenants they have no access to.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    with pytest.raises(HTTPException) as foreign:
        await resolve_workspace_principal("someone", str(world.ws_other))
    with pytest.raises(HTTPException) as absent:
        await resolve_workspace_principal("someone", str(uuid.uuid4()))

    assert foreign.value.status_code == 403
    assert foreign.value.detail == absent.value.detail == WorkspaceSelectionError.PUBLIC_MESSAGE


async def test_membership_of_the_tenant_is_not_membership_of_the_workspace(world, monkeypatch):
    """`outsider` is an active member of the tenant that owns `ws_a`."""
    p = principal_for(world.outsider, world.tenant)
    _patch_resolver(monkeypatch, p)

    with pytest.raises(HTTPException) as e:
        await resolve_workspace_principal("someone", str(world.ws_a))
    assert e.value.status_code == 403


async def test_a_malformed_header_is_a_refusal_not_a_crash(world, monkeypatch):
    """A 500 here would leak a stack trace naming tables, and would report a
    client's bad input as a server fault."""
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    with pytest.raises(HTTPException) as e:
        await resolve_workspace_principal("someone", "not-a-uuid")
    assert e.value.status_code == 403
    assert e.value.detail == WorkspaceSelectionError.PUBLIC_MESSAGE


async def test_cloud_refuses_to_choose_a_workspace_when_none_was_named(world, monkeypatch):
    """The standing rule, as a test: no default-workspace fallback in cloud.

    400 rather than 403 — the caller is not forbidden, their request is
    incomplete. And critically NOT a 200 carrying somebody's default workspace,
    which is what "be helpful" would have produced.
    """
    p = principal_for(world.admin, world.tenant, edition=EDITION_CLOUD)
    _patch_resolver(monkeypatch, p)
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)

    with pytest.raises(HTTPException) as e:
        await resolve_workspace_principal("someone", None)
    assert e.value.status_code == 400
    assert WORKSPACE_HEADER in e.value.detail


async def test_the_private_edition_falls_back_only_for_the_verified_legacy_owner(monkeypatch):
    """A private request with no header gets the owner's default workspace.

    IRA's API and UI have no workspace concept, so refusing every unheadered
    request would break the running product. The fallback is still a full
    verification — it goes through `select_workspace` — so it is a narrower
    door, not an open one.
    """
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    p = principal_for(
        private_user_id("admin"), PRIVATE_TENANT_ID,
        edition=EDITION_PRIVATE, is_owner=True, username="admin",
    )
    _patch_resolver(monkeypatch, p)

    bound = await resolve_workspace_principal("admin", None)
    assert bound.workspace_id is not None
    assert bound.tenant_id == PRIVATE_TENANT_ID


async def test_the_private_fallback_is_closed_to_a_non_owner(world, monkeypatch):
    """`legacy_owner_workspace` serves the legacy owner and nobody else, so an
    ordinary private account with no header is refused rather than handed the
    owner's workspace."""
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    p = principal_for(
        world.admin, world.tenant, edition=EDITION_PRIVATE, is_owner=False,
    )
    _patch_resolver(monkeypatch, p)

    with pytest.raises(HTTPException) as e:
        await resolve_workspace_principal("someone", None)
    assert e.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 2. Capabilities
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_viewer_may_read_but_not_write_or_delete(world, monkeypatch):
    p = principal_for(world.viewer, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))
    assert bound.workspace_role == "viewer"

    async with workspace_connection(bound) as conn:
        await require_capability_or_403(conn, bound, "workspace.read")
        for denied in ("workspace.write", "workspace.delete_content"):
            with pytest.raises(HTTPException) as e:
                await require_capability_or_403(conn, bound, denied)
            assert e.value.status_code == 403


async def test_a_workspace_admin_carries_delete_content(world, monkeypatch):
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))

    async with workspace_connection(bound) as conn:
        for allowed in ("workspace.read", "workspace.write", "workspace.delete_content"):
            await require_capability_or_403(conn, bound, allowed)


async def test_the_tenant_role_does_not_confer_a_workspace_capability(world, monkeypatch):
    """CLOUD-ADR-0016 as a test. `viewer` here is a tenant `member`."""
    p = principal_for(world.viewer, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))

    async with workspace_connection(bound) as conn:
        tenant_role = await conn.fetchval(
            "SELECT role FROM tenant_memberships WHERE tenant_id=$1 AND user_id=$2",
            world.tenant, world.viewer,
        )
        assert tenant_role == "member"
        with pytest.raises(HTTPException):
            await require_capability_or_403(conn, bound, "workspace.write")


# ─────────────────────────────────────────────────────────────────────────────
# 3. The Memory Vault — Issue #55, cloud half
# ─────────────────────────────────────────────────────────────────────────────


async def _insert_memory(principal, text: str):
    """The INSERT `api/routes/memory.py` runs, verbatim in shape."""
    async with workspace_connection(principal) as conn:
        return await conn.fetchrow(
            """INSERT INTO memory_embeddings
                   (source_id, source_type, content, user_id, metadata,
                    tenant_id, workspace_id)
               VALUES ($1, 'note', $2, $3, '{}'::jsonb,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       NULLIF(current_setting('app.workspace_id', true), '')::uuid)
               RETURNING id, tenant_id, workspace_id""",
            uuid.uuid4(), text, VAULT_MARKER,
        )


async def test_a_vault_write_is_accepted_and_records_its_own_context(world, monkeypatch):
    """The Issue #55 regression, in one test.

    Through a bare `acquire()` this INSERT was refused outright: `tenant_id` is
    NOT NULL with no default and `memory_embeddings` has forced RLS, so an
    unbound connection could neither supply the column nor satisfy the policy.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))

    row = await _insert_memory(bound, "batch 5c vault write")

    assert row["tenant_id"] == world.tenant
    assert row["workspace_id"] == world.ws_a, (
        "the row must carry the workspace the transaction was bound to — not a "
        "value Python passed alongside it, which could disagree"
    )


async def test_a_vault_read_returns_the_row_it_just_wrote(world, monkeypatch):
    """The other half of the regression: reads matched zero rows silently."""
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))

    await _insert_memory(bound, "readable")
    async with workspace_connection(bound) as conn:
        rows = await conn.fetch(
            "SELECT content FROM memory_embeddings "
            " WHERE user_id = $1 AND source_type <> 'message'",
            VAULT_MARKER,
        )
    assert any(r["content"] == "readable" for r in rows)


async def test_another_tenant_cannot_see_the_row(world, monkeypatch):
    """Tenant isolation IS enforced today — `023`'s policy predicates on
    `app.tenant_id`, and this proves the new write path did not escape it."""
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    bound = await resolve_workspace_principal("someone", str(world.ws_a))
    await _insert_memory(bound, "tenant-private")

    other = principal_for(world.outsider, world.other_tenant)
    other = other.__class__(
        **{**{f.name: getattr(other, f.name) for f in other.__dataclass_fields__.values()},
           "workspace_id": world.ws_other}
    )
    async with workspace_connection(other) as conn:
        rows = await conn.fetch(
            "SELECT content FROM memory_embeddings WHERE content = 'tenant-private'"
        )
    assert rows == []


# ─────────────────────────────────────────────────────────────────────────────
# 3b. The denial tests — what 049 actually refuses
#
#     These replace `test_cross_workspace_read_is_not_refused_yet`, which
#     asserted the opposite and was written to fail the moment the policies
#     landed. It did, on the first run against a database carrying 049.
#
#     One workspace and one tenant are not the same boundary and are tested
#     separately: the second was already enforced by 023 and is proved
#     elsewhere in this file, and these are about the first.
#
#     Every operation gets its own test rather than one test with four
#     statements. A single test that stops at the first refusal proves the
#     first predicate and nothing about the other three, and the per-command
#     policies exist precisely so the four can differ.
# ─────────────────────────────────────────────────────────────────────────────


async def _memory_in(principal, content: str):
    """One memory row written through the caller's own workspace binding."""
    await _insert_memory(principal, content)


async def test_a_workspace_cannot_read_another_workspaces_rows(world, monkeypatch):
    """Same tenant, same user, different workspace — and nothing comes back.

    This is the whole point of Batch 5C in one assertion. The reader here is not
    an attacker or another customer: it is the same person, verified, in the
    same tenant, holding an admin role in both workspaces. The boundary is
    between the workspaces, not between the people.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "written in workspace A")

    in_b = await resolve_workspace_principal("someone", str(world.ws_b))
    async with workspace_connection(in_b) as conn:
        rows = await conn.fetch(
            "SELECT workspace_id FROM memory_embeddings "
            " WHERE content = 'written in workspace A'")

    assert rows == [], (
        "workspace A's row was visible from workspace B. The policies read "
        "app.workspace_id, so either one was dropped or a permissive policy "
        "left over from before 049 is being OR-ed with them.")


async def test_a_workspace_cannot_insert_into_another_workspace(world, monkeypatch):
    """A write fails loudly where a read fails silently.

    `WITH CHECK` is what stops a caller naming somebody else's workspace on the
    way in. Without it the row would be created, invisible to its author and
    visible to a workspace that never asked for it.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    in_a = await resolve_workspace_principal("someone", str(world.ws_a))

    async with workspace_connection(in_a) as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO memory_embeddings (source_type, content, user_id,"
                "                               tenant_id, workspace_id) "
                "VALUES ('fact', 'planted', $1, $2, $3)",
                VAULT_MARKER, world.tenant, world.ws_b)


async def test_a_workspace_cannot_update_another_workspaces_rows(world, monkeypatch):
    """Silent, and therefore worth its own test.

    An UPDATE whose USING clause matches nothing reports `UPDATE 0` — no error,
    no exception, nothing for a caller to notice. So the assertion is on the row
    itself: it must still say what workspace A wrote.

    **Unqualified**, and 3C-06 is why. PostgreSQL applies SELECT policies to an
    UPDATE or DELETE that references any column, so a `WHERE content = ...`
    filters the statement through the SELECT policy before the UPDATE policy is
    ever consulted — and reports `UPDATE 0` whether the UPDATE policy is correct
    or entirely absent. Written that way, this test proved the SELECT policy
    twice and the UPDATE policy never.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "A owns this")

    in_b = await resolve_workspace_principal("someone", str(world.ws_b))
    async with workspace_connection(in_b) as conn:
        await conn.execute(
            "UPDATE memory_embeddings SET content = 'B rewrote it'")

    async with workspace_connection(in_a) as conn:
        still = await conn.fetchval(
            "SELECT count(*) FROM memory_embeddings WHERE content = 'A owns this'")
    assert still == 1, "workspace B rewrote workspace A's row"


async def test_a_workspace_cannot_move_a_row_into_itself(world, monkeypatch):
    """The subtler half of UPDATE, and the reason both arms are stated.

    `USING` alone would let a row be read from the caller's own workspace and
    written into another — it passes the read check on the way in and lands
    anywhere. That is a cross-workspace write dressed as an edit, so `WITH
    CHECK` has to refuse the destination too.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "stays in A")

    async with workspace_connection(in_a) as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE memory_embeddings SET workspace_id = $1 "
                " WHERE content = 'stays in A'", world.ws_b)


async def test_a_workspace_cannot_delete_another_workspaces_rows(world, monkeypatch):
    """Also silent — `DELETE 0` is not an error either.

    Unqualified for the reason given above. Verified: dropping the workspace arm
    from a DELETE policy is caught by nothing while this reads
    `WHERE content = ...`, and caught immediately without it.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "A keeps this")

    in_b = await resolve_workspace_principal("someone", str(world.ws_b))
    async with workspace_connection(in_b) as conn:
        await conn.execute("DELETE FROM memory_embeddings")

    async with workspace_connection(in_a) as conn:
        still = await conn.fetchval(
            "SELECT count(*) FROM memory_embeddings WHERE content = 'A keeps this'")
    assert still == 1, "workspace B deleted workspace A's row"


async def test_an_upsert_cannot_reach_another_workspaces_row(world, monkeypatch):
    """R-68, from the other direction.

    A unique index is checked against the physical heap, not against the rows a
    policy makes visible — which is why `047` had to rescope the constraint
    rather than rely on RLS. With the key scoped AND the policy in place, an
    upsert from workspace B creates B's own row instead of conflicting with A's.

    If this ever returns one row, either 047 was reverted or its index lost the
    workspace column, and the failure will read as a policy problem when it is
    a constraint problem. Hence the message.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    async with workspace_connection(in_a) as conn:
        await conn.execute(
            "INSERT INTO entities (tenant_id, workspace_id, type, name, description) "
            "VALUES ($1, $2, 'person', 'Alice', 'A''s Alice')",
            world.tenant, world.ws_a)

    in_b = await resolve_workspace_principal("someone", str(world.ws_b))
    async with workspace_connection(in_b) as conn:
        await conn.execute(
            "INSERT INTO entities (tenant_id, workspace_id, type, name, description) "
            "VALUES ($1, $2, 'person', 'Alice', 'B''s Alice') "
            "ON CONFLICT (tenant_id, workspace_id, type, name) "
            "DO UPDATE SET description = EXCLUDED.description",
            world.tenant, world.ws_b)

    async with workspace_connection(in_a) as conn:
        description = await conn.fetchval(
            "SELECT description FROM entities WHERE type='person' AND name='Alice'")
    assert description == "A's Alice", (
        "workspace B's upsert overwrote workspace A's entity. RLS cannot "
        "prevent this on its own — the conflict is detected against the heap, "
        "not against policy-visible rows — so check 047's scoped index first.")


async def test_an_edge_cannot_reference_an_entity_in_another_workspace(world, monkeypatch):
    """A foreign key is a read of another row, and it must not cross either.

    Two ways this could go wrong and both are covered by the same assertion:
    the referenced entity is invisible under the policy, and `046`'s composite
    FK requires the workspace columns to agree. Either refusing is correct; the
    edge existing is not.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)

    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    async with workspace_connection(in_a) as conn:
        theirs = await conn.fetchval(
            "INSERT INTO entities (tenant_id, workspace_id, type, name) "
            "VALUES ($1, $2, 'person', 'Ada') RETURNING id",
            world.tenant, world.ws_a)

    in_b = await resolve_workspace_principal("someone", str(world.ws_b))
    async with workspace_connection(in_b) as conn:
        mine = await conn.fetchval(
            "INSERT INTO entities (tenant_id, workspace_id, type, name) "
            "VALUES ($1, $2, 'person', 'Grace') RETURNING id",
            world.tenant, world.ws_b)
        with pytest.raises((asyncpg.exceptions.ForeignKeyViolationError,
                            asyncpg.exceptions.InsufficientPrivilegeError)):
            await conn.execute(
                "INSERT INTO edges (tenant_id, workspace_id, src_entity_id,"
                "                   dst_entity_id, relation) "
                "VALUES ($1, $2, $3, $4, 'knows')",
                world.tenant, world.ws_b, mine, theirs)


async def test_a_connection_with_no_workspace_reads_and_writes_nothing(world, monkeypatch):
    """The fail-closed direction, at the database rather than in Python.

    `workspace_connection` refuses a principal with no workspace, which is the
    application-level guard and is tested separately. This is the guarantee
    underneath it: even if something bound only the tenant — a raw `acquire()`,
    a helper somebody added, a psql session — the tables are empty and the
    inserts refuse. The Python guard is a better error message on top of a
    boundary that does not need it.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "only visible with a workspace")

    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal_for(world.admin, world.tenant)) as conn:
        rows = await conn.fetch(
            "SELECT content FROM memory_embeddings "
            " WHERE content = 'only visible with a workspace'")
        assert rows == [], "a tenant-only connection read workspace-scoped content"

        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO memory_embeddings (source_type, content, user_id, tenant_id) "
                "VALUES ('fact', 'unscoped', $1, $2)", VAULT_MARKER, world.tenant)


async def test_a_forged_workspace_id_reaches_nothing(world, monkeypatch):
    """Binding a workspace the user is not in is not the same as being in it.

    `select_workspace` refuses this at the door, so a real request cannot get
    here. This binds the GUC directly — the shape of a bug that skipped the
    verification — and shows the database refuses independently.
    """
    p = principal_for(world.admin, world.tenant)
    _patch_resolver(monkeypatch, p)
    in_a = await resolve_workspace_principal("someone", str(world.ws_a))
    await _memory_in(in_a, "not reachable by a forged workspace")

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("SELECT set_config('app.tenant_id', $1, false)",
                           str(world.tenant))
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)",
                           str(uuid.uuid4()))
        rows = await conn.fetch(
            "SELECT content FROM memory_embeddings "
            " WHERE content = 'not reachable by a forged workspace'")
    finally:
        await conn.close()

    assert rows == []


async def test_an_unscoped_principal_cannot_open_a_workspace_connection(world):
    """No fallback. A principal with no workspace raises rather than resolving
    to the default — the single property Batch 5A was built around."""
    from core.identity.db_context import MissingWorkspaceContext

    p = principal_for(world.admin, world.tenant)  # no workspace_id
    with pytest.raises(MissingWorkspaceContext):
        async with workspace_connection(p):
            pass  # pragma: no cover


# ─────────────────────────────────────────────────────────────────────────────
# 4. Owner provisioning
# ─────────────────────────────────────────────────────────────────────────────


async def test_provisioning_gives_a_non_default_owner_name_real_memberships(monkeypatch):
    """`023` seeds rows for `private_user_id("admin")` only, and says other
    deployments get theirs "from the application on first login". This is that
    path, so a deployment with `IRA_ADMIN_USERNAME=ira` is not locked out of
    every workspace-scoped endpoint."""
    from config import get_settings
    from core.identity.provisioning import ensure_private_owner_identity

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    name = f"owner-{uuid.uuid4().hex[:8]}"
    settings = get_settings()
    monkeypatch.setattr(settings, "ira_admin_username", name, raising=False)

    result = await ensure_private_owner_identity()
    assert result is not None
    assert result.created_user and result.created_tenant_membership
    assert result.created_workspace_membership
    assert result.user_id == private_user_id(name)

    try:
        # And the principal it provisioned can now select a workspace.
        p = principal_for(
            result.user_id, PRIVATE_TENANT_ID,
            edition=EDITION_PRIVATE, is_owner=True, username=name,
        )
        _patch_resolver(monkeypatch, p)
        bound = await resolve_workspace_principal(name, None)
        assert bound.workspace_id == result.workspace_id
        assert bound.workspace_role == "workspace_admin"
    finally:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute("DELETE FROM workspace_memberships WHERE user_id=$1", result.user_id)
            await conn.execute("DELETE FROM tenant_memberships WHERE user_id=$1", result.user_id)
            await conn.execute("DELETE FROM users WHERE id=$1", result.user_id)
        finally:
            await conn.close()


async def test_provisioning_is_idempotent(monkeypatch):
    """Runs on every boot, so a second run must create nothing."""
    from config import get_settings
    from core.identity.provisioning import ensure_private_owner_identity

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    name = f"owner-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(get_settings(), "ira_admin_username", name, raising=False)

    first = await ensure_private_owner_identity()
    second = await ensure_private_owner_identity()
    try:
        assert first.created_user
        assert not (
            second.created_user
            or second.created_tenant_membership
            or second.created_workspace_membership
        )
    finally:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute("DELETE FROM workspace_memberships WHERE user_id=$1", first.user_id)
            await conn.execute("DELETE FROM tenant_memberships WHERE user_id=$1", first.user_id)
            await conn.execute("DELETE FROM users WHERE id=$1", first.user_id)
        finally:
            await conn.close()


async def test_provisioning_does_not_reinstate_a_suspended_membership(monkeypatch):
    """`ON CONFLICT DO NOTHING`, never `DO UPDATE`.

    An operator who suspends an account must not have it silently restored by
    the next restart. That would make suspension mean nothing and would be
    invisible — the only evidence is a row nobody re-reads.
    """
    from config import get_settings
    from core.identity.provisioning import ensure_private_owner_identity

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    name = f"owner-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(get_settings(), "ira_admin_username", name, raising=False)
    first = await ensure_private_owner_identity()

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "UPDATE tenant_memberships SET status='suspended' WHERE user_id=$1",
            first.user_id,
        )
        await ensure_private_owner_identity()
        status = await conn.fetchval(
            "SELECT status FROM tenant_memberships WHERE user_id=$1", first.user_id
        )
        assert status == "suspended"

        # And a suspended membership cannot select a workspace.
        p = principal_for(
            first.user_id, PRIVATE_TENANT_ID,
            edition=EDITION_PRIVATE, is_owner=True, username=name,
        )
        _patch_resolver(monkeypatch, p)
        with pytest.raises(HTTPException) as e:
            await resolve_workspace_principal(name, None)
        assert e.value.status_code == 403
    finally:
        await conn.execute("DELETE FROM workspace_memberships WHERE user_id=$1", first.user_id)
        await conn.execute("DELETE FROM tenant_memberships WHERE user_id=$1", first.user_id)
        await conn.execute("DELETE FROM users WHERE id=$1", first.user_id)
        await conn.close()


async def test_provisioning_is_a_no_op_in_the_cloud_edition(monkeypatch):
    """There is no ".env owner" in a multi-tenant process, and minting one
    would be exactly the private-tenant fallback every other module refuses."""
    from core.identity.provisioning import ensure_private_owner_identity

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)
    assert await ensure_private_owner_identity() is None
