"""P12-R1, FINDING CB — the Privacy Centre's own dependency, unfaked.

The privacy surface is the one place a user is told to go to exercise their
data rights. `core/privacy/disclosure.py` names it in the sentence the page
renders: *"You can export or delete your data at any time from the Privacy
Centre."*

**In the cloud edition every route in that module answered 400.**
`api/routes/privacy._principal` read `X-Tenant-ID` and hard-coded the workspace
as `None`, and `resolve_workspace_principal` answers that with
`WorkspaceRequired` — *"the server does not select one on your behalf"*, which
is the correct rule applied to a request the client had no way to make
correctly.

── Why nothing caught it ──────────────────────────────────────────────────

`test_p1205_data_export.py` drives the same routes through the same app and is
right to: its subject is what the export CONTAINS. To fix a caller it
**overrides `_principal`** — so the one component that was broken is the one
component replaced. That is Phase 12's own third lesson recurring on a third
control: *an audit is only as wide as its predicate.*

So this file overrides `require_auth` and **nothing else**. Authentication is a
different subject with its own suites; the workspace resolution under test is
the real one, reached the way a request reaches it, with the headers a browser
sends and against real PostgreSQL.
"""
from __future__ import annotations

import os
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


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def _tenant(admin, label: str) -> dict:
    """A cloud tenant with a default workspace and one RESOLVABLE member.

    `email_verified_at` is set deliberately: `_resolve_cloud_principal` requires
    it, and a fixture that omits it produces a 503 *"no resolvable cloud
    identity"* — which would look exactly like the refusal under test while
    having nothing to do with workspaces.
    """
    slug = f"p12r1-{label}-{uuid.uuid4().hex[:8]}"
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (name, slug, edition) VALUES ($1, $1, 'cloud') "
        "RETURNING id", slug)
    org_id = await admin.fetchval(
        "INSERT INTO organisations (tenant_id, name) VALUES ($1, 'P12-R1') "
        "RETURNING id", tenant_id)
    workspace_id = await admin.fetchval(
        "INSERT INTO workspaces (tenant_id, organisation_id, name, slug, "
        "                        is_default, status) "
        "VALUES ($1, $2, 'Default', 'default', true, 'active') RETURNING id",
        tenant_id, org_id)
    user_id = await admin.fetchval(
        "INSERT INTO users (email, display_name, status, email_verified_at) "
        "VALUES ($1, $2, 'active', now()) RETURNING id",
        f"{slug}@example.invalid", slug)
    await admin.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
        "VALUES ($1, $2, 'owner', 'active')", tenant_id, user_id)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, "
        "                                   role, status) "
        "VALUES ($1, $2, $3, 'workspace_admin', 'active')",
        tenant_id, workspace_id, user_id)
    return {"tenant_id": tenant_id, "workspace_id": workspace_id,
            "user_id": user_id, "slug": slug,
            "email": f"{slug}@example.invalid"}


@pytest.fixture
async def two_tenants(admin):
    a = await _tenant(admin, "a")
    b = await _tenant(admin, "b")
    yield a, b

    # ── Teardown that cannot half-complete ──────────────────────────────────
    #
    # `iratest` is shared, and `test_zz_no_fixture_residue` fails the WHOLE
    # directory over one tenant left holding memberships — because
    # `rollback/035` refuses while more than one does, three stages later, with
    # an error naming 035 and nothing pointing back at the fixture.
    #
    # Every child of `tenants` cascades, so ONE delete removes all of them and
    # there is no order to get wrong; and each tenant is torn down
    # independently, so a failure on the first cannot leave the second
    # standing. Both properties are `test_p1205_data_export.py`'s, learned
    # there from five leaked tenants and copied here rather than rediscovered.
    failures = []
    for t in (a, b):
        try:
            await admin.execute("DELETE FROM tenants WHERE id = $1", t["tenant_id"])
            # `users` carries no tenant_id, so it is the one row that does not
            # cascade.
            await admin.execute("DELETE FROM users WHERE id = $1", t["user_id"])
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            failures.append(f"{t['slug']}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "the fixture leaked a tenant holding memberships, which fails the "
        f"rollback step in a job stage that cannot point back here: {failures}")


@pytest.fixture
def cloud_client(pool, monkeypatch):
    """An ASGI client in the CLOUD edition, authenticated as one email.

    Only `require_auth` is overridden. `_principal` — the thing that was wrong —
    resolves for real, from the headers the request carries.
    """
    from httpx import ASGITransport, AsyncClient

    from api.middleware.auth import require_auth
    from app.factory import create_app

    # `IRA_EDITION`, not a patched `current_edition`. The function reads the
    # environment on every call, and several modules bind the NAME at import
    # time — `core/identity/resolver.py` among them. Patching the attribute on
    # `core.editions` therefore moved the workspace layer to cloud and left the
    # identity layer in private, which resolved a synthetic uuid5 principal with
    # no membership and refused 403 for a reason that had nothing to do with the
    # subject of this file. One source, read by everything, cannot half-apply.
    monkeypatch.setenv("IRA_EDITION", "cloud")

    def build(email: str):
        app = create_app()

        async def _who() -> str:
            return email

        app.dependency_overrides[require_auth] = _who
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p12r1.invalid")

    return build


async def test_the_privacy_surface_works_in_cloud_when_a_workspace_is_named(
        pool, admin, two_tenants, cloud_client):
    """The regression. Before P12-R1 this was 400, for every route here."""
    a, _ = two_tenants

    async with cloud_client(a["email"]) as client:
        response = await client.get(
            "/api/v1/privacy/overview",
            headers={"X-Workspace-ID": str(a["workspace_id"]),
                     "X-Tenant-ID": str(a["tenant_id"])},
        )

    assert response.status_code == 200, (
        "the Privacy Centre's overview was refused in the cloud edition with a "
        "workspace correctly named. This is FINDING CB: `_principal` passed "
        f"`None` as the requested workspace. Body: {response.text}")
    assert response.json()["edition"] == "cloud"


@pytest.mark.parametrize(
    "path",
    ["/api/v1/privacy/overview", "/api/v1/privacy/memories",
     "/api/v1/privacy/export?scope=memories"],
)
async def test_every_read_route_here_accepts_the_workspace_header(
        pool, admin, two_tenants, cloud_client, path):
    """Not just the one route. `_principal` is shared by the whole module, so a
    fix applied to one endpoint and not the others would pass a single-route
    test and leave the surface broken."""
    a, _ = two_tenants

    async with cloud_client(a["email"]) as client:
        response = await client.get(
            path,
            headers={"X-Workspace-ID": str(a["workspace_id"]),
                     "X-Tenant-ID": str(a["tenant_id"])},
        )

    assert response.status_code == 200, f"{path} → {response.status_code}: {response.text}"


async def test_no_workspace_named_is_still_refused_in_cloud(
        pool, admin, two_tenants, cloud_client):
    """The fix must not become "the server picks one".

    `WorkspaceRequired` was the right answer to a request naming no workspace.
    What was wrong was that the client had no way to name one. If this ever
    starts returning 200, the server has begun choosing a workspace on the
    caller's behalf — which is the failure P12-02 exists to prevent, and it
    would be invisible from the browser because the page would simply work.
    """
    a, _ = two_tenants

    async with cloud_client(a["email"]) as client:
        response = await client.get(
            "/api/v1/privacy/overview",
            headers={"X-Tenant-ID": str(a["tenant_id"])},
        )

    assert response.status_code == 400, (
        "a cloud request naming no workspace was answered "
        f"{response.status_code}. The server must not select one: "
        f"{response.text}")
    assert "X-Workspace-ID" in response.text


async def test_another_tenants_workspace_is_refused(
        pool, admin, two_tenants, cloud_client):
    """The header is a REQUEST for a workspace, never an assertion of one.

    Without this, "reads the header" and "trusts the header" are the same
    change, and the second is a cross-tenant disclosure.
    """
    a, b = two_tenants

    async with cloud_client(a["email"]) as client:
        response = await client.get(
            "/api/v1/privacy/overview",
            headers={"X-Workspace-ID": str(b["workspace_id"]),
                     "X-Tenant-ID": str(a["tenant_id"])},
        )

    assert response.status_code == 403, (
        "tenant A named tenant B's workspace and was answered "
        f"{response.status_code}: {response.text}")


async def test_the_export_route_is_reachable_and_scoped(
        pool, admin, two_tenants, cloud_client):
    """FINDING CA's endpoint, reached the way the Privacy Centre now reaches it.

    The page is what makes this route reachable by a person; this is what makes
    it reachable at all in the edition the promise is made in.
    """
    a, _ = two_tenants

    async with cloud_client(a["email"]) as client:
        response = await client.get(
            "/api/v1/privacy/export?scope=memories",
            headers={"X-Workspace-ID": str(a["workspace_id"]),
                     "X-Tenant-ID": str(a["tenant_id"])},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["tables"], "the export reported no tables at all"
    # Every table carries the boundary it was read under, including the empty
    # ones — the property that keeps "an empty account" distinguishable from
    # "an export that bound nothing".
    assert all(t["boundary"] for t in body["tables"])
    assert str(a["workspace_id"]) in {t["workspace_id"] for t in body["tables"]}
