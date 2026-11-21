"""Deleting your conversations also deletes what chat remembered from them.

`memory.store.save_message` embeds every chat message into `memory_embeddings`
with `source_type = 'message'`, and chat recall searches those rows. The
`conversations` erasure scope deleted `messages` and `conversations` only, so
after "delete my conversations" IRA could still recall what was said in them
(`docs/security/FORGET_LEAVES_TRANSCRIPT_COPIES_2026-10-19.md`). The owner
decided on 2026-10-19 that deleting conversations removes those memories too,
and that nothing else changes:

  * only the person's own chat memories go, under any name they are filed
    under (`core.privacy.retention._own_memory_namespaces`);
  * the Memory Vault stays: forgetting a saved memory is the vault's action;
  * notes the person saved (any `source_type` other than `message`) stay;
  * a colleague's chat memories in the same workspace stay.

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

from memory.namespaces import VAULT_NAMESPACE  # noqa: E402

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"chatmem-{TENANT.hex[:8]}"

ALICE = uuid.uuid4()
BOB = uuid.uuid4()
ALICE_EMAIL = f"alice-{SLUG}@chatmem.test"
BOB_EMAIL = f"bob-{SLUG}@chatmem.test"
#: A private-edition account: no email, its name kept in `external_subject`.
CAROL = uuid.uuid4()
CAROL_NAME = f"carol-{SLUG}"

#: (label, user_id, source_type). The label is the row's content.
ROWS = [
    ("alice chat, filed under her email", ALICE_EMAIL, "message"),
    ("alice chat, filed under her id", str(ALICE), "message"),
    ("alice saved note", ALICE_EMAIL, "note"),
    ("bob chat", BOB_EMAIL, "message"),
    ("carol chat", CAROL_NAME, "message"),
    ("vault entry", VAULT_NAMESPACE, "projects"),
    ("vault-namespace chat row", VAULT_NAMESPACE, "message"),
]
ALICE_CHAT = {"alice chat, filed under her email", "alice chat, filed under her id"}


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
    """One tenant and workspace shared by three accounts, with one memory per row
    in `ROWS`."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Chat memory erasure {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for user, email, subject in ((ALICE, ALICE_EMAIL, None), (BOB, BOB_EMAIL, None),
                                     (CAROL, None, CAROL_NAME)):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) "
                "VALUES ($1,$2,'R','password',$3,'active')", user, email, subject)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, user)
        for label, user_id, source_type in ROWS:
            await admin.execute(
                "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,source_type,content) "
                "VALUES ($1,$2,$3,$4,$5)", TENANT, WORKSPACE, user_id, source_type, label)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for user in (ALICE, BOB, CAROL):
        await admin.execute("DELETE FROM users WHERE id = $1", user)


async def _remaining(admin) -> set[str]:
    rows = await admin.fetch(
        "SELECT content FROM memory_embeddings WHERE tenant_id = $1", TENANT)
    return {r["content"] for r in rows}


def _principal(user: uuid.UUID, username: str):
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=username, is_owner=False)


EVERYTHING = {label for label, _, _ in ROWS}


async def test_deleting_conversations_removes_what_chat_remembered_from_them(seeded):
    from core.privacy.erasure import erase_postgres

    result = await erase_postgres(_principal(ALICE, ALICE_EMAIL), scope="conversations")

    assert result.ok, result
    assert await _remaining(seeded) == EVERYTHING - ALICE_CHAT, (
        "deleting Alice's conversations must remove her chat memories, and nothing else")


async def test_the_vault_and_saved_notes_survive(seeded):
    from core.privacy.erasure import erase_postgres

    await erase_postgres(_principal(ALICE, ALICE_EMAIL), scope="conversations")
    left = await _remaining(seeded)

    assert {"vault entry", "vault-namespace chat row"} <= left, (
        "deleting conversations reached the Memory Vault")
    assert "alice saved note" in left, "deleting conversations removed a saved note"


async def test_a_colleagues_chat_memories_survive(seeded):
    from core.privacy.erasure import erase_postgres

    await erase_postgres(_principal(ALICE, ALICE_EMAIL), scope="conversations")

    assert {"bob chat", "carol chat"} <= await _remaining(seeded), (
        "one person's deletion removed another person's chat memories")


@pytest.mark.parametrize("username", [CAROL_NAME, ""], ids=["by-name", "no-name"])
async def test_a_private_account_name_is_found_in_its_users_row(seeded, username):
    """The private edition files chat memories under the account name, kept in
    `users.external_subject`; it is found even when the principal carries no name."""
    from core.privacy.erasure import erase_postgres

    result = await erase_postgres(_principal(CAROL, username), scope="conversations")

    assert result.ok, result
    assert await _remaining(seeded) == EVERYTHING - {"carol chat"}


async def test_the_vector_check_reports_chat_memories_left_behind(seeded):
    """The `pgvector` store line must say whether the chat memories are gone, not
    report that conversations have no embeddings."""
    from core.privacy.erasure import erase_postgres, erase_vectors

    alice = _principal(ALICE, ALICE_EMAIL)
    before = await erase_vectors(alice, scope="conversations")
    assert not before.ok and before.unerasable == 2, before

    await erase_postgres(alice, scope="conversations")
    after = await erase_vectors(alice, scope="conversations")
    assert after.ok, after
