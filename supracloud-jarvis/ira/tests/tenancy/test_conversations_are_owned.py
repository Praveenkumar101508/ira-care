"""Conversations belong to one user (ADR-0009, migration 093).

Two people, Alice and Bob, share ONE tenant and ONE workspace. Before 093 the
four policies on `conversations` and `messages` checked tenant and workspace
only, and on `main` at c8f262f Bob could:

  1. read Alice's chat history, given her session id (`/chat/history`);
  2. receive Alice's conversation in his own data export;
  3. delete Alice's conversation with "delete my conversations".

Each is asserted here in the direction that must now hold, through the same
functions the routes call, as `ira_app` with RLS forced. A plant that drops the
owner predicate from the policies turns every one of them red.

Rows are seeded through the admin DSN with `owner_user_id` stated, as 093's
backfill or the column default would have set it. A row seeded WITHOUT an owner
is a legacy row: hidden from everyone, and reached by no personal deletion.
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

# `memory.store` imports `memory.embeddings`, which imports `SentenceTransformer`
# by name. conftest stubs the package without that attribute; nothing here
# embeds anything.
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

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
ALICE = uuid.uuid4()
BOB = uuid.uuid4()
SLUG = f"convown-{TENANT.hex[:8]}"
ALICE_SESSION = f"alice-session-{SLUG}"
LEGACY_SESSION = f"legacy-session-{SLUG}"


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
    """One tenant and workspace shared by Alice and Bob. Alice has a two-message
    conversation; a third, legacy conversation has no owner."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Conversation ownership {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for user, name in ((ALICE, "alice"), (BOB, "bob")):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status) "
                "VALUES ($1,$2,'R','password','active')", user, f"{name}-{SLUG}@own.test")
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, user)

        alice_conv = await admin.fetchval(
            "INSERT INTO conversations (tenant_id,workspace_id,session_id,owner_user_id,created_at) "
            "VALUES ($1,$2,$3,$4, NOW() - INTERVAL '400 days') RETURNING id",
            TENANT, WORKSPACE, ALICE_SESSION, ALICE)
        for role, text in (("user", "Alice: my private question"), ("assistant", "answer to Alice")):
            await admin.execute(
                "INSERT INTO messages (conversation_id,tenant_id,workspace_id,owner_user_id,"
                " role,content,created_at) VALUES ($1,$2,$3,$4,$5,$6, NOW() - INTERVAL '400 days')",
                alice_conv, TENANT, WORKSPACE, ALICE, role, text)

        legacy_conv = await admin.fetchval(
            "INSERT INTO conversations (tenant_id,workspace_id,session_id,created_at) "
            "VALUES ($1,$2,$3, NOW() - INTERVAL '400 days') RETURNING id",
            TENANT, WORKSPACE, LEGACY_SESSION)
        await admin.execute(
            "INSERT INTO messages (conversation_id,tenant_id,workspace_id,role,content,created_at) "
            "VALUES ($1,$2,$3,'user','nobody can be named for this', NOW() - INTERVAL '400 days')",
            legacy_conv, TENANT, WORKSPACE)
        yield admin, alice_conv, legacy_conv
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for user in (ALICE, BOB):
        await admin.execute("DELETE FROM users WHERE id = $1", user)


def _principal(user):
    from core.identity.principal import Principal

    name = "alice" if user == ALICE else "bob"
    return Principal(user_id=user, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=f"{name}-{SLUG}@own.test", is_owner=False)


async def _exists(admin, conv_id) -> bool:
    return bool(await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", conv_id))


# ── 1. reading ──────────────────────────────────────────────────────────────

async def test_another_users_session_id_reads_nothing(seeded):
    """What `GET /chat/history` does: `ensure_conversation`, then
    `get_recent_messages`. Bob naming Alice's session gets a conversation of his
    own, empty, and Alice's is untouched."""
    from memory.store import ensure_conversation, get_recent_messages

    admin, alice_conv, _ = seeded
    bobs = await ensure_conversation(_principal(BOB), ALICE_SESSION)
    assert bobs != str(alice_conv), "Bob's lookup resolved to Alice's conversation"
    assert await get_recent_messages(_principal(BOB), bobs, limit=50) == []
    assert await admin.fetchval(
        "SELECT owner_user_id FROM conversations WHERE id = $1", uuid.UUID(bobs)) == BOB

    alices = await ensure_conversation(_principal(ALICE), ALICE_SESSION)
    assert alices == str(alice_conv)
    msgs = await get_recent_messages(_principal(ALICE), alices, limit=50)
    assert [m["content"] for m in msgs] == ["Alice: my private question", "answer to Alice"]


async def test_reading_by_conversation_id_is_refused_too(seeded):
    """The context read takes a conversation id, not a session. Bob holding
    Alice's id still reads nothing."""
    from memory.store import get_recent_messages

    _, alice_conv, _ = seeded
    assert await get_recent_messages(_principal(BOB), str(alice_conv), limit=50) == []


# ── 2. export ───────────────────────────────────────────────────────────────

async def test_an_export_holds_only_the_callers_conversations(seeded):
    from core.privacy.export import export_subject_data

    def counts(result):
        return {t.table: t.count for t in result.tables}

    bobs = counts(await export_subject_data(_principal(BOB), scope="conversations"))
    assert bobs == {"messages": 0, "conversations": 0}, (
        f"Bob's export carried someone else's conversation: {bobs}")
    alices = counts(await export_subject_data(_principal(ALICE), scope="conversations"))
    assert alices == {"messages": 2, "conversations": 1}, (
        f"Alice's export missed her own conversation, or carried the legacy one: {alices}")


# ── 3. erasure ──────────────────────────────────────────────────────────────

async def test_erasure_deletes_only_the_callers_conversations(seeded):
    from core.privacy.erasure import erase_postgres

    admin, alice_conv, legacy_conv = seeded
    result = await erase_postgres(_principal(BOB), scope="conversations")
    assert result.ok, result
    assert await _exists(admin, alice_conv), "Bob's erasure deleted Alice's conversation"

    result = await erase_postgres(_principal(ALICE), scope="conversations")
    assert result.ok, result
    assert not await _exists(admin, alice_conv), "Alice's erasure left her own conversation"
    assert await admin.fetchval(
        "SELECT count(*) FROM messages WHERE conversation_id = $1", alice_conv) == 0
    assert await _exists(admin, legacy_conv), (
        "a personal erasure deleted a conversation nobody owns")


# ── retention ───────────────────────────────────────────────────────────────

async def test_a_conversation_window_reaches_only_the_callers_conversations(seeded):
    from core.privacy.retention import enforce_for
    from core.privacy.store import RetentionSettings, save_settings

    admin, alice_conv, legacy_conv = seeded
    await save_settings(_principal(BOB), RetentionSettings(conversation_retention_days=1))
    outcome = await enforce_for(_principal(BOB))
    assert outcome.conversations_deleted == 0 and outcome.messages_deleted == 0
    assert await _exists(admin, alice_conv), "Bob's window deleted Alice's conversation"

    await save_settings(_principal(ALICE), RetentionSettings(conversation_retention_days=1))
    outcome = await enforce_for(_principal(ALICE))
    assert (outcome.conversations_deleted, outcome.messages_deleted) == (1, 2)
    assert await _exists(admin, legacy_conv), "a personal window deleted a legacy conversation"


# ── the rows nobody owns, and the write side ────────────────────────────────

async def test_a_conversation_nobody_owns_is_visible_to_nobody(seeded):
    from memory.store import ensure_conversation, get_recent_messages

    _, _, legacy_conv = seeded
    for user in (ALICE, BOB):
        assert await get_recent_messages(_principal(user), str(legacy_conv), limit=50) == []
        conv = await ensure_conversation(_principal(user), LEGACY_SESSION)
        assert conv != str(legacy_conv)


async def test_a_message_cannot_be_filed_into_another_users_conversation(seeded):
    """`save_message` takes a conversation id. A message carrying Bob as owner
    and naming Alice's conversation is refused by the composite foreign key,
    even before RLS has a say."""
    from memory.store import save_message

    admin, alice_conv, _ = seeded
    with pytest.raises(asyncpg.exceptions.PostgresError):
        await save_message(_principal(BOB), str(alice_conv), "user",
                           "slipped into Alice's history", user_id=str(BOB))
    assert await admin.fetchval(
        "SELECT count(*) FROM messages WHERE conversation_id = $1", alice_conv) == 2


async def test_new_rows_take_their_owner_from_the_principal(seeded):
    from memory.store import ensure_conversation, save_message

    admin, _, _ = seeded
    conv = await ensure_conversation(_principal(BOB), f"bob-new-{SLUG}")
    await save_message(_principal(BOB), conv, "user", "hello", user_id=str(BOB))
    assert await admin.fetchval(
        "SELECT owner_user_id FROM conversations WHERE id = $1", uuid.UUID(conv)) == BOB
    assert await admin.fetchval(
        "SELECT array_agg(DISTINCT owner_user_id) FROM messages WHERE conversation_id = $1",
        uuid.UUID(conv)) == [BOB]


async def test_the_policies_check_the_owner(seeded):
    """The predicate itself, so a later migration that rebuilds these policies
    from 049's template cannot silently drop it."""
    admin, _, _ = seeded
    rows = await admin.fetch(
        "SELECT tablename, cmd, coalesce(qual, with_check) AS predicate FROM pg_policies "
        " WHERE tablename IN ('conversations','messages')")
    assert len(rows) == 8
    for r in rows:
        assert "app.user_id" in r["predicate"] and "owner_user_id" in r["predicate"], (
            f"{r['tablename']} {r['cmd']} policy does not check the owner")
