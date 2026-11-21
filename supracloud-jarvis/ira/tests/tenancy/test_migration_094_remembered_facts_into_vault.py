"""094 moves the owner's old "remember" facts into the Memory Vault, and only those.

Before #60 the owner's "remember X" filed X under the chat username, where the
Memory Vault could neither list nor forget it. The owner decided on 2026-10-19
to move those facts into the vault. What matters here is what does NOT move:

  * chat transcript rows, and facts from any source but the owner's chat;
  * anything already in the vault;
  * a cloud tenant's facts: the cloud vault is shared by the workspace, so
    moving a private fact there would widen who can read it.

And that a moved fact can be put back exactly: the rollback returns every moved
row to the name it came from, with its metadata as it was.

── How these run without damaging anything ─────────────────────────────────

As in `test_migration_048_workspace_backfill.py`: the real migration and
rollback files run inside a transaction that is always rolled back, with their
own BEGIN/COMMIT stripped so an inner COMMIT cannot commit the outer
transaction. `test_both_files_are_wrapped_in_a_transaction` asserts the pair is
in each file, so stripping them here cannot hide their absence there.

Both files run as `ira_schema_owner`, the role `scripts/migrate.py` uses. A
superuser bypasses RLS and would pass a migration that moves nothing in
production, where FORCE RLS hides every row from the owner unless 094 lifts it.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "094_remembered_facts_into_vault.sql"
ROLLBACK = POSTGRES / "rollback" / "094_remembered_facts_into_vault_down.sql"

#: The owner's chat username in these fixtures.
OWNER_CHAT_NAME = "admin"


def _body(path: pathlib.Path) -> str:
    """The script with its outermost BEGIN/COMMIT removed (see the module doc)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    text = re.sub(r"(?im)^COMMIT;\s*$", "", text)
    return text


async def _run_as_schema_owner(conn, path: pathlib.Path) -> None:
    await conn.execute("SET LOCAL ROLE ira_schema_owner")
    try:
        await conn.execute(_body(path))
    finally:
        await conn.execute("RESET ROLE")


@pytest.fixture
async def db():
    """A connection inside a transaction that is always rolled back."""
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def world(db):
    """Facts in a private-edition tenant and in a cloud tenant, inside the
    transaction.

    Both tenants are created here rather than found: the shared database holds
    whatever private-edition tenants other suites left, some with no workspace.
    Every id is fresh, so the assertions read only these rows; whatever else
    the shared database holds is moved or not by the same rule and rolled back.
    """
    async def tenant(edition: str) -> tuple[uuid.UUID, uuid.UUID]:
        tid, wid = uuid.uuid4(), uuid.uuid4()
        slug = f"m094-{edition}-{tid.hex[:8]}"
        await db.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,$4)",
            tid, f"094 {edition}", slug, edition)
        org = await db.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            tid, slug)
        await db.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", wid, tid, org, slug)
        return tid, wid

    private, private_ws = await tenant("private")
    cloud, cloud_ws = await tenant("cloud")
    await db.execute("SET CONSTRAINTS ALL IMMEDIATE")

    rows: dict[str, uuid.UUID] = {}

    async def add(label, *, tenant, ws, user_id, source_type="fact", source="owner_chat",
                  memory_type="fact", metadata=None, superseded_by=None):
        rows[label] = uuid.uuid4()
        await db.execute(
            "INSERT INTO memory_embeddings (id,tenant_id,workspace_id,user_id,source_type,"
            "source,memory_type,content,metadata,superseded_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)",
            rows[label], tenant, ws, user_id, source_type, source, memory_type, label,
            json.dumps(metadata or {}), superseded_by)

    p = {"tenant": private, "ws": private_ws}
    await add("remembered", **p, user_id=OWNER_CHAT_NAME,
              metadata={"pinned": False, "note": "kept"})
    await add("replacement", **p, user_id=OWNER_CHAT_NAME)
    await add("replaced", **p, user_id=OWNER_CHAT_NAME, memory_type="superseded_fact",
              superseded_by=rows["replacement"])
    await add("chat transcript", **p, user_id=OWNER_CHAT_NAME, source_type="message",
              source=None)
    await add("fact from elsewhere", **p, user_id=OWNER_CHAT_NAME, source="document")
    await add("already in the vault", **p, user_id=VAULT_NAMESPACE)
    await add("vault note", **p, user_id=VAULT_NAMESPACE, source_type="note", source=None)
    await add("cloud fact", tenant=cloud, ws=cloud_ws, user_id="alice@m094.test")
    return rows


async def _state(db, rows) -> dict[str, tuple[str, dict]]:
    found = await db.fetch(
        "SELECT id, user_id, metadata FROM memory_embeddings WHERE id = ANY($1::uuid[])",
        list(rows.values()))
    by_id = {r["id"]: (r["user_id"], json.loads(r["metadata"] or "{}")) for r in found}
    return {label: by_id[i] for label, i in rows.items()}


MOVED = {"remembered", "replacement", "replaced"}


async def test_the_owners_remembered_facts_move_into_the_vault(db, world):
    await _run_as_schema_owner(db, MIGRATION)
    state = await _state(db, world)

    for label in MOVED:
        user_id, meta = state[label]
        assert user_id == VAULT_NAMESPACE, f"{label!r} was not moved into the vault"
        assert meta["moved_to_vault"] == {"from": OWNER_CHAT_NAME, "by": "094"}, label
    assert state["remembered"][1]["note"] == "kept", "the move dropped existing metadata"


async def test_nothing_else_moves(db, world):
    before = await _state(db, world)
    await _run_as_schema_owner(db, MIGRATION)
    after = await _state(db, world)

    for label in set(world) - MOVED:
        assert after[label] == before[label], f"094 changed {label!r}"


async def test_a_cloud_tenants_facts_stay_out_of_its_shared_vault(db, world):
    await _run_as_schema_owner(db, MIGRATION)

    assert (await _state(db, world))["cloud fact"][0] == "alice@m094.test", (
        "a cloud fact was moved into a vault every workspace member can read")


async def test_a_replacement_chain_moves_whole(db, world):
    await _run_as_schema_owner(db, MIGRATION)
    link = await db.fetchrow(
        "SELECT o.user_id AS old_ns, n.user_id AS new_ns FROM memory_embeddings o "
        "JOIN memory_embeddings n ON n.id = o.superseded_by WHERE o.id = $1",
        world["replaced"])

    assert link["old_ns"] == link["new_ns"] == VAULT_NAMESPACE


async def test_running_it_twice_changes_nothing_more(db, world):
    await _run_as_schema_owner(db, MIGRATION)
    once = await _state(db, world)
    await _run_as_schema_owner(db, MIGRATION)

    assert await _state(db, world) == once


async def test_the_rollback_puts_every_moved_fact_back_exactly(db, world):
    before = await _state(db, world)
    await _run_as_schema_owner(db, MIGRATION)
    await _run_as_schema_owner(db, ROLLBACK)

    assert await _state(db, world) == before


async def test_force_rls_is_restored(db, world):
    await _run_as_schema_owner(db, MIGRATION)
    await _run_as_schema_owner(db, ROLLBACK)

    assert await db.fetchval(
        "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'memory_embeddings'")


def test_both_files_are_wrapped_in_a_transaction():
    for path in (MIGRATION, ROLLBACK):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"(?im)^BEGIN;\s*$", text) and re.search(r"(?im)^COMMIT;\s*$", text), (
            f"{path.name} is not wrapped in BEGIN/COMMIT")
        code = re.sub(r"--[^\n]*", "", text)
        assert code.index("NO FORCE ROW LEVEL SECURITY") < code.rindex(
            "FORCE ROW LEVEL SECURITY;"), f"{path.name} does not restore FORCE RLS"
