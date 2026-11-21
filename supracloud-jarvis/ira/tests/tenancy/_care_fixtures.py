"""Shared fixtures for the IRA CARE database-backed suites.

Not a test module (no `test_` prefix). Two families, each a tenant with a
default workspace and three users (an administrator, a member and a viewer),
created as the privileged role and removed afterwards; an ASGI client whose
principal can be switched between calls; and a connection pool installed where
`utils.db.acquire()` finds it.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")
# Access notes are sealed under the connector encryption key: a fixed TEST key, 32 bytes.
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", "dGVzdC1rZXktZm9yLWlyYS1jYXJlLXRlc3RzLTAwMDE=")

_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


def _ws_of(tenant: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant}")


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


async def _make_family(pool, admin, key: str) -> dict:
    tenant_id, admin_user, member_user, viewer_user = (uuid.uuid4() for _ in range(4))
    ws = _ws_of(tenant_id)
    slug = f"careapi-{key}-{tenant_id.hex[:8]}"
    await admin.execute("INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                        tenant_id, slug, slug)
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(admin_user))
        org = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name) VALUES ($1,$2) RETURNING id", tenant_id, slug)
        await conn.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default,status) "
            "VALUES ($1,$2,$3,'Default','default',TRUE,'active')", ws, tenant_id, org)
        for uid in (admin_user, member_user, viewer_user):
            await conn.execute(
                "INSERT INTO users (id,email,display_name,status) VALUES ($1,$2,'care probe','active')",
                uid, f"{slug}-{uid.hex[:8]}@example.invalid")
        for k in ("app.tenant_id", "app.user_id", "app.workspace_id"):
            await conn.execute(f"SELECT set_config('{k}','',false)")
    return {"tenant_id": tenant_id, "workspace_id": ws, "admin": admin_user,
            "member": member_user, "viewer": viewer_user}


@pytest.fixture
async def families(pool):
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        a = await _make_family(pool, admin, "a")
        b = await _make_family(pool, admin, "b")
        yield {"a": a, "b": b}
        for fam in (a, b):
            await admin.execute("DELETE FROM care_access_log WHERE tenant_id = $1", fam["tenant_id"])
            await admin.execute("DELETE FROM tenants WHERE id = $1", fam["tenant_id"])
            await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])",
                                [fam["admin"], fam["member"], fam["viewer"]])
    finally:
        await admin.close()


def _principal(fam: dict, who: str, role: str):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    return Principal(user_id=fam[who], tenant_id=fam["tenant_id"], session_id=uuid.uuid4(),
                     edition=EDITION_CLOUD, workspace_id=fam["workspace_id"], workspace_role=role)


class _Client:
    """One ASGI client whose principal can be switched between calls."""

    def __init__(self, app, http):
        self.app, self.http, self.principal = app, http, None

    def act_as(self, fam, who, role):
        self.principal = _principal(fam, who, role)
        return self

    def __getattr__(self, name):
        return getattr(self.http, name)


@pytest.fixture
async def client(pool):
    from httpx import ASGITransport, AsyncClient

    from api.middleware.auth import require_auth
    from api.middleware.workspace import require_workspace_principal
    from app.factory import create_app

    app = create_app()
    holder = _Client(app, None)

    async def _auth():
        return str(holder.principal.user_id)

    async def _p():
        return holder.principal

    app.dependency_overrides[require_auth] = _auth
    app.dependency_overrides[require_workspace_principal] = _p
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://care.invalid") as http:
        holder.http = http
        yield holder
    app.dependency_overrides.clear()


