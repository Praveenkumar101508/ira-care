"""Durable "log out everywhere": the token version lives in PostgreSQL (096).

The owner approved the review package of 21 October as written on 23 October
(docs/security/GLOBAL_LOGOUT_REVIEW_PACKAGE_2026-10-21.md, decisions A-F).
These are its regression tests 1-17, numbered as in its §6.

── The defect ──────────────────────────────────────────────────────────────

The version lived only in Redis, and a missing key read as 0. Every way Redis
loses or lowers a key (deleted, a restart without persistence, a restore from
an older snapshot, unreachable and failing open) revived every token issued
before the last "log out everywhere".

── How Redis failures are produced here ────────────────────────────────────

By their effect on the real test Redis (IRA_TEST_REDIS_URL), so the tests run
the same in CI, where Redis is a service container this job cannot restart:

  * a deleted key: DEL on the account's `ira:token_ver:<name>`;
  * a restart with no persistence: every token-version key of the account
    gone, which is what such a restart leaves;
  * an older snapshot: the key set back to a lower value;
  * unreachable: the client pointed at a port nothing listens on.

PostgreSQL is the real test database. Each test makes its own account, with a
unique name, and removes it afterwards.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")
redis = pytest.importorskip("redis.asyncio", reason="redis not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
REDIS_URL = os.getenv("IRA_TEST_REDIS_URL", "")
if not (DSN and ADMIN_DSN and REDIS_URL):
    pytest.skip("IRA_TEST_DATABASE_DSN, IRA_TEST_ADMIN_DSN and IRA_TEST_REDIS_URL "
                "are needed", allow_module_level=True)

from api.middleware import auth  # noqa: E402
from config import get_settings  # noqa: E402
from core.identity.principal import PRIVATE_TENANT_ID, private_user_id  # noqa: E402

POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "096_auth_token_versions.sql"
ROLLBACK = POSTGRES / "rollback" / "096_auth_token_versions_down.sql"
VER_KEY = "ira:token_ver:"
#: Nothing listens here: the unreachable-Redis case.
DEAD_REDIS = "redis://127.0.0.1:1/0"


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def acct(admin, monkeypatch):
    """A private-edition owner account with a `users` row, the app wired to the
    real test database and Redis, and an HTTP client on the real app."""
    import utils.db as db
    from utils import redis_client

    name = f"logout-{uuid.uuid4().hex[:10]}"
    user_id = private_user_id(name)
    await admin.execute(
        "INSERT INTO users (id, display_name, auth_provider, external_subject) "
        "VALUES ($1, $2, 'password', $2)", user_id, name)

    monkeypatch.setenv("IRA_EDITION", "private")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("IRA_ADMIN_USERNAME", name)
    monkeypatch.setenv("IRA_SECRET_KEY", "local-disposable-logout-test-key-2026")
    get_settings.cache_clear()

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=8)
    previous_pool, db._pool = db._pool, pool
    cache = redis.from_url(REDIS_URL, decode_responses=True,
                           socket_connect_timeout=2, socket_timeout=2)
    assert await cache.ping()
    monkeypatch.setattr(redis_client, "_redis", cache)

    from app import factory

    app = factory.create_app()
    limiter = getattr(app.state, "limiter", None)
    was_enabled = getattr(limiter, "enabled", None)
    if limiter is not None:
        limiter.enabled = False     # the refresh limit is per minute, per process
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="https://test", follow_redirects=False)
    state = SimpleNamespace(name=name, user_id=user_id, admin=admin, cache=cache,
                            client=client, pool=pool)
    try:
        yield state
    finally:
        await client.aclose()
        if limiter is not None:
            limiter.enabled = was_enabled
        await cache.delete(VER_KEY + name)
        await cache.aclose()
        db._pool = previous_pool
        await pool.close()
        await admin.execute("DELETE FROM auth_token_versions WHERE user_id = $1", user_id)
        await admin.execute("DELETE FROM users WHERE id = $1", user_id)
        get_settings.cache_clear()


async def _login(a):
    return await auth.create_login_tokens(a.name)


async def _logout_all(a, access_token):
    return await a.client.post("/auth/logout/all",
                               headers={"Authorization": f"Bearer {access_token}"})


async def _refresh(a, refresh_token):
    return await a.client.post("/auth/refresh",
                               headers={"Authorization": f"Bearer {refresh_token}"})


async def _access_ok(token) -> bool:
    """Does `require_auth` accept this access token?"""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    try:
        await auth.require_auth(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return True
    except HTTPException as e:
        if e.status_code == 401:
            return False
        raise


async def _table_version(a) -> int | None:
    return await a.admin.fetchval(
        "SELECT version FROM auth_token_versions WHERE user_id = $1", a.user_id)


async def _signed_out_everywhere(a):
    """Log in, log out everywhere, and return the tokens from before."""
    old = await _login(a)
    r = await _logout_all(a, old.access_token)
    assert r.status_code == 200, r.text
    return old


# ── 1-6: Redis loses, lowers or cannot serve the key ─────────────────────────

async def test_01_after_logout_all_the_old_refresh_token_is_refused(acct):
    old = await _signed_out_everywhere(acct)
    assert (await _refresh(acct, old.refresh_token)).status_code == 401
    assert not await _access_ok(old.access_token)


async def test_02_after_the_redis_key_is_deleted(acct):
    old = await _signed_out_everywhere(acct)
    await acct.cache.delete(VER_KEY + acct.name)
    assert (await _refresh(acct, old.refresh_token)).status_code == 401
    assert not await _access_ok(old.access_token)


async def test_03_after_redis_restarts_with_no_data(acct):
    old = await _signed_out_everywhere(acct)
    # What a restart without persistence leaves: no token-version key at all.
    async for key in acct.cache.scan_iter(match=f"{VER_KEY}*{acct.name}*"):
        await acct.cache.delete(key)
    assert (await _refresh(acct, old.refresh_token)).status_code == 401
    assert not await _access_ok(old.access_token)


async def test_04_after_redis_is_restored_from_an_older_snapshot(acct):
    first = await _signed_out_everywhere(acct)
    middle = await _login(acct)
    assert (await _logout_all(acct, middle.access_token)).status_code == 200
    # The snapshot was taken between the two sign-outs: a LOWER value.
    await acct.cache.set(VER_KEY + acct.name, 1)
    for old in (first, middle):
        assert (await _refresh(acct, old.refresh_token)).status_code == 401
        assert not await _access_ok(old.access_token)


async def test_05_a_token_issued_after_the_logout_works_in_1_to_4(acct):
    await _signed_out_everywhere(acct)
    new = await _login(acct)
    for damage in ("none", "deleted", "lowered"):
        if damage == "deleted":
            await acct.cache.delete(VER_KEY + acct.name)
        if damage == "lowered":
            await acct.cache.set(VER_KEY + acct.name, 0)
        assert await _access_ok(new.access_token), damage
        r = await _refresh(acct, new.refresh_token)
        assert r.status_code == 200, (damage, r.text)
        # The token the refresh mints carries the table's version, not
        # Redis's, so it works too.
        minted = r.json()["access_token"]
        assert auth.decode_token(minted).ver == await _table_version(acct), damage
        assert await _access_ok(minted), damage


async def test_06_redis_unreachable_old_refresh_refused_new_token_works(acct, monkeypatch):
    from utils import redis_client

    old = await _signed_out_everywhere(acct)
    new = await _login(acct)
    dead = redis.from_url(DEAD_REDIS, decode_responses=True,
                          socket_connect_timeout=0.5, socket_timeout=0.5)
    monkeypatch.setattr(redis_client, "_redis", dead)
    try:
        assert (await _refresh(acct, old.refresh_token)).status_code == 401
        assert not await _access_ok(old.access_token)
        assert await _access_ok(new.access_token)
    finally:
        await dead.aclose()


# ── 7-8: PostgreSQL unreachable; concurrency ─────────────────────────────────

async def test_07_postgres_unreachable_check_and_logout_all_are_503_and_nothing_changes(
        acct, monkeypatch):
    from contextlib import asynccontextmanager

    import utils.db as db
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    tokens = await _login(acct)
    before = await _table_version(acct)

    @asynccontextmanager
    async def unreachable():
        raise ConnectionRefusedError("synthetic PostgreSQL outage")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "acquire", unreachable)
    with pytest.raises(HTTPException) as caught:
        await auth.require_auth(HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=tokens.access_token))
    assert caught.value.status_code == 503
    with pytest.raises(HTTPException) as caught:
        await auth.bump_token_version(acct.name)
    assert caught.value.status_code == 503
    monkeypatch.undo()
    assert await _table_version(acct) == before


async def test_08_fifty_concurrent_bumps_raise_the_version_by_exactly_fifty(acct):
    from core.identity import token_versions

    await _login(acct)
    start = await _table_version(acct)
    await asyncio.gather(*(token_versions.bump(acct.user_id) for _ in range(50)))
    assert await _table_version(acct) == start + 50


# ── 9: what each role may do to the table ────────────────────────────────────

async def _as(conn, role: str, sql: str, *args, bound=None, fetch=False):
    """Run one statement as `role` in a transaction that is rolled back.

    `bound` binds `app.user_id` for the transaction first, as
    `token_versions._account_bound` does for every statement the app runs.
    """
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(f"SET LOCAL ROLE {role}")
        if bound is not None:
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(bound))
        if fetch:
            return await conn.fetchval(sql, *args)
        return await conn.execute(sql, *args)
    finally:
        await tx.rollback()


async def test_09_roles_lowering_raises_delete_refused_worker_cannot_update_purge_waits(acct):
    from core.identity import token_versions

    await _login(acct)
    await token_versions.bump(acct.user_id)
    a = acct.admin
    with pytest.raises(asyncpg.RaiseError, match="may not decrease"):
        await _as(a, "ira_app", "UPDATE auth_token_versions SET version = 0 WHERE user_id = $1",
                  acct.user_id, bound=acct.user_id)
    # ira_app may DELETE only through the purge policy: a live account's
    # counter is not deletable, so the statement matches nothing.
    assert await _as(a, "ira_app", "DELETE FROM auth_token_versions WHERE user_id = $1",
                     acct.user_id) == "DELETE 0"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _as(a, "ira_worker", "UPDATE auth_token_versions SET version = version + 1 "
                                   "WHERE user_id = $1", acct.user_id)
    # Erased three days ago: under seven, so the purge policy admits nothing.
    await a.execute("UPDATE users SET deleted_at = now() - interval '3 days' WHERE id = $1",
                    acct.user_id)
    status = await _as(a, "ira_worker", "DELETE FROM auth_token_versions WHERE user_id = $1",
                       acct.user_id)
    assert status == "DELETE 0"
    assert await _table_version(acct) == 1


# ── 10: start-up seeding ─────────────────────────────────────────────────────

async def test_10_seeding_carries_a_redis_counter_in_and_old_tokens_stay_refused(acct):
    from core.identity import token_versions

    old = auth._make_access_token(acct.name, ver=2)[0]
    await acct.cache.set(VER_KEY + acct.name, 3)
    assert await _table_version(acct) is None
    report = await token_versions.seed_from_redis()
    assert await _table_version(acct) == 3
    assert acct.name not in report.unmatched
    await acct.cache.delete(VER_KEY + acct.name)
    assert not await _access_ok(old)
    # Idempotent, and never lowers: a lower Redis value changes nothing.
    await acct.cache.set(VER_KEY + acct.name, 1)
    await token_versions.seed_from_redis()
    assert await _table_version(acct) == 3


# ── 11: an open socket ───────────────────────────────────────────────────────

class _Socket:
    def __init__(self):
        self.closed_with = None

    async def close(self, code=1000):
        self.closed_with = code


async def test_11_an_open_socket_closes_4401_after_logout_all_and_on_database_errors(
        acct, monkeypatch):
    from contextlib import asynccontextmanager

    import utils.db as db

    tokens = await _login(acct)
    claims = auth.decode_token(tokens.access_token)

    sock = _Socket()
    watcher = asyncio.create_task(auth.watch_socket_credential(sock, claims, interval=0.05))
    await asyncio.sleep(0.12)
    assert sock.closed_with is None, "a live credential must keep its socket"
    assert (await _logout_all(acct, tokens.access_token)).status_code == 200
    await asyncio.wait_for(watcher, timeout=0.5)          # within a few polls
    assert sock.closed_with == auth.SOCKET_CREDENTIAL_REVOKED

    fresh = auth.decode_token((await _login(acct)).access_token)

    @asynccontextmanager
    async def unreachable():
        raise ConnectionRefusedError("synthetic PostgreSQL outage")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "acquire", unreachable)
    sock = _Socket()
    await asyncio.wait_for(
        auth.watch_socket_credential(sock, fresh, interval=0.02, max_errors=2), timeout=1.0)
    assert sock.closed_with == auth.SOCKET_CREDENTIAL_REVOKED, \
        "a re-check that cannot reach the database must never keep the socket"


# ── 12: the other two bump sites ─────────────────────────────────────────────

async def test_12_playbook_rotation_bumps_the_table_and_reports_a_database_error(
        acct, monkeypatch):
    from contextlib import asynccontextmanager

    import utils.db as db
    from utils import playbooks

    await _login(acct)
    start = await _table_version(acct)
    assert await playbooks.rotate_tokens(acct.name) is True
    assert await _table_version(acct) == start + 1

    @asynccontextmanager
    async def unreachable():
        raise ConnectionRefusedError("synthetic PostgreSQL outage")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "acquire", unreachable)
    with pytest.raises(Exception):
        await playbooks.rotate_tokens(acct.name)


# ── 13: the counter cannot be reset by re-using an account's name ────────────

async def test_13_delete_then_recreate_and_rename_cannot_reset_the_counter(acct):
    old = await _signed_out_everywhere(acct)
    # Delete-then-recreate at the same name: the row cannot be deleted while
    # its counter exists, so the name's id keeps its counter.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await acct.admin.execute("DELETE FROM users WHERE id = $1", acct.user_id)
    assert not await _access_ok(old.access_token)
    # A rename makes a new account with its own counter at 0; tokens from
    # before carry the old account and meet the old counter.
    renamed = f"{acct.name}-renamed"
    renamed_id = private_user_id(renamed)
    await acct.admin.execute(
        "INSERT INTO users (id, display_name, auth_provider, external_subject) "
        "VALUES ($1, $2, 'password', $2)", renamed_id, renamed)
    try:
        assert not await _access_ok(old.access_token)
        assert (await _refresh(acct, old.refresh_token)).status_code == 401
    finally:
        await acct.admin.execute("DELETE FROM auth_token_versions WHERE user_id = $1", renamed_id)
        await acct.admin.execute("DELETE FROM users WHERE id = $1", renamed_id)


# ── 14: erasure ──────────────────────────────────────────────────────────────

async def test_14_erasure_bumps_sets_deleted_at_and_the_row_is_purged_after_seven_days(acct):
    """The account-scope step of `erase_postgres`, and the purge, on the real table."""
    from core.identity import token_versions

    old = await _login(acct)
    # The erasure step itself: raise the version, set users.deleted_at.
    await token_versions.bump(acct.user_id)
    await acct.admin.execute(
        "UPDATE users SET deleted_at = COALESCE(deleted_at, NOW()) WHERE id = $1", acct.user_id)
    assert not await _access_ok(old.access_token)
    assert await acct.admin.fetchval("SELECT deleted_at IS NOT NULL FROM users WHERE id = $1",
                                     acct.user_id)
    # The purge every start runs, as ira_app (acct's pool). The worker's daily
    # job runs the same statement; test 20 runs it as ira_worker.
    for days, present in ((0, True), (6, True), (8, False)):
        await acct.admin.execute(
            "UPDATE users SET deleted_at = now() - make_interval(days => $2) WHERE id = $1",
            acct.user_id, days)
        await _backdate_counter(acct, days)
        if present:
            # The policy, not only the purge's own WHERE clause, refuses: a
            # plain DELETE by either role removes nothing under seven days.
            for role in ("ira_app", "ira_worker"):
                status = await _as(acct.admin, role,
                                   "DELETE FROM auth_token_versions WHERE user_id = $1",
                                   acct.user_id)
                assert status == "DELETE 0", (days, role, status)
        purged = await token_versions.purge_erased()
        assert (await _table_version(acct) is not None) is present, f"day {days}"
        if not present:
            assert purged >= 1, purged


async def _backdate_counter(acct, days: int, user_id=None) -> None:
    """Set the counter's `updated_at` back, as `days` of stillness would.

    The trigger sets `updated_at` on every UPDATE, for every role, which is the
    point: no role can backdate it. A test can, by skipping triggers for one
    transaction as the superuser (`session_replication_role = replica`).
    """
    async with acct.admin.transaction():
        await acct.admin.execute("SET LOCAL session_replication_role = replica")
        await acct.admin.execute(
            "UPDATE auth_token_versions SET updated_at = now() - make_interval(days => $2) "
            "WHERE user_id = $1", user_id or acct.user_id, days)


async def _as_commit(conn, role: str, sql: str):
    async with conn.transaction():
        await conn.execute(f"SET LOCAL ROLE {role}")
        return await conn.execute(sql)


async def test_14b_erase_postgres_runs_the_step_for_the_account_scope():
    """The step is wired into `erase_postgres`, after every other pass, for `account` only."""
    source = (pathlib.Path(__file__).resolve().parents[2] / "core" / "privacy" / "erasure.py"
              ).read_text(encoding="utf-8")
    body = source[source.index("async def erase_postgres"):source.index("async def _erase_checkpoints")]
    # `erase_postgres` has more than one `account`-only block (the care push-token
    # pass inside the per-workspace loop came later), so the step is located by
    # the bump itself: the nearest `account` guard before it.
    bump = body.index("token_versions.bump(principal.user_id)")
    step = body.rindex('if scope == "account":', 0, bump)
    assert "deleted_at = COALESCE(deleted_at, NOW())" in body[step:]
    assert body.index("_erase_checkpoints(principal)") < step
    assert body.count("token_versions.bump(principal.user_id)") == 1


async def test_14c_every_start_purges_a_counter_past_its_retention(acct):
    """The start-up step every install runs (`app.lifespan._seed_token_versions`)
    purges, as ira_app; the daily worker job runs only where a worker does."""
    from app.lifespan import _seed_token_versions
    from core.identity import token_versions

    await _login(acct)
    await token_versions.bump(acct.user_id)
    await acct.admin.execute(
        "UPDATE users SET deleted_at = now() - interval '8 days' WHERE id = $1", acct.user_id)
    await _backdate_counter(acct, 6)
    await _seed_token_versions()
    assert await _table_version(acct) == 1, "a counter six days still was purged"
    await _backdate_counter(acct, 8)
    await _seed_token_versions()
    assert await _table_version(acct) is None, "the start did not purge"


# ── 15: the migration and its rollback ───────────────────────────────────────

def _body(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    return re.sub(r"(?im)^COMMIT;\s*$", "", text)


async def _shape(conn) -> dict:
    return {
        "table": await conn.fetchval("SELECT to_regclass('public.auth_token_versions') IS NOT NULL"),
        "forced": await conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid = to_regclass('public.auth_token_versions')"),
        "policies": sorted(r["policyname"] for r in await conn.fetch(
            "SELECT policyname FROM pg_policies WHERE tablename = 'auth_token_versions'")),
        "trigger": await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'auth_token_versions_monotonic'"),
        "app": sorted(r["privilege_type"] for r in await conn.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'auth_token_versions' AND grantee = 'ira_app'")),
        "worker": sorted(r["privilege_type"] for r in await conn.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'auth_token_versions' AND grantee = 'ira_worker'")),
    }


def test_15a_both_files_are_wrapped_in_a_transaction():
    for path in (MIGRATION, ROLLBACK):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"(?im)^BEGIN;\s*$", text) and re.search(r"(?im)^COMMIT;\s*$", text), path


async def test_15_096_applies_rolls_back_and_reapplies_to_the_same_shape(admin):
    expected = {
        "table": True, "forced": True, "trigger": 1,
        "policies": ["auth_token_versions_bump", "auth_token_versions_create",
                     "auth_token_versions_purge", "auth_token_versions_purge_read",
                     "auth_token_versions_read"],
        "app": ["DELETE", "INSERT", "SELECT", "UPDATE"], "worker": ["DELETE", "SELECT"],
    }
    tx = admin.transaction()
    await tx.start()
    try:
        assert await _shape(admin) == expected            # the chain applied it
        await admin.execute("SET LOCAL ROLE ira_schema_owner")
        await admin.execute(_body(ROLLBACK))
        await admin.execute("RESET ROLE")
        gone = await _shape(admin)
        assert gone["table"] is False and gone["policies"] == [] and gone["trigger"] == 0
        await admin.execute("SET LOCAL ROLE ira_schema_owner")
        await admin.execute(_body(MIGRATION))
        await admin.execute(_body(MIGRATION))             # idempotent
        await admin.execute("RESET ROLE")
        assert await _shape(admin) == expected
    finally:
        await tx.rollback()


# ── 16: legacy delegated accounts ────────────────────────────────────────────

async def test_16_a_legacy_delegated_account_is_refused_until_seeded_then_has_a_counter(
        acct, monkeypatch):
    from core.identity import token_versions

    legacy = f"legacy-{uuid.uuid4().hex[:8]}"
    legacy_id = private_user_id(legacy)
    # A pre-040 row: no `user_id`, so no `users` row and no counter. The table
    # is closed to new rows by a trigger, so one is planted with triggers off
    # for this one statement, as a row from before the trigger would exist.
    async with acct.admin.transaction():
        await acct.admin.execute("SET LOCAL session_replication_role = replica")
        await acct.admin.execute(
            "INSERT INTO delegated_users (id, tenant_id, username, password_hash) "
            "VALUES ($1, $2, $3, 'x')", uuid.uuid4(), PRIVATE_TENANT_ID, legacy)
    try:
        token = auth._make_access_token(legacy, ver=0)[0]
        with pytest.raises(Exception) as caught:
            await auth._get_token_version(legacy)
        assert getattr(caught.value, "status_code", None) == 401, \
            "an account with no users row must be refused, not passed"
        report = await token_versions.seed_from_redis()
        assert report.legacy_rows_created >= 1
        assert await acct.admin.fetchval("SELECT count(*) FROM users WHERE id = $1", legacy_id) == 1
        assert await auth._get_token_version(legacy) == 0
        assert await token_versions.current_for_login(legacy_id) == 0
        assert await acct.admin.fetchval(
            "SELECT version FROM auth_token_versions WHERE user_id = $1", legacy_id) == 0
        del token
    finally:
        await acct.admin.execute("DELETE FROM delegated_users WHERE username = $1", legacy)
        await acct.admin.execute("DELETE FROM auth_token_versions WHERE user_id = $1", legacy_id)
        await acct.admin.execute("DELETE FROM users WHERE id = $1", legacy_id)


# ── 17: only a current access token may sign out everywhere ──────────────────

async def test_17_logout_all_needs_a_current_access_token(acct):
    tokens = await _login(acct)
    start = await _table_version(acct)
    r = await _logout_all(acct, tokens.refresh_token)
    assert r.status_code == 401, r.text
    assert await _table_version(acct) == start, "a refresh token bumped the version"
    assert (await _logout_all(acct, tokens.access_token)).status_code == 200
    bumped = await _table_version(acct)
    r = await _logout_all(acct, tokens.access_token)       # now below the version
    assert r.status_code == 401, r.text
    assert await _table_version(acct) == bumped, "a stale access token bumped the version"


# ── 18: each connection reaches only the account it is bound to ─────────────

async def test_18_a_connection_reaches_only_the_account_it_is_bound_to(acct):
    """The policies key on `app.user_id`, as `user_credentials` does.

    A statement that forgot its WHERE clause must still reach one account's
    counter, and an unbound connection none, so a bug in a query can neither
    read every account's counter nor sign every account out at once. The
    worker, which binds no account, sees only counters its purge may delete.
    """
    from core.identity import token_versions

    a = acct.admin
    other = private_user_id(f"{acct.name}-other")
    await a.execute("INSERT INTO users (id, display_name, auth_provider, external_subject) "
                    "VALUES ($1, 'other', 'password', 'other')", other)
    try:
        await token_versions.bump(acct.user_id)
        await token_versions.bump(other)
        # No WHERE on the account: only on which rows are this test's, because
        # the shared database may hold other tests' erased accounts, which the
        # purge policy lets ira_app see.
        ours = [acct.user_id, other]
        count = f"SELECT count(*) FROM auth_token_versions WHERE user_id = ANY('{{{ours[0]},{ours[1]}}}'::uuid[])"
        everyone = "UPDATE auth_token_versions SET version = version + 1"

        # Unbound: nothing is visible, and a WHERE-less bump raises nothing.
        assert await _as(a, "ira_app", count, fetch=True) == 0
        assert await _as(a, "ira_app", everyone) == "UPDATE 0"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _as(a, "ira_app", "INSERT INTO auth_token_versions (user_id, version) "
                                    "VALUES ($1, 5)", private_user_id(f"{acct.name}-x"))

        # Bound: exactly the bound account, whatever the statement says.
        assert await _as(a, "ira_app", count, fetch=True, bound=acct.user_id) == 1
        assert await _as(a, "ira_app", everyone, bound=acct.user_id) == "UPDATE 1"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await _as(a, "ira_app", "INSERT INTO auth_token_versions (user_id, version) "
                                    "VALUES ($1, 5) ON CONFLICT (user_id) DO NOTHING",
                      private_user_id(f"{acct.name}-x"), bound=acct.user_id)

        # The worker: neither counter belongs to an account erased a week ago.
        assert await _as(a, "ira_worker", count, fetch=True) == 0

        # The read on an unbound connection: the counter (1) is hidden, so it
        # reads as missing, which would mean 0. The read refuses to answer.
        tx = a.transaction()
        await tx.start()
        try:
            await a.execute("SET LOCAL ROLE ira_app")
            row = await a.fetchrow(token_versions._CURRENT_SQL, acct.user_id)
        finally:
            await tx.rollback()
        assert row is not None and row["version"] is None
        with pytest.raises(token_versions.TokenVersionUnavailable):
            token_versions._version_of(row, acct.user_id)

        # Nothing above was committed, and the real store still reads both.
        assert await token_versions.current(acct.user_id) == 1
        assert await token_versions.current(other) == 1
    finally:
        await a.execute("DELETE FROM auth_token_versions WHERE user_id = $1", other)
        await a.execute("DELETE FROM users WHERE id = $1", other)


# ── 19: the rollback helper copies every counter back to Redis ─────────────

async def test_19_the_rollback_helper_copies_every_counter_and_never_lowers_one(acct):
    """`scripts/token_versions_to_redis.py`, run as the application role.

    No IRA role bypasses row-level security, so a helper that read the table
    unbound would see nothing, copy nothing and report success, and the
    rollback after it would lose every counter.
    """
    import importlib.util

    from core.identity import token_versions

    spec = importlib.util.spec_from_file_location(
        "token_versions_to_redis", POSTGRES.parent / "scripts" / "token_versions_to_redis.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    a = acct.admin
    other_name = f"{acct.name}-second"
    other = private_user_id(other_name)
    await a.execute("INSERT INTO users (id, display_name, auth_provider, external_subject) "
                    "VALUES ($1, $2, 'password', $2)", other, other_name)
    try:
        for _ in range(3):
            await token_versions.bump(acct.user_id)
        await token_versions.bump(other)
        await acct.cache.set(VER_KEY + other_name, 9)        # Redis already higher

        conn = await asyncpg.connect(DSN)                     # ira_app, as the app runs
        try:
            rows = await helper.read_counters(conn)
        finally:
            await conn.close()
        # The shared test database holds other tests' accounts too; these two
        # are this test's.
        ours = [r for r in rows if r["user_id"] in (acct.user_id, other)]
        assert {r["external_subject"]: r["version"] for r in ours} == {
            acct.name: 3, other_name: 1}, ours

        assert await helper.copy_to_redis(ours, acct.cache, cloud=False, dry_run=True) == 0
        assert await acct.cache.get(VER_KEY + acct.name) is None     # a dry run writes nothing
        assert await helper.copy_to_redis(ours, acct.cache, cloud=False, dry_run=False) == 0
        assert await acct.cache.get(VER_KEY + acct.name) == "3"
        assert await acct.cache.get(VER_KEY + other_name) == "9"     # never lowered
    finally:
        await acct.cache.delete(VER_KEY + other_name)
        await a.execute("DELETE FROM auth_token_versions WHERE user_id = $1", other)
        await a.execute("DELETE FROM users WHERE id = $1", other)


# ── 20: a backdated erasure cannot purge a counter that moved recently ─────

async def test_20_only_a_counter_still_for_seven_days_can_be_purged(acct):
    """The purge resets a counter to 0, which is safe only once every token it
    refused has expired. `users.deleted_at` can be written by ira_app, so it
    alone is not enough: anyone who could backdate it could purge a counter
    the day after "log out everywhere" and revive the tokens it refused. The
    counter's own `updated_at` is set by the trigger on every UPDATE, for every
    role, so the policy requires it to be seven days old as well.
    """
    from core.identity import token_versions

    old = await _signed_out_everywhere(acct)              # counter 1, moved just now
    a = acct.admin
    for role in ("ira_app", "ira_worker"):
        tx = a.transaction()
        await tx.start()
        try:
            if role == "ira_app":
                await a.execute("SET LOCAL ROLE ira_app")
                # What ira_app can do: backdate the erasure, and try to backdate
                # the counter (the trigger sets it back to now).
                await a.execute("UPDATE users SET deleted_at = now() - interval '30 days' "
                                "WHERE id = $1", acct.user_id)
                await a.execute("SELECT set_config('app.user_id', $1, true)", str(acct.user_id))
                await a.execute("UPDATE auth_token_versions SET updated_at = now() - "
                                "interval '30 days' WHERE user_id = $1", acct.user_id)
                await a.execute("SELECT set_config('app.user_id', '', true)")
            else:
                # The worker cannot write `users`; the backdate is done for it.
                await a.execute("UPDATE users SET deleted_at = now() - interval '30 days' "
                                "WHERE id = $1", acct.user_id)
                await a.execute("SET LOCAL ROLE ira_worker")
            # An attacker's DELETE, not the purge's own statement (which
            # checks updated_at itself): only the policy stands in the way.
            status = await a.execute("DELETE FROM auth_token_versions WHERE user_id = $1",
                                     acct.user_id)
            assert status == "DELETE 0", (role, status)
        finally:
            await tx.rollback()
    assert await _table_version(acct) == 1
    assert not await _access_ok(old.access_token)

    # Both clocks past seven days: the worker's daily job purges it.
    await a.execute("UPDATE users SET deleted_at = now() - interval '8 days' WHERE id = $1",
                    acct.user_id)
    await _backdate_counter(acct, 8)
    assert await _as_commit(a, "ira_worker", token_versions._PURGE_SQL) != "DELETE 0"
    assert await _table_version(acct) is None

