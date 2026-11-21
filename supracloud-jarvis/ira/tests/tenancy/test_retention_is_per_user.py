"""One user's memory window deletes that user's memories, and nobody else's (#30).

`enforce_for` applied the caller's `memory_retention_days` as an age-based
`DELETE FROM memory_embeddings` on the caller's workspace connection. The
workspace policy narrows by tenant and workspace only, so the DELETE reached
every user's rows in the workspace. User B never chose a window, and lost a
400-day-old memory because user A chose 30 days.

The rule, decided by the owner on 2026-10-19:

  * a row is the account's own when its `user_id` is the account's UUID or a
    name the account is known by: the username chat files memories under
    (`principal.username`), or the same names read from `users` for that id;
  * a reserved namespace (`memory.namespaces`) is never one account's own:
    the Memory Vault (`owner`), `system`, meeting transcripts and the reference
    corpora are shared. One person's window does not delete them; the
    workspace's weekly purge ages them out (`test_retention_spares_reference_corpora`).

The names are read from `users` because the scheduled pass
(`core/jobs/scheduler.py::run_retention_once`) builds its principal with
`username=""`. Matching `principal.username` alone would have made the
scheduled pass delete none of the rows chat wrote under a name, while still
reporting success. Every test here runs both principal shapes.

Real PostgreSQL, real RLS, as `ira_app`. Rows are seeded and aged through the
admin DSN, so no embedding model is involved.
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
# stubs the package without that attribute; nothing here instantiates it.
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
SLUG = f"retuser-{TENANT.hex[:8]}"

#: Two cloud accounts sharing one workspace. Chat files their memories under
#: the normalised email (`api/routes/chat.py::_workspace_username`).
ALICE = uuid.uuid4()
BOB = uuid.uuid4()
ALICE_EMAIL = f"alice-{SLUG}@retention.test"
BOB_EMAIL = f"bob-{SLUG}@retention.test"
#: A private-edition style account: no email, its name in `external_subject`
#: (`core/identity/provisioning.py::_ensure_user`).
CAROL = uuid.uuid4()
CAROL_NAME = f"carol-{SLUG}"
#: An account whose stored name spells a reserved namespace in another case.
DAVE = uuid.uuid4()
DAVE_NAME = "Owner"

SHARED = [VAULT_NAMESPACE, SYSTEM_NAMESPACE, MEETING_NAMESPACE,
          TECH_LIBRARY_NAMESPACE, SELF_KNOWLEDGE_NAMESPACE]

OLD_DAYS = 400
FRESH_DAYS = 2
WINDOW_DAYS = 30


def _label(user_id: str, age: int) -> str:
    return f"{user_id}/{age}d"


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
    """One tenant, one default workspace, four accounts, and an old and a fresh
    memory under every account name and every shared namespace."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Retention per user {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) "
            "VALUES ($1,'O',$2) RETURNING id", TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
            " is_default) VALUES ($1,$2,$3,'W',$4,TRUE)",
            WORKSPACE, TENANT, org, SLUG)
        for user, email, subject in ((ALICE, ALICE_EMAIL, None),
                                     (BOB, BOB_EMAIL, None),
                                     (CAROL, None, CAROL_NAME),
                                     (DAVE, None, DAVE_NAME)):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,"
                " external_subject,status) "
                "VALUES ($1,$2,'R','password',$3,'active')", user, email, subject)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            await admin.execute(
                "INSERT INTO workspace_memberships "
                " (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, user)

        for user_id in _every_tag():
            for age in (OLD_DAYS, FRESH_DAYS):
                await admin.execute(
                    "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,"
                    " source_type,content,created_at) "
                    "VALUES ($1,$2,$3,'note',$4, NOW() - ($5 || ' days')::interval)",
                    TENANT, WORKSPACE, user_id, _label(user_id, age), str(age))
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


def _every_tag() -> list[str]:
    return [str(ALICE), ALICE_EMAIL, str(BOB), BOB_EMAIL,
            str(CAROL), CAROL_NAME, str(DAVE), DAVE_NAME, *SHARED]


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for user in (ALICE, BOB, CAROL, DAVE):
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


#: The two ways a pass is started. `scheduled` is built exactly as
#: `run_retention_once` builds it; `signed_in` is the principal the
#: `POST /api/v1/privacy/settings/enforce` route receives.
SHAPES = {
    "scheduled": lambda user, name: _principal(user, ""),
    "signed_in": lambda user, name: _principal(user, name),
}


async def _window_for(user, name, shape):
    from core.privacy.retention import enforce_for
    from core.privacy.store import RetentionSettings, save_settings

    principal = SHAPES[shape](user, name)
    await save_settings(principal, RetentionSettings(memory_retention_days=WINDOW_DAYS))
    return await enforce_for(principal)


def _all_but(*gone: str) -> set[str]:
    everything = {_label(t, age) for t in _every_tag() for age in (OLD_DAYS, FRESH_DAYS)}
    return everything - {_label(t, OLD_DAYS) for t in gone}


@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_one_users_window_leaves_another_users_memories(seeded, shape):
    """The reproduction from #30: Alice chooses 30 days, Bob chooses nothing.

    Only Alice's old rows go, under both names chat may have filed them under.
    Bob's old rows stay, and so does every shared namespace.
    """
    outcome = await _window_for(ALICE, ALICE_EMAIL, shape)

    assert await _remaining(seeded) == _all_but(str(ALICE), ALICE_EMAIL), (
        "a memory window reached rows that are not this user's own")
    assert outcome.memories_deleted == 2


@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_a_private_account_name_is_found_in_its_users_row(seeded, shape):
    """The private edition files chat memories under the account name, which
    `users` keeps in `external_subject`. The scheduled pass knows only the UUID,
    so without reading the row it would delete none of them."""
    outcome = await _window_for(CAROL, CAROL_NAME, shape)

    assert await _remaining(seeded) == _all_but(str(CAROL), CAROL_NAME)
    assert outcome.memories_deleted == 2


@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_an_account_named_like_a_namespace_cannot_claim_it(seeded, shape):
    """An account called `Owner` is not the Memory Vault. Its window deletes
    its UUID-filed rows only, and the vault's rows survive it."""
    outcome = await _window_for(DAVE, DAVE_NAME, shape)

    assert await _remaining(seeded) == _all_but(str(DAVE))
    assert outcome.memories_deleted == 1
