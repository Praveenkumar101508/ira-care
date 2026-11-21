"""P0-03, against real PostgreSQL: nothing is erased or restored unconfirmed.

Two destructive routes, proved on the database rather than on mocks:

* **`POST /api/v1/privacy/delete`** asked for the scope to be typed back only
  for `everything` and `account`. `memories`, `conversations` and `voice` ran
  on one request carrying nothing but the scope, so one click erased every
  conversation a person had, across every store, irreversibly. Every scope now
  needs the word typed back, the confirmation the two widest scopes already
  had. The rows are counted before and after: a refusal that still deleted
  would pass a status-code test.

* **`POST /api/v1/backup/restore`** runs the uploaded SQL with `psql`. Here it
  really does: the archive creates a probe schema, and the schema exists only
  after the second request, with the token issued for that archive and the
  owner password. The unit tests in `tests/test_destructive_action_approvals.py`
  cover the other refusals with the restore stubbed.

Real PostgreSQL, as in the rest of this directory. Rows are seeded through the
admin DSN; the routes run through the app with only `require_auth` replaced.
"""
from __future__ import annotations

import gzip
import importlib.util
import os
import pathlib
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

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

_IRA = pathlib.Path(__file__).resolve().parents[2]


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


# ── Privacy deletion ──────────────────────────────────────────────────────────


@pytest.fixture
async def person(admin):
    """A cloud tenant with one resolvable member, a conversation with a message,
    and one memory of their own. Torn down in one cascading delete, as
    `test_p12r1_privacy_workspace_header.py` does, so nothing is left behind
    for `test_zz_no_fixture_residue`."""
    slug = f"p0-03-{uuid.uuid4().hex[:8]}"
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (name, slug, edition) VALUES ($1, $1, 'cloud') "
        "RETURNING id", slug)
    org_id = await admin.fetchval(
        "INSERT INTO organisations (tenant_id, name) VALUES ($1, 'P0-03') "
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
    conversation_id = await admin.fetchval(
        "INSERT INTO conversations (tenant_id, workspace_id, owner_user_id, "
        "                           session_id, title) "
        "VALUES ($1, $2, $3, $4, 'P0-03') RETURNING id",
        tenant_id, workspace_id, user_id, f"{slug}-session")
    await admin.execute(
        "INSERT INTO messages (conversation_id, tenant_id, workspace_id, "
        "                      owner_user_id, role, content) "
        "VALUES ($1, $2, $3, $4, 'user', 'kept until the person confirms')",
        conversation_id, tenant_id, workspace_id, user_id)
    await admin.execute(
        "INSERT INTO memory_embeddings (tenant_id, workspace_id, user_id, "
        "                               source_type, content) "
        "VALUES ($1, $2, $3, 'note', 'a memory kept until the person confirms')",
        tenant_id, workspace_id, str(user_id))
    try:
        yield SimpleNamespace(tenant_id=tenant_id, workspace_id=workspace_id,
                              user_id=user_id, email=f"{slug}@example.invalid",
                              conversation_id=conversation_id)
    finally:
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await admin.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture
def cloud_client(pool, monkeypatch):
    """An ASGI client in the cloud edition, authenticated as one email. Only
    `require_auth` is replaced; the workspace principal resolves for real."""
    from httpx import ASGITransport, AsyncClient

    from api.middleware.auth import require_auth
    from app.factory import create_app

    monkeypatch.setenv("IRA_EDITION", "cloud")

    def build(email: str):
        app = create_app()

        async def _who() -> str:
            return email

        app.dependency_overrides[require_auth] = _who
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p0-03.invalid")

    return build


async def _held(admin, p) -> dict:
    return {
        "conversations": await admin.fetchval(
            "SELECT count(*) FROM conversations WHERE owner_user_id = $1", p.user_id),
        "messages": await admin.fetchval(
            "SELECT count(*) FROM messages WHERE owner_user_id = $1", p.user_id),
        "memories": await admin.fetchval(
            "SELECT count(*) FROM memory_embeddings WHERE tenant_id = $1 "
            "AND user_id = $2", p.tenant_id, str(p.user_id)),
    }


def _headers(p) -> dict:
    return {"X-Workspace-ID": str(p.workspace_id), "X-Tenant-ID": str(p.tenant_id)}


@pytest.mark.parametrize("scope", ["memories", "conversations", "voice"])
async def test_a_bulk_deletion_without_the_scope_typed_back_erases_nothing(
        pool, admin, person, cloud_client, scope):
    """The gap: these three scopes ran on the scope alone."""
    before = await _held(admin, person)
    async with cloud_client(person.email) as client:
        response = await client.post("/api/v1/privacy/delete", json={"scope": scope},
                                     headers=_headers(person))
    after = await _held(admin, person)

    assert after == before, (
        f"POST /privacy/delete scope={scope!r} with no confirmation erased data: "
        f"{before} -> {after}")
    assert response.status_code == 400, (
        f"scope={scope!r} with no confirmation answered {response.status_code}: "
        f"{response.text}")
    # The app wraps errors in its own envelope; the route's sentence is in it.
    assert f"confirm={scope!r}" in response.text, response.text


async def test_the_wrong_word_typed_back_erases_nothing(pool, admin, person, cloud_client):
    before = await _held(admin, person)
    async with cloud_client(person.email) as client:
        response = await client.post(
            "/api/v1/privacy/delete",
            json={"scope": "conversations", "confirm": "memories"},
            headers=_headers(person))
    assert response.status_code == 400, response.text
    assert await _held(admin, person) == before


async def test_the_person_can_still_delete_their_conversations_by_confirming(
        pool, admin, person, cloud_client):
    """The legitimate flow: the scope typed back, and the rows really go."""
    before = await _held(admin, person)
    assert before["conversations"] == 1 and before["messages"] == 1

    async with cloud_client(person.email) as client:
        response = await client.post(
            "/api/v1/privacy/delete",
            json={"scope": "conversations", "confirm": "conversations"},
            headers=_headers(person))

    assert response.status_code == 200, response.text
    after = await _held(admin, person)
    assert after["conversations"] == 0, "the confirmed deletion left the conversation"
    assert after["messages"] == 0, "the confirmed deletion left the message"
    assert after["memories"] == before["memories"], (
        "deleting conversations must not take the person's saved memories")


async def test_the_person_can_still_delete_their_memories_by_confirming(
        pool, admin, person, cloud_client):
    async with cloud_client(person.email) as client:
        response = await client.post(
            "/api/v1/privacy/delete",
            json={"scope": "memories", "confirm": "memories"},
            headers=_headers(person))

    assert response.status_code == 200, response.text
    after = await _held(admin, person)
    assert after["memories"] == 0, "the confirmed deletion left the memory"
    assert after["conversations"] == 1, "deleting memories must not take conversations"


# ── Backup restore, with psql ─────────────────────────────────────────────────


def _admin_parts() -> dict:
    """Host, port, user, password and database from the admin DSN, in either
    shape this repository uses (TCP in CI, a socket directory locally; see
    `_dsn.py`)."""
    parts = urlsplit(ADMIN_DSN)
    query = parse_qs(parts.query)
    return {
        "host": parts.hostname or (query.get("host") or ["localhost"])[0],
        "port": parts.port or int((query.get("port") or ["5432"])[0]),
        "user": unquote(parts.username or "postgres"),
        "password": unquote(parts.password or ""),
        "db": parts.path.lstrip("/"),
    }


def _real_worker_backup():
    """`tests/conftest.py` stubs `worker.backup`; load the real file, as
    `test_backup_files_are_private.py` does."""
    spec = importlib.util.spec_from_file_location(
        "worker._backup_real_p0_03", _IRA / "worker" / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def probe_schema(admin):
    name = f"p0_03_restore_probe_{uuid.uuid4().hex[:10]}"
    try:
        yield name
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def owner_restore_client(tmp_path, monkeypatch):
    """The backup router in the private edition, as the owner, with the real
    `restore_from_file` pointed at the test database."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    import api.routes.backup as route
    from api.middleware.auth import _admin_password_hash, require_auth
    from config import get_settings

    monkeypatch.setenv("IRA_EDITION", "private")
    _admin_password_hash.cache_clear()

    db = _admin_parts()
    settings = SimpleNamespace(
        backup_dir=str(tmp_path / "backups"),
        postgres_host=db["host"], postgres_port=db["port"],
        postgres_user=db["user"], postgres_password=db["password"],
        postgres_db=db["db"],
    )
    real = _real_worker_backup()
    monkeypatch.setattr(real, "get_settings", lambda: settings)
    monkeypatch.setattr(route, "get_settings", lambda: settings)
    monkeypatch.setattr(route, "restore_from_file", real.restore_from_file)

    async def no_snapshot():
        return None   # the pre-restore pg_dump is not the subject here

    monkeypatch.setattr(route, "run_database_backup", no_snapshot)

    owner = get_settings().ira_admin_username
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: owner
    yield SimpleNamespace(
        client=lambda: AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://p0-03.invalid"),
        password=get_settings().ira_admin_password)
    _admin_password_hash.cache_clear()


async def _schema_exists(admin, name: str) -> bool:
    return await admin.fetchval("SELECT to_regnamespace($1) IS NOT NULL", name)


# The route checks lockdown before anything else. This test is about the token
# and password reaching the database, so lockdown is pinned off: read for real,
# the state would depend on what the lockdown suites left in Redis and
# PostgreSQL. Restore's refusal during lockdown is tested in
# tests/test_destructive_action_approvals.py.
@pytest.mark.usefixtures("lockdown_inactive")
async def test_a_restore_reaches_the_database_only_with_the_token_and_password(
        admin, probe_schema, owner_restore_client):
    archive = gzip.compress(
        f'CREATE SCHEMA "{probe_schema}";\n'
        f'CREATE TABLE "{probe_schema}".restored (note text);\n'
        f"INSERT INTO \"{probe_schema}\".restored VALUES ('restored by P0-03');\n".encode())

    def upload(**form):
        return {"files": {"file": ("backup.sql.gz", archive, "application/gzip")},
                "data": {k: v for k, v in form.items() if v is not None}}

    async with owner_restore_client.client() as client:
        # One request, carrying the fixed header the route used to accept.
        staged = await client.post("/api/v1/backup/restore",
                                   headers={"X-Restore-Confirm": "yes"}, **upload())
        assert staged.status_code == 200, staged.text
        assert not await _schema_exists(admin, probe_schema), (
            "the archive's SQL ran on the request that only staged it")
        token = staged.json()["token"]

        no_password = await client.post("/api/v1/backup/restore",
                                        **upload(confirm_token=token))
        assert no_password.status_code == 403, no_password.text
        assert not await _schema_exists(admin, probe_schema), (
            "the archive's SQL ran without the owner password")

        restored = await client.post(
            "/api/v1/backup/restore",
            **upload(confirm_token=token, owner_password=owner_restore_client.password))

    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "restored"
    assert await _schema_exists(admin, probe_schema), (
        "the owner confirmed with the token and password, and the archive was "
        "not applied")
    assert await admin.fetchval(
        f'SELECT note FROM "{probe_schema}".restored') == "restored by P0-03"
