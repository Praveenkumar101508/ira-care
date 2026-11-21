"""The Trust Ledger fails closed, and the journal replays into it: real PostgreSQL
(P1-09, failure mode 12).

── What is real ────────────────────────────────────────────────────────────

PostgreSQL as `ira_app`, under FORCE RLS, with the real `append_record`, its
advisory lock, its `journal_entry_id` check and its post-commit hook. The real
memory router over ASGI with the real `require_workspace_principal`, and the
real backup router with the real `psql` restore, as in
`test_owner_gated_actions_are_recorded.py`. The journal is the real one, in a
per-test directory. Records are read back with `recent_records` and checked
with `verify_chain`.

── How the ledger is "down" while the database is up ───────────────────────

`INSERT` on `trust_ledger` is revoked from `ira_app` for the length of the
outage, and granted back in `finally`. Every other table works, so the memory
route can read the memory and the restore can run, and the ledger write fails
inside the real `append_record`, in the real transaction, exactly as an
unwritable ledger does. It is this test database's grant, and only for the
test.

── Negative controls (planted and watched fail; see
   docs/testing/evidence/ledger-fail-closed-2026-11-01/) ────────────────────

* `_run_and_record` fail-soft again → the Class A test fails (the memory is
  forgotten while the ledger is down)
* `append_record` ignoring `journal_entry_id` → the duplicate test fails
* the replay skipping the chain check → the tamper test fails
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.editions import EDITION_PRIVATE  # noqa: E402
from core.identity.principal import (  # noqa: E402
    PRIVATE_TENANT_ID,
    Principal,
    system_principal,
)
from memory.namespaces import VAULT_NAMESPACE  # noqa: E402
from trust import journal as J  # noqa: E402
from trust.ledger import append_record, export_head, recent_records, verify_chain  # noqa: E402

pytestmark = [
    pytest.mark.usefixtures("lockdown_inactive"),
    pytest.mark.skipif(J._fcntl is None, reason="the journal needs POSIX locking"),
]

OWNER_USERNAME = "admin"
CANARY = "LEDGER-CANARY-MARKER-p109pg"
_IRA = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield pool
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    monkeypatch.delenv("IRA_EDITION", raising=False)


@pytest.fixture(autouse=True)
def unbound_request_context():
    from core.runtime import request_context

    request_context.clear()
    yield
    request_context.clear()


@asynccontextmanager
async def ledger_down():
    """`trust_ledger` refuses INSERTs from `ira_app` until the block ends."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await admin.fetchval(
            "SELECT has_table_privilege('ira_app', 'trust_ledger', 'INSERT')")
        await admin.execute("REVOKE INSERT ON trust_ledger FROM ira_app")
        try:
            yield
        finally:
            await admin.execute("GRANT INSERT ON trust_ledger TO ira_app")
    finally:
        await admin.close()


@pytest.fixture
async def world(monkeypatch):
    """A private-edition tenant whose owner is workspace_admin, with one memory."""
    w = SimpleNamespace()
    w.tenant, w.owner = uuid.uuid4(), uuid.uuid4()
    tag = w.tenant.hex[:8]
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                            w.tenant, f"ledger-fc-{tag}")
        await admin.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                            w.owner, f"{w.owner.hex[:12]}@example.test")
        w.ws = await admin.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", w.tenant, w.owner)
        await admin.execute(
            "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", w.tenant, w.ws, w.owner)
        w.memory = await admin.fetchval(
            "INSERT INTO memory_embeddings (tenant_id, workspace_id, user_id, "
            " source_type, source, memory_type, content) "
            "VALUES ($1,$2,$3,'fact','owner_chat','fact',$4) RETURNING id",
            w.tenant, w.ws, VAULT_NAMESPACE, f"My locker code is {CANARY} ({tag})")
        principal = Principal(
            user_id=w.owner, tenant_id=w.tenant, session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE, roles=frozenset({"primary_owner"}),
            username=OWNER_USERNAME, is_owner=True)

        import api.middleware.workspace as ws_module

        async def fake_resolve(username, **kw):
            return principal

        monkeypatch.setattr(ws_module, "resolve_principal", fake_resolve)
        w.admin = admin
        yield w
    finally:
        try:
            await admin.execute("DELETE FROM memory_embeddings WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM workspace_memberships WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM tenant_memberships WHERE tenant_id=$1", w.tenant)
            await admin.execute("DELETE FROM users WHERE id=$1", w.owner)
        finally:
            await admin.close()


def _vault_client():
    import httpx
    from fastapi import FastAPI

    from api.middleware.auth import require_auth
    from api.routes.memory import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: OWNER_USERNAME
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://ledger-fc.test")


async def _rows(tenant) -> list[dict]:
    rows = await recent_records(limit=250, principal=system_principal(
        tenant_id=tenant, reason="ledger-fc-test"))
    return sorted(rows, key=lambda r: r["seq"])


async def _memory_left(w) -> bool:
    return bool(await w.admin.fetchval(
        "SELECT count(*) FROM memory_embeddings WHERE id = $1", w.memory))


# ── Class A, end to end ──────────────────────────────────────────────────────


async def test_class_a_forget_is_refused_while_the_ledger_is_down_and_the_retry_is_recorded(
        world):
    headers = {"X-Workspace-ID": str(world.ws)}
    async with _vault_client() as client:
        draft = await client.delete(f"/api/v1/memory/{world.memory}", headers=headers)
        assert draft.status_code == 200, draft.text
        token = draft.json()["token"]

        async with ledger_down():
            refused = await client.delete(f"/api/v1/memory/{world.memory}",
                                          params={"confirm_token": token}, headers=headers)
        assert refused.status_code == 503, refused.text
        assert refused.headers["X-IRA-Trust-Ledger"] == "unavailable"
        assert "Trust Ledger could not record this" in refused.json()["detail"]
        assert await _memory_left(world), "the memory was forgotten with no ledger record"
        assert await _rows(world.tenant) == []
        assert not J.journal_path().exists(), "a Class A action wrote the fallback journal"

        done = await client.delete(f"/api/v1/memory/{world.memory}",
                                   params={"confirm_token": token}, headers=headers)
    assert done.status_code == 200, done.text
    assert not await _memory_left(world)
    rows = await _rows(world.tenant)
    assert [r["kind"] for r in rows] == ["action", "approval", "execution"]
    assert verify_chain(rows) == 3
    assert CANARY not in json.dumps(rows, default=str)


# ── Class B, end to end: journal, then replay into the real chain ───────────


def _admin_parts() -> dict:
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
    spec = importlib.util.spec_from_file_location(
        "worker._backup_real_ledger_fc", _IRA / "worker" / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def probe_schema():
    name = f"ledger_fc_restore_{uuid.uuid4().hex[:10]}"
    yield name
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await admin.close()


@pytest.fixture
def owner_restore_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    import api.routes.backup as route
    from api.middleware.auth import _admin_password_hash, require_auth
    from config import get_settings

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
        return None

    monkeypatch.setattr(route, "run_database_backup", no_snapshot)
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: get_settings().ira_admin_username
    yield SimpleNamespace(
        client=lambda: AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://ledger-fc.test"),
        password=get_settings().ira_admin_password)
    _admin_password_hash.cache_clear()


async def test_class_b_restore_runs_on_the_journal_and_replays_into_the_chain_once(
        probe_schema, owner_restore_client):
    private = system_principal(tenant_id=PRIVATE_TENANT_ID, reason="ledger-fc-test")
    head = await export_head(principal=private)
    assert head is not None

    archive = gzip.compress(
        f'CREATE SCHEMA "{probe_schema}";\n'
        f'CREATE TABLE "{probe_schema}".restored (note text);\n'.encode())
    upload = {"files": {"file": ("backup.sql.gz", archive, "application/gzip")}}
    async with owner_restore_client.client() as client:
        staged = await client.post("/api/v1/backup/restore", **upload)
        token = staged.json()["token"]
        async with ledger_down():
            restored = await client.post(
                "/api/v1/backup/restore", **upload,
                data={"confirm_token": token, "owner_password": owner_restore_client.password})
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "restored"
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        assert await admin.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1",
            probe_schema) == 1, "the restore did not run"
    finally:
        await admin.close()

    window = [r for r in await recent_records(limit=250, principal=private)
              if r["seq"] > head["seq"]]
    assert not [r for r in window if r["action_id"].startswith("backup_restore-")], (
        "the ledger took a record while INSERT was revoked")
    state = J.open_journal().read_state()
    assert state.chain_ok and [e.body["kind"] for e in state.pending] == \
        ["action", "approval", "execution"]
    assert {e.body["tenant_id"] for e in state.pending} == {str(PRIVATE_TENANT_ID)}

    # The ledger is back. The next real write to it starts the replay.
    await append_record("verification", user_id="ledger-fc-test",
                        action_id=f"ledger-fc-trigger-{uuid.uuid4().hex[:8]}",
                        record={"probe": True}, principal=private)
    task = J._STATE.replay_task
    assert task is not None, "a real ledger write did not start the replay"
    result = await task
    assert result["status"] == "ok" and result["replayed"] == 3, result

    window = sorted((r for r in await recent_records(limit=250, principal=private)
                     if r["seq"] > head["seq"]), key=lambda r: r["seq"])
    mine = [r for r in window if r["action_id"].startswith("backup_restore-")]
    assert [r["kind"] for r in mine] == ["action", "approval", "execution"]
    assert [r["record"]["journal_entry_id"] for r in mine] == [e.entry_id for e in state.pending]
    assert all(r["record"]["journalled_at"] for r in mine)
    assert window[0]["prev_hash"] == head["hash"], "the replay forked the chain"
    verify_chain(window, allow_partial=True)

    # Exactly once: a second replay adds nothing, and the ledger itself
    # refuses a second row for an entry id it holds.
    again = await J.replay_pending()
    assert again["replayed"] == 0 and again["pending"] == 0
    first = state.pending[0]
    duplicate = await append_record(
        "action", user_id=first.body["user_id"], action_id=first.body["action_id"],
        record=first.body["record"], principal=private, journal_entry_id=first.entry_id)
    assert duplicate.get("duplicate") is True and duplicate["seq"] == mine[0]["seq"]
    after = [r for r in await recent_records(limit=250, principal=private)
             if r["action_id"] == first.body["action_id"]]
    assert len(after) == 3, "a replayed record landed twice"
    text = json.dumps(mine, default=str)
    assert token not in text and owner_restore_client.password not in text


# ── A tampered journal is not replayed, and the ledger records why ──────────


async def test_a_tampered_journal_is_refused_and_recorded_as_a_security_event():
    private = system_principal(tenant_id=PRIVATE_TENANT_ID, reason="ledger-fc-test")
    head = await export_head(principal=private)
    journal = J.open_journal()
    marker = uuid.uuid4().hex[:8]
    for kind in ("action", "approval", "execution"):
        journal.append(J.ENTRY_RECORD, {
            "kind": kind, "user_id": "admin", "action_id": f"revoke_access-{marker}",
            "task_id": None, "record": {"note": "planted for the tamper test"},
            "tenant_id": str(PRIVATE_TENANT_ID), "reason": "ledger_unavailable"})
    lines = J.journal_path().read_bytes().split(b"\n")[:-1]
    lines[0], lines[1] = lines[1], lines[0]
    J.journal_path().write_bytes(b"".join(line + b"\n" for line in lines))

    result = await J.replay_pending()
    assert result["status"] == "refused"
    window = sorted((r for r in await recent_records(limit=250, principal=private)
                     if r["seq"] > head["seq"]), key=lambda r: r["seq"])
    assert not [r for r in window if r["action_id"] == f"revoke_access-{marker}"], (
        "a tampered journal was replayed into the ledger")
    events = [r for r in window if r["kind"] == "security_event"
              and r["record"].get("event") == "ledger_journal_verification_failed"]
    assert len(events) == 1 and events[0]["user_id"] == J.SYSTEM_USER
    verify_chain(window, allow_partial=True)
