"""Actions confirmed through `owner_gated_action` reach the Trust Ledger: real PostgreSQL.

── The defect (P2, found 1 November 2026 by a clean install of main @ c1d47fbf) ──

`utils/approval.py::owner_gated_action` is the two-step confirmation behind
memory forget, backup restore, file delete, people forget, access grant, role
change and revoke, the Command Center, Cal.com bookings, Android taps and the
mobile task queue. It never wrote to the Trust Ledger. On a real install the
owner forgot a memory through its confirmation dialog: the API log shows the two
DELETEs and no `ledger append`, and the Trust Ledger panel, which says it shows
"every important action", listed nothing.

Actions through `actions/gated_execution.confirm_action` (email send, note
delete) did write `action`, `approval` and `execution` records. This file proves
both halves on the same database: the gated_execution control records, and an
owner-gated forget now records the same three kinds, on the same chain, and the
chain verifies.

── What is real ────────────────────────────────────────────────────────────

PostgreSQL as `ira_app`, under FORCE RLS. The real memory router over ASGI, with
the real `require_workspace_principal` (only `require_auth` and the username →
principal lookup are replaced, as in `test_replaced_and_deleted_memories.py`),
so the request principal the ledger writes under is bound the way production
binds it. The real backup router with the real `psql` restore. The ledger is
read back through `trust.ledger.records_for` / `recent_records`, the functions
`GET /api/v1/trust/ledger` uses, and checked with `verify_chain`.

── Fixture residue ─────────────────────────────────────────────────────────

Memberships and users are removed in teardown (`test_zz_no_fixture_residue.py`
fails the directory otherwise). Tenants that now hold ledger rows stay: the
ledger is append-only and `trust_ledger.tenant_id` is `ON DELETE RESTRICT`.

── Negative controls (planted and watched fail; see the evidence README) ────

* `owner_gated_action` without the ledger writes (main @ 5b7f4283) → every
  "is recorded" test here fails with no rows; the gated_execution control passes.
* The memory route without `ledger_preview` → the forget test fails: the
  forgotten memory's text is in the ledger.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import pathlib
import sys
import time
import uuid
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
from trust.ledger import export_head, recent_records, records_for, verify_chain  # noqa: E402

# Not a lockdown suite: the forget and restore routes read the lockdown state
# first, and what the lockdown suites left in Redis must not decide this one.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")

OWNER_USERNAME = "admin"
#: Never a real secret: its presence anywhere in a ledger record is the defect.
CANARY = "LEDGER-CANARY-MARKER-4c1e"

_IRA = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
async def app_pool():
    """The ledger writes through the application pool, as in production."""
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
    """No principal carried in from elsewhere; none carried out."""
    from core.runtime import request_context

    request_context.clear()
    yield
    request_context.clear()


# ── A tenant with an owner and one memory ─────────────────────────────────────


@pytest.fixture
async def world(monkeypatch):
    """One private-edition tenant whose owner is workspace_admin of its default
    workspace, with two vault memories. One carries `CANARY`, so a ledger record
    that quotes the forgotten memory is visible."""
    w = SimpleNamespace()
    w.tenant, w.owner = uuid.uuid4(), uuid.uuid4()
    tag = w.tenant.hex[:8]
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("INSERT INTO tenants (id, name) VALUES ($1, $2)",
                            w.tenant, f"ledger-og-{tag}")
        await admin.execute("INSERT INTO users (id, email) VALUES ($1, $2)",
                            w.owner, f"{w.owner.hex[:12]}@example.test")
        w.ws = await admin.fetchval("SELECT provision_tenant_substrate($1)", w.tenant)
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'member','active')", w.tenant, w.owner)
        await admin.execute(
            "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", w.tenant, w.ws, w.owner)
        w.memory = {}
        for label, content in (("secret", f"My locker code is {CANARY} ({tag})"),
                               ("other", f"The synthetic venue is Leeds ({tag})")):
            w.memory[label] = await admin.fetchval(
                "INSERT INTO memory_embeddings (tenant_id, workspace_id, user_id, "
                " source_type, source, memory_type, content) "
                "VALUES ($1,$2,$3,'fact','owner_chat','fact',$4) RETURNING id",
                w.tenant, w.ws, VAULT_NAMESPACE, content)
        w.unscoped = Principal(
            user_id=w.owner, tenant_id=w.tenant, session_id=uuid.uuid4(),
            edition=EDITION_PRIVATE, roles=frozenset({"primary_owner"}),
            username=OWNER_USERNAME, is_owner=True)

        import api.middleware.workspace as ws_module

        async def fake_resolve(username, **kw):
            return w.unscoped

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


def _app(*routers):
    """The routers under test, with the REAL workspace dependency.

    Only `require_auth` is replaced. `require_workspace_principal` runs for
    real: it verifies the workspace membership and binds the request principal
    (`set_principal`), which is what the ledger append reads to know whose
    chain to write. A test that overrode it would bind nothing, and the records
    would land in the private-edition tenant instead.
    """
    import httpx
    from fastapi import FastAPI

    from api.middleware.auth import require_auth

    app = FastAPI()
    for router in routers:
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: OWNER_USERNAME
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://ledger-og.test")


def _vault_client():
    from api.routes.memory import router

    return _app(router)


def _headers(w) -> dict:
    return {"X-Workspace-ID": str(w.ws)}


async def _tenant_rows(tenant) -> list[dict]:
    """Every ledger row this tenant can see, oldest first, as the ledger API
    returns them. The tenant is fresh, so these are this test's rows only."""
    rows = await recent_records(limit=250, principal=system_principal(
        tenant_id=tenant, reason="ledger-og-test"))
    return sorted(rows, key=lambda r: r["seq"])


def _text(rows) -> str:
    return json.dumps(rows, default=str)


async def _memory_left(w, label) -> bool:
    return bool(await w.admin.fetchval(
        "SELECT count(*) FROM memory_embeddings WHERE id = $1", w.memory[label]))


# ── The control: gated_execution records, on this database ────────────────────


async def test_control_a_gated_execution_action_is_recorded_on_this_database(world):
    """The precedent, on the same tenant and database: `confirm_action` writes
    action, approval and execution. If this failed, an empty ledger below would
    say nothing about `owner_gated_action`."""
    from actions.gated_execution import confirm_action, stage_action
    from actions.shadow_world import ActionSpec
    from core.runtime.request_context import set_principal
    from utils.approval import ApprovalGuardrail

    set_principal(system_principal(tenant_id=world.tenant, reason="ledger-og-control"))
    spec = ActionSpec(kind="file_change", risk="low", requested_by=OWNER_USERNAME,
                      intended_action="Delete note 'control' (cannot be undone)",
                      action_id=f"note-delete-control-{world.tenant.hex[:8]}")
    async def dry_run(_spec):   # Shadow World has no file_change simulator
        return {"ok": True, "detail": "control: note exists"}

    guard = ApprovalGuardrail()
    staged = await stage_action(spec, is_owner=True, execute=lambda: {"status": "deleted"},
                                simulator=dry_run, guard=guard)
    assert staged["status"] == "confirmation_required", staged
    done = await confirm_action(spec, token=staged["token"], simulator=dry_run, guard=guard)
    assert done["status"] == "executed", done
    assert done["ledger_recorded"] is True

    rows = await _tenant_rows(world.tenant)
    assert [r["kind"] for r in rows] == ["action", "approval", "execution"]
    assert verify_chain(rows) == 3


# ── Memory forget, the investor-demo path ─────────────────────────────────────


async def test_a_confirmed_memory_forget_is_recorded_and_the_chain_verifies(world):
    memory_id = world.memory["secret"]
    async with _vault_client() as client:
        draft = await client.delete(f"/api/v1/memory/{memory_id}", headers=_headers(world))
        assert draft.status_code == 200, draft.text
        token = draft.json()["token"]
        assert await _tenant_rows(world.tenant) == [], (
            "the draft wrote to the ledger: gated_execution records nothing at "
            "stage, and a draft is not an action")

        done = await client.delete(f"/api/v1/memory/{memory_id}",
                                   params={"confirm_token": token}, headers=_headers(world))
    assert done.status_code == 200, done.text
    assert not await _memory_left(world, "secret"), "the confirmed forget left the memory"

    rows = await _tenant_rows(world.tenant)
    assert [r["kind"] for r in rows] == ["action", "approval", "execution"], (
        "a memory forgotten through its confirmation left no Trust Ledger record: "
        f"{[(r['kind'], r['action_id']) for r in rows]}")
    action_ids = {r["action_id"] for r in rows}
    assert len(action_ids) == 1, action_ids
    (action_id,) = action_ids
    assert action_id.startswith("forget_memory-"), action_id
    assert {r["user_id"] for r in rows} == {OWNER_USERNAME}

    intent = rows[0]["record"]
    assert intent["kind"] == "forget_memory"
    assert str(memory_id) in intent["intended_action"], intent
    assert rows[1]["record"] == {"approval_level": "plan_approval",
                                 "approved_by": "plan_approval"}
    assert rows[2]["record"]["result"]["shape"] == "object with 2 field(s)"

    # The chain this tenant holds starts at GENESIS and verifies end to end,
    # and the API's per-action reader returns the same three records.
    assert verify_chain(rows) == 3
    by_action = await records_for(action_id, principal=system_principal(
        tenant_id=world.tenant, reason="ledger-og-test"))
    assert [r["seq"] for r in by_action] == [r["seq"] for r in rows]

    # Forgetting must not leave a copy in an append-only log, and the one-shot
    # token is an approval credential, not a record.
    text = _text(rows)
    assert CANARY not in text, "the forgotten memory's text is in the Trust Ledger"
    assert token not in text, "the confirmation token is in the Trust Ledger"


async def test_a_refused_token_records_nothing_and_forgets_nothing(world):
    """A wrong token, and a token drafted for another memory: both refused, and
    neither reaches the ledger."""
    target, other = world.memory["secret"], world.memory["other"]
    async with _vault_client() as client:
        await client.delete(f"/api/v1/memory/{target}", headers=_headers(world))
        wrong = await client.delete(f"/api/v1/memory/{target}",
                                    params={"confirm_token": "not-the-token"},
                                    headers=_headers(world))
        assert wrong.status_code == 409, wrong.text

        other_draft = await client.delete(f"/api/v1/memory/{other}", headers=_headers(world))
        crossed = await client.delete(f"/api/v1/memory/{target}",
                                      params={"confirm_token": other_draft.json()["token"]},
                                      headers=_headers(world))
        assert crossed.status_code == 409, crossed.text

    assert await _memory_left(world, "secret") and await _memory_left(world, "other")
    assert await _tenant_rows(world.tenant) == [], (
        "a refused confirmation wrote to the Trust Ledger")


async def test_an_expired_token_records_nothing_and_forgets_nothing(world, monkeypatch):
    from utils import approval

    memory_id = world.memory["secret"]
    async with _vault_client() as client:
        draft = await client.delete(f"/api/v1/memory/{memory_id}", headers=_headers(world))
        token = draft.json()["token"]
        later = time.monotonic() + approval.guardrail.ttl_seconds + 60
        monkeypatch.setattr(approval.guardrail, "_now", lambda: later)
        expired = await client.delete(f"/api/v1/memory/{memory_id}",
                                      params={"confirm_token": token}, headers=_headers(world))
    assert expired.status_code == 409, expired.text
    assert await _memory_left(world, "secret")
    assert await _tenant_rows(world.tenant) == [], "an expired confirmation wrote to the ledger"


# ── The ratchet: a route nobody has written yet ───────────────────────────────


async def test_a_new_route_that_drafts_through_the_chokepoint_is_recorded(world):
    """A route written after this fix, with nothing about the ledger in it.

    It is recorded because `owner_gated_action` records, not because its author
    remembered to. Its executor returns a credential-shaped value, which must
    reach the caller and must not reach the ledger.
    """
    from fastapi import APIRouter, Depends

    from api.middleware.workspace import require_workspace_principal
    from utils.approval import owner_gated_action

    router = APIRouter(prefix="/ratchet")

    @router.delete("/{thing}")
    async def remove_thing(thing: str, confirm_token: str | None = None,
                           principal=Depends(require_workspace_principal)):
        async def _do():
            return {"removed": thing, "api_key": CANARY}

        outcome = await owner_gated_action(
            owner_username=principal.username, is_owner=True,
            action="ratchet_remove_thing", preview=f"Remove thing {thing}",
            execute=_do, confirm_token=confirm_token)
        return outcome.get("result", outcome)

    async with _app(router) as client:
        draft = await client.delete("/api/v1/ratchet/t-1", headers=_headers(world))
        done = await client.delete("/api/v1/ratchet/t-1",
                                   params={"confirm_token": draft.json()["token"]},
                                   headers=_headers(world))
    assert done.json() == {"removed": "t-1", "api_key": CANARY}, done.text

    rows = await _tenant_rows(world.tenant)
    assert [r["kind"] for r in rows] == ["action", "approval", "execution"], rows
    assert rows[0]["record"]["kind"] == "ratchet_remove_thing"
    assert rows[0]["record"]["intended_action"] == "Remove thing t-1"
    assert verify_chain(rows) == 3
    assert CANARY not in _text(rows), "an executor's credential reached the ledger"


# ── Backup restore, with psql ─────────────────────────────────────────────────


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
    """`tests/conftest.py` stubs `worker.backup`; load the real file."""
    spec = importlib.util.spec_from_file_location(
        "worker._backup_real_ledger_og", _IRA / "worker" / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def probe_schema():
    name = f"ledger_og_restore_{uuid.uuid4().hex[:10]}"
    yield name
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await admin.close()


@pytest.fixture
def owner_restore_client(tmp_path, monkeypatch):
    """The backup router in the private edition, as the owner, with the real
    `restore_from_file` pointed at the test database (as in
    `test_destructive_action_approvals_pg.py`)."""
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
                                   base_url="http://ledger-og.test"),
        password=get_settings().ira_admin_password)
    _admin_password_hash.cache_clear()


async def test_a_confirmed_restore_is_recorded_on_the_private_chain(
        probe_schema, owner_restore_client):
    """Restore runs with no workspace principal (`require_auth` only), so the
    private edition's one tenant is whose chain it extends."""
    private = system_principal(tenant_id=PRIVATE_TENANT_ID, reason="ledger-og-test")
    head = await export_head(principal=private)
    assert head is not None, "the private tenant's ledger head could not be read"

    archive = gzip.compress(
        f'CREATE SCHEMA "{probe_schema}";\n'
        f'CREATE TABLE "{probe_schema}".restored (note text);\n'.encode())
    upload = {"files": {"file": ("backup.sql.gz", archive, "application/gzip")}}
    async with owner_restore_client.client() as client:
        staged = await client.post("/api/v1/backup/restore", **upload)
        assert staged.status_code == 200, staged.text
        token, sha256 = staged.json()["token"], staged.json()["sha256"]
        restored = await client.post(
            "/api/v1/backup/restore", **upload,
            data={"confirm_token": token, "owner_password": owner_restore_client.password})
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "restored"

    window = sorted((r for r in await recent_records(limit=250, principal=private)
                     if r["seq"] > head["seq"]), key=lambda r: r["seq"])
    mine = [r for r in window if r["action_id"].startswith("backup_restore-")]
    assert [r["kind"] for r in mine] == ["action", "approval", "execution"], (
        "a confirmed restore left no Trust Ledger record: "
        f"{[(r['kind'], r['action_id']) for r in window]}")
    assert sha256 in mine[0]["record"]["intended_action"]
    assert mine[2]["record"]["result"]["status"] == "restored"

    # The new records extend the chain the private tenant already had.
    assert window[0]["prev_hash"] == head["hash"], "the restore's records forked the chain"
    verify_chain(window, allow_partial=True)
    text = _text(window)
    assert token not in text, "the confirmation token is in the Trust Ledger"
    assert owner_restore_client.password not in text, "the owner password is in the Trust Ledger"
