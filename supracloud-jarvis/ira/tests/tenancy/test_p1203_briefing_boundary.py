"""P12-03 finding G — a briefing is one workspace's, and the route agrees.

Before `081`, `briefings` had no `tenant_id`, no `workspace_id`, no row-level
security and no policies, and `GET /api/v1/briefing/latest` read it on a bare
`acquire()` behind `require_auth` with no predicate at all. The most recent
briefing generated anywhere in the deployment was returned, `content` and all,
to any authenticated user.

That content is the most concentrated customer data in the schema:
`worker/briefing.py` assembles it from four workspace-scoped tables through
correctly bound connections — P12-01 observed all four binding — so the gather
was right and only the storage and the read were wrong.

These tests are written at two levels on purpose. The database-level ones prove
the policy; the HTTP ones prove the ROUTE goes through it. P12-01's whole
lesson is that those are different claims, and the second is the one that was
false here while the first would have looked fine.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


@pytest.fixture
async def pool():
    import utils.db as db_module

    # max_size=1: the same connection serves every scope below, so a leaked
    # `app.workspace_id` would be found rather than missed (D3 item 9).
    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=1)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


async def _tenant(conn, label: str, *, workspaces: int = 1):
    """A tenant with `workspaces` active workspaces and one member in each."""
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    slug = f"p1203-{tenant_id.hex[:10]}"
    await conn.execute(
        "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
        tenant_id, f"P12-03 {label} {slug}", slug)
    await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
    await conn.execute("SELECT set_config('app.user_id',$1,false)", str(user_id))
    org = await conn.fetchval(
        "INSERT INTO organisations (tenant_id,name) VALUES ($1,'P12-03') RETURNING id",
        tenant_id)
    await conn.execute(
        "INSERT INTO users (id,email,display_name,status) "
        "VALUES ($1,$2,'P12-03','active')",
        user_id, f"p1203-{user_id.hex[:12]}@example.invalid")
    await conn.execute(
        "INSERT INTO tenant_memberships (tenant_id,user_id,role,status,joined_at) "
        "VALUES ($1,$2,'owner','active',now())", tenant_id, user_id)

    ws_ids = []
    for i in range(workspaces):
        ws = uuid.uuid5(_NAMESPACE, f"workspace:{tenant_id}:{i}")
        await conn.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
            "                        is_default,status) "
            "VALUES ($1,$2,$3,$4,$5,$6,'active')",
            ws, tenant_id, org, f"W{i}", f"w{i}", i == 0)
        await conn.execute(
            "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,"
            "                                   role,status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", tenant_id, ws, user_id)
        ws_ids.append(ws)

    await conn.execute("SELECT set_config('app.tenant_id','',false)")
    await conn.execute("SELECT set_config('app.user_id','',false)")
    return {"tenant_id": tenant_id, "user_id": user_id, "workspaces": ws_ids}


def _principal(t, workspace_id):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=t["user_id"], tenant_id=t["tenant_id"], session_id=uuid.uuid4(),
        edition=EDITION_CLOUD, workspace_id=workspace_id,
        workspace_role="workspace_admin")


@pytest.fixture
async def world(pool):
    """Tenant A with two workspaces, tenant B with one."""
    async with pool.acquire() as conn:
        a = await _tenant(conn, "A", workspaces=2)
        b = await _tenant(conn, "B")
    yield a, b
    async with pool.acquire() as conn:
        for t in (a, b):
            await conn.execute("SELECT set_config('app.tenant_id',$1,false)",
                               str(t["tenant_id"]))
            await conn.execute(
                "DELETE FROM workspace_memberships WHERE tenant_id = $1",
                t["tenant_id"])
            await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id = $1",
                               t["tenant_id"])
        await conn.execute("SELECT set_config('app.tenant_id','',false)")


async def _write_briefing(principal, text: str):
    from core.identity.db_context import workspace_connection

    async with workspace_connection(principal) as conn:
        return await conn.fetchval(
            "INSERT INTO briefings (briefing_type, content, summary) "
            "VALUES ('morning', $1, 'summary') RETURNING id", text)


# ── The boundary, at the database ───────────────────────────────────────────


async def test_the_disclosure_is_closed_at_the_database(world, pool):
    """The exact query that leaked, now returning nothing.

    Reproduces the finding rather than describing it: write a briefing as
    tenant A, then run the route's original statement — `ORDER BY created_at
    DESC LIMIT 1`, no predicate — on a connection bound to tenant B.
    """
    a, b = world
    await _write_briefing(_principal(a, a["workspaces"][0]),
                          "TENANT-A CONFIDENTIAL: revenue 1.2M")

    from core.identity.db_context import workspace_connection

    async with workspace_connection(_principal(b, b["workspaces"][0])) as conn:
        row = await conn.fetchrow(
            "SELECT content FROM briefings WHERE briefing_type='morning' "
            " ORDER BY created_at DESC LIMIT 1")

    assert row is None, (
        f"tenant B read tenant A's briefing: {row and row['content']!r}. This "
        f"is finding G, and the query is the one the route actually ran.")


async def test_a_second_workspace_in_the_same_tenant_cannot_read_it(world):
    """The boundary `049` exists for, applied to the aggregate of its tables.

    Cross-tenant is refused by `tenant_id` alone and has been since Phase 2.
    Workspace A2 shares tenant A's tenant, so only the workspace half of the
    predicate can refuse this one.
    """
    a, _ = world
    await _write_briefing(_principal(a, a["workspaces"][0]), "WORKSPACE-1 ONLY")

    from core.identity.db_context import workspace_connection

    async with workspace_connection(_principal(a, a["workspaces"][1])) as conn:
        rows = await conn.fetch("SELECT content FROM briefings")

    assert rows == [], (
        "a second workspace in the same tenant read the first workspace's "
        "briefing — the summary of its tasks, calendar, business and security "
        "data")


async def test_an_unbound_write_is_refused_rather_than_stored(world, pool):
    """Provenance is mandatory: a briefing with no workspace is unrepresentable.

    081 made both columns NOT NULL with defaults read from the bound GUCs, so a
    writer with nothing bound produces NULL and is refused — loudly, at the
    write, rather than by producing a row nobody can attribute later.
    """
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO briefings (briefing_type, content) "
                "VALUES ('morning','no provenance')")


async def test_a_returned_connection_carries_no_briefing_scope(world, pool):
    """D3 item 9, on this table specifically.

    The pool holds one connection, so the unbound read below is guaranteed to
    reuse the one workspace A just wrote through.
    """
    a, _ = world
    await _write_briefing(_principal(a, a["workspaces"][0]), "POOL PROBE")

    from utils.db import acquire

    async with acquire() as conn:
        leaked = await conn.fetchval(
            "SELECT current_setting('app.workspace_id', true)")
        rows = await conn.fetch("SELECT content FROM briefings")

    assert not leaked, f"the connection came back carrying workspace {leaked!r}"
    assert rows == [], "an unbound connection read a briefing"


# ── The boundary, at the route ──────────────────────────────────────────────


@pytest.fixture
def client_for(world, pool):
    """An ASGI client whose auth is overridden to ONE named principal.

    The auth dependency is replaced here and the workspace one is NOT: this
    file's subject is whether the briefing routes go through
    `require_workspace_principal` and `workspace_connection` at all, which was
    the half that was wrong. P12-02's `test_p1202_workspace_chain.py` is where
    the full unfaked identity chain is proved, and it stays the place for that.
    """
    from httpx import ASGITransport, AsyncClient

    from api.middleware.workspace import require_workspace_principal
    from app.factory import create_app

    def build(principal):
        app = create_app()

        async def _p():
            return principal

        app.dependency_overrides[require_workspace_principal] = _p
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p1203.invalid")

    return build


async def test_the_route_does_not_serve_another_tenants_briefing(world, client_for):
    """`GET /api/v1/briefing/latest`, the endpoint that leaked."""
    a, b = world
    await _write_briefing(_principal(a, a["workspaces"][0]),
                          "TENANT-A CONFIDENTIAL: revenue 1.2M")

    async with client_for(_principal(b, b["workspaces"][0])) as client:
        latest = await client.get("/api/v1/briefing/latest")
        history = await client.get("/api/v1/briefing/history")

    assert "CONFIDENTIAL" not in latest.text, (
        f"the route served another tenant's briefing content: {latest.text[:300]}")
    assert "CONFIDENTIAL" not in history.text
    # Absence is the point, but so is the shape of the answer: an empty result
    # must read as "you have none", not as a briefing belonging to nobody.
    assert latest.status_code == 200
    assert history.json() == []


async def test_the_route_does_not_cross_workspaces_within_a_tenant(world, client_for):
    a, _ = world
    await _write_briefing(_principal(a, a["workspaces"][0]), "WORKSPACE-1 ONLY")

    async with client_for(_principal(a, a["workspaces"][1])) as client:
        latest = await client.get("/api/v1/briefing/latest")

    assert "WORKSPACE-1 ONLY" not in latest.text, (
        "the route served workspace 1's briefing to workspace 2")


async def test_the_route_returns_the_callers_own_briefing(world, client_for):
    """The negative tests above would all pass if the route returned nothing ever.

    This is the control that stops "deny everything" from looking like a fix —
    the same reason `test_the_completion_guard_is_not_vacuous` exists.
    """
    a, _ = world
    await _write_briefing(_principal(a, a["workspaces"][0]), "MINE, AND VISIBLE")

    async with client_for(_principal(a, a["workspaces"][0])) as client:
        latest = await client.get("/api/v1/briefing/latest")

    assert latest.status_code == 200, latest.text
    assert "MINE, AND VISIBLE" in latest.text, (
        f"the caller could not read their OWN workspace's briefing, so the "
        f"boundary is refusing everything rather than the right things: "
        f"{latest.text[:300]}")


async def test_the_routes_demand_a_workspace(world):
    """Missing workspace authority fails explicitly, and is not served as [].

    D3 item 10. Under 081's predicate an unbound read matches nothing, so a
    route that tolerated a missing workspace would answer "no briefings yet" —
    which is the plausible-empty failure this phase exists to remove. Asserted
    by DEPENDENCY rather than by response, because the guarantee is that the
    request never reaches the query.
    """
    from api.middleware.workspace import require_workspace_principal
    from api.routes.briefing import router

    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        deps = [d.call for d in dependant.dependencies] if dependant else []
        assert require_workspace_principal in deps, (
            f"{sorted(route.methods)} {route.path} does not require a verified "
            f"workspace. Under 081 it would read zero rows and report them as "
            f"'no briefings yet'.")
