"""The workspace picker's endpoints, against a real PostgreSQL.

`/api/v1/workspaces` is the one route a client can call before it knows any
workspace id, which makes it the one route where getting the scoping wrong is
invisible to every other guard: it cannot require the header, so it cannot rely
on `select_workspace` to filter for it, and it runs on a tenant-bound
connection where a missing `WHERE user_id = ...` would quietly return every
workspace in the tenant rather than the caller's own.

So this file asks, of the listing:

    would a user see a workspace they are not a member of?
    would a user see another tenant's workspace?
    would a user see an archived one, which they cannot select?
    would a user with revoked membership still see theirs?

and, of the verification:

    does `/select` refuse the same four cases, with one message?

Runs as `ira_app` — non-superuser, NOBYPASSRLS — because "RLS keeps the other
tenant's rows out" is a claim about the database, and a superuser connection
would make it true by accident.
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
        "IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
        allow_module_level=True,
    )

from fastapi import HTTPException  # noqa: E402

from api.routes.workspaces import SelectBody, list_workspaces, select  # noqa: E402
from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402
from core.identity.principal import Principal  # noqa: E402
from core.identity.workspace import WorkspaceSelectionError  # noqa: E402


@pytest.fixture(autouse=True)
async def app_pool():
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
    """One tenant with four workspaces in four different states, plus a second
    tenant whose workspace must never appear.

        ws_default   — the tenant's default; `member` is an active member
        ws_second    — active; `member` is an active member
        ws_unjoined  — active; `stranger` is a member and `member` is not
        ws_archived  — `member` IS a member, but the workspace is archived
    """
    w = World()
    w.tenant = uuid.uuid4()
    w.other_tenant = uuid.uuid4()
    w.member = uuid.uuid4()
    w.stranger = uuid.uuid4()

    async def build():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            for t, n in ((w.tenant, "wsl"), (w.other_tenant, "wsl-other")):
                await conn.execute(
                    "INSERT INTO tenants (id, name) VALUES ($1, $2)",
                    t, f"{n}-{t.hex[:8]}",
                )
            for u in (w.member, w.stranger):
                await conn.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)",
                    u, f"{u.hex[:12]}@example.test",
                )
            w.ws_default = await conn.fetchval(
                "SELECT provision_tenant_substrate($1)", w.tenant
            )
            w.ws_other = await conn.fetchval(
                "SELECT provision_tenant_substrate($1)", w.other_tenant
            )
            org = await conn.fetchval(
                "SELECT id FROM organisations WHERE tenant_id = $1", w.tenant
            )

            async def make(name, slug, status):
                ws = uuid.uuid4()
                # `workspaces_archived_at_agrees` refuses an archived row with
                # no archival timestamp — the schema does not let the two
                # disagree, so the fixture supplies both.
                await conn.execute(
                    """INSERT INTO workspaces
                         (id, tenant_id, organisation_id, name, slug, is_default,
                          status, archived_at)
                       VALUES ($1,$2,$3,$4,$5,false,$6,
                               CASE WHEN $6 = 'archived' THEN now() ELSE NULL END)""",
                    ws, w.tenant, org, name, slug, status,
                )
                return ws

            w.ws_second = await make("Second", "second", "active")
            w.ws_unjoined = await make("Unjoined", "unjoined", "active")
            w.ws_archived = await make("Archived", "archived", "archived")

            for u in (w.member, w.stranger):
                await conn.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
                       VALUES ($1,$2,'member','active')""",
                    w.tenant, u,
                )
            for ws in (w.ws_default, w.ws_second, w.ws_archived):
                await conn.execute(
                    """INSERT INTO workspace_memberships
                         (tenant_id, workspace_id, user_id, role, status)
                       VALUES ($1,$2,$3,'workspace_admin','active')""",
                    w.tenant, ws, w.member,
                )
            # `stranger` holds ws_unjoined. Without this the fixture cannot tell
            # a correct listing from one that forgot `WHERE m.user_id = $1`:
            # every membership row in the tenant would belong to `member`
            # anyway, so the missing filter would return the same set. Planting
            # that exact defect is how the gap was found.
            await conn.execute(
                """INSERT INTO workspace_memberships
                     (tenant_id, workspace_id, user_id, role, status)
                   VALUES ($1,$2,$3,'workspace_admin','active')""",
                w.tenant, w.ws_unjoined, w.stranger,
            )
        finally:
            await conn.close()

    async def teardown():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            for t in (w.tenant, w.other_tenant):
                await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM workspaces WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM organisations WHERE tenant_id=$1", t)
                await conn.execute("DELETE FROM tenants WHERE id=$1", t)
            for u in (w.member, w.stranger):
                await conn.execute("DELETE FROM users WHERE id=$1", u)
        finally:
            await conn.close()

    _run(build())
    yield w
    _run(teardown())


def principal_for(user_id, tenant_id, *, edition=EDITION_CLOUD) -> Principal:
    return Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=uuid.uuid4(),
        edition=edition,
    )


def _patch_resolver(monkeypatch, principal: Principal) -> None:
    """Cloud identity is Phase 2 work; the real resolver refuses in cloud."""

    async def fake(username, **kw):
        return principal

    monkeypatch.setattr("api.middleware.workspace.resolve_principal", fake)


async def _revoke_membership(tenant, workspace, user) -> None:
    """Remove a workspace membership the way the schema models removal.

    `status` is a closed vocabulary — invited / active / suspended / removed —
    and `workspace_memberships_status_removed_agree` refuses a removed row
    without a `removed_at`. Both are set, so the generated
    `workspace_grants_access` column goes false for the reason the database
    recognises rather than by a value it would reject.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            """UPDATE workspace_memberships
                  SET status = 'removed', removed_at = now()
                WHERE tenant_id=$1 AND workspace_id=$2 AND user_id=$3""",
            tenant, workspace, user,
        )
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# The listing
# ─────────────────────────────────────────────────────────────────────────────


async def test_lists_only_the_workspaces_the_caller_is_a_member_of(world, monkeypatch):
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    result = await list_workspaces("someone")

    ids = {w.id for w in result.workspaces}
    assert ids == {world.ws_default, world.ws_second}, (
        "the listing must be derived from workspace_memberships. "
        f"ws_unjoined={world.ws_unjoined} is in the caller's tenant and the "
        "caller is not a member of it."
    )
    assert result.count == 2


async def test_never_lists_another_tenants_workspace(world, monkeypatch):
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    result = await list_workspaces("someone")

    assert world.ws_other not in {w.id for w in result.workspaces}


async def test_does_not_list_an_archived_workspace(world, monkeypatch):
    """Listed-and-greyed would be worse than absent.

    `select_workspace` refuses an archived workspace, so showing one would
    offer a choice that cannot be made. Absence also gives the client a single
    rule — "gone from the list means gone" — that covers archived, deleted and
    revoked alike, which is what the server already reports identically.
    """
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    result = await list_workspaces("someone")

    assert world.ws_archived not in {w.id for w in result.workspaces}


async def test_a_revoked_membership_removes_it_from_the_listing(world, monkeypatch):
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))
    before = {w.id for w in (await list_workspaces("someone")).workspaces}
    assert world.ws_second in before

    await _revoke_membership(world.tenant, world.ws_second, world.member)

    after = {w.id for w in (await list_workspaces("someone")).workspaces}
    assert after == {world.ws_default}


async def test_two_members_of_one_tenant_see_different_workspaces(world, monkeypatch):
    """Tenant membership is not workspace membership — CLOUD-ADR-0016.

    Both users are verified members of the same tenant, and they must not see
    the same list. This is the test that a dropped `WHERE m.user_id = $1`
    fails; the earlier version of this fixture could not tell the difference,
    because every membership row in the tenant belonged to one user.
    """
    _patch_resolver(monkeypatch, principal_for(world.stranger, world.tenant))

    result = await list_workspaces("someone")

    assert {ws.id for ws in result.workspaces} == {world.ws_unjoined}, (
        "`stranger` holds exactly one workspace membership and must see "
        "exactly that workspace — not the two `member` holds."
    )


async def test_the_default_workspace_is_offered_first_but_not_chosen(world, monkeypatch):
    """Ordering is presentation. It is not a selection, and the response says
    nothing that a client could read as one."""
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    result = await list_workspaces("someone")

    assert result.workspaces[0].id == world.ws_default
    assert result.workspaces[0].is_default is True
    # There is no "selected", "current" or "active" field to mistake for a
    # decision the server made.
    assert not {"selected", "current", "active", "default_selection"} & set(
        result.model_dump()
    )


async def test_the_edition_states_whether_a_selection_is_required(world, monkeypatch):
    """The client asks rather than infers — both directions.

    Guessing "cloud" would block the private edition's sign-in behind a picker
    it does not need; guessing "private" would let a cloud user into the UI with
    nothing selected, to find out one 400 at a time.
    """
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)
    cloud = await list_workspaces("someone")
    assert cloud.edition == EDITION_CLOUD
    assert cloud.selection_required is True

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    private = await list_workspaces("someone")
    assert private.edition == EDITION_PRIVATE
    assert private.selection_required is False


# ─────────────────────────────────────────────────────────────────────────────
# The verification
# ─────────────────────────────────────────────────────────────────────────────


async def test_select_confirms_a_workspace_the_caller_may_use(world, monkeypatch):
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    result = await select(SelectBody(workspace_id=str(world.ws_second)), "someone")

    assert result.workspace_id == world.ws_second
    assert result.tenant_id == world.tenant
    assert result.role == "workspace_admin"
    assert "workspace.read" in result.capabilities


async def test_select_refuses_every_bad_case_identically(world, monkeypatch):
    """One message for five different reasons, on purpose.

    "Not a member", "another tenant's workspace", "archived", "no such
    workspace" and "not even a UUID" must be indistinguishable from outside.
    Distinguishing them tells a caller which workspace ids exist in tenants
    they cannot see.
    """
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    details = set()
    for bad in (
        str(world.ws_unjoined),   # in my tenant, not my membership
        str(world.ws_other),      # another tenant's
        str(world.ws_archived),   # mine, but archived
        str(uuid.uuid4()),        # does not exist
        "not-a-uuid",
    ):
        with pytest.raises(HTTPException) as exc:
            await select(SelectBody(workspace_id=bad), "someone")
        assert exc.value.status_code == 403, bad
        details.add(exc.value.detail)

    assert details == {WorkspaceSelectionError.PUBLIC_MESSAGE}


async def test_select_refuses_after_the_membership_is_revoked(world, monkeypatch):
    """The check is at the point of use, not at the point of listing.

    A tab left open holds a selection the client believes in. Revocation makes
    the next verification refuse, which is what turns "I had access" back into
    "I have access" on every request.
    """
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))
    assert (await select(SelectBody(workspace_id=str(world.ws_second)), "someone"))

    await _revoke_membership(world.tenant, world.ws_second, world.member)

    with pytest.raises(HTTPException) as exc:
        await select(SelectBody(workspace_id=str(world.ws_second)), "someone")
    assert exc.value.status_code == 403


async def test_the_listing_and_the_verifier_agree(world, monkeypatch):
    """Everything offered can be selected; nothing withheld can be.

    Two implementations of the same predicate — one a SQL `WHERE`, one
    `select_workspace`'s ordered checks — are two things that can drift. This
    is the test that notices.
    """
    _patch_resolver(monkeypatch, principal_for(world.member, world.tenant))

    offered = {w.id for w in (await list_workspaces("someone")).workspaces}

    for ws in (
        world.ws_default,
        world.ws_second,
        world.ws_unjoined,
        world.ws_archived,
        world.ws_other,
    ):
        try:
            await select(SelectBody(workspace_id=str(ws)), "someone")
            selectable = True
        except HTTPException:
            selectable = False
        assert selectable is (ws in offered), (
            f"workspace {ws} is {'offered' if ws in offered else 'withheld'} by "
            f"the listing but {'selectable' if selectable else 'refused'} by the "
            f"verifier. The picker and the check must not disagree."
        )
