"""One person's memory actions reach that person's memories, and nobody else's.

The workspace policy on `memory_embeddings` has no user clause. Every Privacy
Centre memory action narrowed by tenant alone, so for one ordinary member it:

  * listed everyone's memories in the workspace;
  * let them edit, approve, reject and delete anyone's memory by id;
  * exported everyone's memories;
  * erased everyone's memories on "delete my memories": a colleague's, the
    Memory Vault, the Technical Library and self-knowledge corpora (which
    nothing rebuilds), meeting transcripts and `system` rows.

And the check that follows an erasure counted on a tenant-only connection,
where the workspace predicate matches nothing, so it always reported success.

The owner decided on 2026-10-20
(`docs/security/MEMORY_ACTIONS_ARE_PERSONAL_2026-10-20.md`) that these actions
reach only the person's own memories, plus the Memory Vault for the owner of a
private install (`core.privacy.retention.personal_memory_namespaces`).

Real PostgreSQL, real RLS, as `ira_app`. Rows are seeded through the admin DSN,
so no embedding model is involved.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

# `core.privacy.retention` reaches `memory.store`, which imports
# `memory.embeddings`, which imports `SentenceTransformer` by name. conftest
# stubs the package without that attribute; nothing here embeds anything.
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

from memory.namespaces import (  # noqa: E402
    MEETING_NAMESPACE,
    SELF_KNOWLEDGE_NAMESPACE,
    SYSTEM_NAMESPACE,
    TECH_LIBRARY_NAMESPACE,
    VAULT_NAMESPACE,
)

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"personal-{TENANT.hex[:8]}"

ALICE = uuid.uuid4()
BOB = uuid.uuid4()
#: Private-edition style accounts: the name is kept in `external_subject`.
ALICE_NAME = f"alice-{SLUG}"
BOB_NAME = f"bob-{SLUG}"

#: (label, user_id, source_type). The label is the row's content.
ROWS = [
    ("alice chat", ALICE_NAME, "message"),
    ("alice chat, filed under her id", str(ALICE), "message"),
    ("alice note", ALICE_NAME, "note"),
    ("bob chat", BOB_NAME, "message"),
    ("vault entry", VAULT_NAMESPACE, "projects"),
    ("technical library page", TECH_LIBRARY_NAMESPACE, "document"),
    ("self-knowledge page", SELF_KNOWLEDGE_NAMESPACE, "document"),
    ("meeting transcript", MEETING_NAMESPACE, "meeting"),
    ("system row", SYSTEM_NAMESPACE, "fact"),
]
EVERYTHING = {label for label, _, _ in ROWS}
ALICE_OWN = {"alice chat", "alice chat, filed under her id", "alice note"}
#: What the owner of a private install may reach: their own memories and the vault.
OWNER_OWN = ALICE_OWN | {"vault entry"}


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """One tenant and workspace shared by Alice and Bob, one memory per row in
    `ROWS`, each with an embedding. Yields the admin connection and the ids."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Personal memory actions {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for user, name in ((ALICE, ALICE_NAME), (BOB, BOB_NAME)):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) "
                "VALUES ($1,NULL,'R','password',$2,'active')", user, name)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, user)
        ids = {}
        for label, user_id, source_type in ROWS:
            ids[label] = await admin.fetchval(
                "INSERT INTO memory_embeddings "
                "  (tenant_id,workspace_id,user_id,source_type,content,embedding) "
                "VALUES ($1,$2,$3,$4,$5, array_fill(0.01::real, ARRAY[1024])::vector) "
                "RETURNING id",
                TENANT, WORKSPACE, user_id, source_type, label)
        yield admin, ids
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for user in (ALICE, BOB):
        await admin.execute("DELETE FROM users WHERE id = $1", user)


def _member(user=ALICE, name=ALICE_NAME):
    """A cloud member, as the workspace resolver builds one."""
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=name, is_owner=False)


def _private_owner():
    """The owner of a private install, as `_resolve_private_principal` builds one."""
    from core.identity.principal import Principal

    return Principal(user_id=ALICE, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="private",
                     roles=frozenset({"primary_owner"}),
                     username=ALICE_NAME, is_owner=True)


async def _live(admin) -> set[str]:
    rows = await admin.fetch(
        "SELECT content FROM memory_embeddings "
        " WHERE tenant_id = $1 AND deleted_at IS NULL", TENANT)
    return {r["content"] for r in rows}


async def _content(admin, memory_id) -> str:
    return await admin.fetchval(
        "SELECT content FROM memory_embeddings WHERE id = $1", memory_id)


# ── Reading ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, expected", [
    ("member", ALICE_OWN),
    ("private owner", OWNER_OWN),
], ids=["member", "private-owner"])
async def test_the_list_holds_only_the_persons_own_memories(seeded, who, expected):
    from core.privacy.store import list_memories

    principal = _member() if who == "member" else _private_owner()
    listed = {m.content for m in await list_memories(principal)}

    assert listed == expected


async def test_pending_review_holds_only_the_persons_own_memories(seeded):
    from core.privacy.store import pending_memories

    admin, _ = seeded
    await admin.execute(
        "UPDATE memory_embeddings SET review_state = 'pending' WHERE tenant_id = $1",
        TENANT)

    assert {m.content for m in await pending_memories(_member())} == ALICE_OWN


# ── Changing one memory by id ────────────────────────────────────────────────


async def test_deleting_someone_elses_memory_by_id_is_refused(seeded):
    from core.privacy.store import delete_memory

    admin, ids = seeded
    alice = _member()

    assert not await delete_memory(alice, ids["bob chat"])
    assert not await delete_memory(alice, ids["technical library page"])
    assert not await delete_memory(alice, ids["vault entry"])
    assert await delete_memory(alice, ids["alice note"]), "her own memory must still delete"
    assert await _live(admin) == EVERYTHING - {"alice note"}


async def test_the_private_owner_may_delete_a_vault_entry_by_id(seeded):
    from core.privacy.store import delete_memory

    admin, ids = seeded

    assert await delete_memory(_private_owner(), ids["vault entry"])
    assert not await delete_memory(_private_owner(), ids["bob chat"])
    assert await _live(admin) == EVERYTHING - {"vault entry"}


async def test_editing_someone_elses_memory_is_refused(seeded):
    from core.privacy.store import update_memory

    admin, ids = seeded
    alice = _member()

    assert not await update_memory(alice, ids["bob chat"], content="rewritten by alice")
    assert await _content(admin, ids["bob chat"]) == "bob chat"
    assert await update_memory(alice, ids["alice note"], content="alice note, edited")
    assert await _content(admin, ids["alice note"]) == "alice note, edited"


async def test_reviewing_someone_elses_memory_is_refused(seeded):
    from core.privacy.store import set_review_state

    admin, ids = seeded
    alice = _member()

    assert not await set_review_state(alice, ids["bob chat"], "rejected")
    assert not await set_review_state(alice, ids["meeting transcript"], "accepted")
    assert "bob chat" in await _live(admin), "rejecting marked a colleague's memory deleted"
    assert await set_review_state(alice, ids["alice chat"], "accepted")


# ── Export ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["memories", "everything"])
@pytest.mark.parametrize("who, expected", [
    ("member", ALICE_OWN),
    ("private owner", OWNER_OWN),
], ids=["member", "private-owner"])
async def test_an_export_holds_only_the_persons_own_memories(seeded, scope, who, expected):
    from core.privacy.export import export_subject_data

    principal = _member() if who == "member" else _private_owner()
    result = await export_subject_data(principal, scope=scope)

    assert result.ok, result.error
    exported = {row["content"] for t in result.tables
                if t.table == "memory_embeddings" for row in t.rows}
    assert exported == expected


# ── Erasure ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who, erased", [
    ("member", ALICE_OWN),
    ("private owner", OWNER_OWN),
], ids=["member", "private-owner"])
async def test_deleting_my_memories_removes_mine_and_nothing_shared(seeded, who, erased):
    from core.privacy.erasure import erase_postgres

    admin, _ = seeded
    principal = _member() if who == "member" else _private_owner()
    result = await erase_postgres(principal, scope="memories")

    assert result.ok, result.error
    assert await _live(admin) == EVERYTHING - erased
    assert {"bob chat", "technical library page", "self-knowledge page",
            "meeting transcript", "system row"} <= await _live(admin), (
        "one person's erasure reached a colleague or a shared corpus")


async def test_deleting_everything_spares_shared_memories_too(seeded):
    from core.privacy.erasure import erase_postgres

    admin, _ = seeded
    result = await erase_postgres(_member(), scope="everything")

    assert result.ok, result.error
    assert await _live(admin) == EVERYTHING - ALICE_OWN


async def test_the_erasure_check_reports_memories_left_behind(seeded):
    """The `pgvector` line must count this person's memories per workspace. It
    counted on a tenant-only connection, matched nothing, and always passed."""
    from core.privacy.erasure import erase_postgres, erase_vectors

    alice = _member()
    before = await erase_vectors(alice, scope="memories")
    assert not before.ok and before.unerasable == len(ALICE_OWN), before

    await erase_postgres(alice, scope="memories")
    after = await erase_vectors(alice, scope="memories")
    assert after.ok, after.error
