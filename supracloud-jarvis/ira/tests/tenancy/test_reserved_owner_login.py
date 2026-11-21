"""An owner login that is a reserved memory name (W-22).

Chat files the owner's messages under their login. `owner`, `system`,
`self_knowledge`, `meeting` and `technical_library` are namespaces IRA keeps for
its own memory, and deletion never touches them, so an owner whose login is
`owner` found that "delete my conversations" kept everything chat remembered,
while the erasure result reported success. The owner chose, on 2026-10-19:

  * a new install with such a login refuses to start, before writing anything;
  * an install that already has an account under it keeps starting and logs
    why, because refusing would lock the owner out and changing the login
    would separate the account from its data;
  * erasure counts the chat memories it kept, so it never reports success for
    a deletion that did not happen.

`docs/security/RESERVED_OWNER_LOGIN_2026-10-19.md` records the decision.

Real PostgreSQL, real RLS, as `ira_app`. Rows are seeded through the admin DSN,
so no embedding model is involved.
"""
from __future__ import annotations

import logging
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

from core.editions import EDITION_PRIVATE  # noqa: E402
from core.identity.principal import private_user_id  # noqa: E402


@pytest.fixture(autouse=True)
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


async def _forget_account(user_id: uuid.UUID) -> None:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("DELETE FROM workspace_memberships WHERE user_id=$1", user_id)
        await admin.execute("DELETE FROM tenant_memberships WHERE user_id=$1", user_id)
        await admin.execute("DELETE FROM users WHERE id=$1", user_id)
    finally:
        await admin.close()


async def _account_exists(user_id: uuid.UUID) -> bool:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        return bool(await admin.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE id=$1)", user_id))
    finally:
        await admin.close()


@pytest.fixture
async def configured_owner(monkeypatch):
    """Set the private edition's owner login for one test; remove any account
    it leaves behind."""
    from config import get_settings

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)
    used: list[uuid.UUID] = []

    def _set(name: str) -> uuid.UUID:
        monkeypatch.setattr(get_settings(), "ira_admin_username", name, raising=False)
        used.append(private_user_id(name))
        return used[-1]

    yield _set
    for user_id in used:
        await _forget_account(user_id)


# ── Startup ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["owner", "Owner", "system", "technical_library"])
async def test_a_new_install_with_a_reserved_owner_login_refuses_to_start(
        configured_owner, name):
    from core.identity.provisioning import ReservedOwnerLogin, ensure_private_owner_identity

    user_id = configured_owner(name)
    await _forget_account(user_id)  # a new install: no account for this login yet

    with pytest.raises(ReservedOwnerLogin, match="reserves for its own memory"):
        await ensure_private_owner_identity()

    assert not await _account_exists(user_id), (
        "the refusal must come before anything is written")


async def test_an_install_that_already_has_the_account_keeps_starting_and_says_why(
        configured_owner, caplog):
    """Refusing here would lock the owner out of an install that worked yesterday."""
    from core.identity.provisioning import ensure_private_owner_identity

    user_id = configured_owner("owner")
    await _forget_account(user_id)
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(
            "INSERT INTO users (id, display_name, auth_provider, external_subject) "
            "VALUES ($1, 'Owner', 'password', 'owner')", user_id)
    finally:
        await admin.close()

    with caplog.at_level(logging.ERROR, logger="ira.identity.provisioning"):
        result = await ensure_private_owner_identity()

    assert result is not None and result.user_id == user_id
    assert result.created_tenant_membership, "provisioning must carry on as before"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("reserves for its own memory" in w and "not removed" in w
               for w in warnings), warnings


async def test_an_ordinary_owner_login_is_unaffected(configured_owner, caplog):
    from core.identity.provisioning import ensure_private_owner_identity

    name = f"w22-{uuid.uuid4().hex[:8]}"
    user_id = configured_owner(name)

    with caplog.at_level(logging.ERROR, logger="ira.identity.provisioning"):
        result = await ensure_private_owner_identity()

    assert result is not None and result.created_user and result.user_id == user_id
    assert not [r for r in caplog.records if "reserves" in r.getMessage()]


# ── Erasure ──────────────────────────────────────────────────────────────────

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"w22-{TENANT.hex[:8]}"
#: The account whose login is reserved, and an ordinary colleague.
OWNER = uuid.uuid4()
ALICE = uuid.uuid4()
ALICE_NAME = f"alice-{SLUG}"


def _rows(reserved_name: str) -> list[tuple[str, str, str]]:
    """(label, user_id, source_type). The label is the row's content."""
    return [
        ("owner chat 1, under the login", reserved_name, "message"),
        ("owner chat 2, under the login", reserved_name, "message"),
        ("owner chat, under the id", str(OWNER), "message"),
        ("vault fact", "owner", "fact"),
        ("alice chat", ALICE_NAME, "message"),
    ]


@pytest.fixture
async def seeded(request):
    """One workspace: an owner whose login is `request.param[0]` (kept in the
    users row when `request.param[1]`), and a colleague."""
    reserved_name, in_users_row = request.param
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Reserved owner login {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for user, subject in ((OWNER, reserved_name if in_users_row else None),
                              (ALICE, ALICE_NAME)):
            await admin.execute(
                "INSERT INTO users (id,display_name,auth_provider,external_subject,status) "
                "VALUES ($1,'R','password',$2,'active')", user, subject)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, user)
        for label, user_id, source_type in _rows(reserved_name):
            await admin.execute(
                "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,source_type,content) "
                "VALUES ($1,$2,$3,$4,$5)", TENANT, WORKSPACE, user_id, source_type, label)
        yield admin, reserved_name, in_users_row
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for user in (OWNER, ALICE):
        await admin.execute("DELETE FROM users WHERE id = $1", user)


def _principal(user: uuid.UUID, username: str):
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=username, is_owner=False)


async def _remaining(admin) -> set[str]:
    rows = await admin.fetch(
        "SELECT content FROM memory_embeddings WHERE tenant_id = $1", TENANT)
    return {r["content"] for r in rows}


# The login carried by the principal (a request), and the login found only in
# the users row (the scheduled pass, which builds its principal with no name).
#
# "System" rather than "Owner" for the second: `test_retention_is_per_user.py`
# already creates a password account named "Owner", and the two must not meet on
# the (auth_provider, external_subject) unique index.
SHAPES = [("owner", False), ("System", True)]
SHAPE_IDS = ["login-on-principal", "login-in-users-row"]


@pytest.mark.parametrize("seeded", SHAPES, ids=SHAPE_IDS, indirect=True)
async def test_erasure_reports_chat_memories_kept_under_a_reserved_login(seeded):
    from core.privacy.erasure import erase_postgres, erase_vectors

    admin, reserved_name, in_users_row = seeded
    owner = _principal(OWNER, "" if in_users_row else reserved_name)

    await erase_postgres(owner, scope="conversations")
    result = await erase_vectors(owner, scope="conversations")

    assert not result.ok, "a deletion that kept chat memories must not report success"
    assert result.unerasable == 2, result
    assert "reserves for its own memory" in (result.error or ""), result
    # What it could delete went, and nothing it must not touch did.
    assert await _remaining(admin) == {
        "owner chat 1, under the login", "owner chat 2, under the login",
        "vault fact", "alice chat"}


@pytest.mark.parametrize("seeded", SHAPES[:1], ids=SHAPE_IDS[:1], indirect=True)
async def test_a_colleague_is_not_charged_with_the_owners_kept_rows(seeded):
    """Only the account's own reserved names count: a colleague's erasure in the
    same workspace still reports success."""
    from core.privacy.erasure import erase_postgres, erase_vectors

    alice = _principal(ALICE, ALICE_NAME)
    await erase_postgres(alice, scope="conversations")
    result = await erase_vectors(alice, scope="conversations")

    assert result.ok, result
