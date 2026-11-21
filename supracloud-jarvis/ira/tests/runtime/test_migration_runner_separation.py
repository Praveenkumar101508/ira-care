"""Phase 3B-3.5 — the application does not apply migrations, and cannot.

The old `run_migrations(pool)` ran on every boot through the ordinary
connection pool. That one call is why the runtime role owned every table: DDL
requires ownership, so the role serving requests also held the authority to
disable forced row-level security, drop policies and reassign ownership.

These tests assert the split holds in both directions — the application refuses
to apply, and the migration command refuses the wrong caller.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[2]


# ── The application cannot apply migrations ──────────────────────────────────

async def test_run_migrations_refuses_rather_than_disappearing():
    """Kept as a loud failure instead of deleted.

    Deleting it would give a stale caller an ImportError naming a module rather
    than a reason, and the shortest fix for an ImportError is to put the
    function back.
    """
    from utils.migrations import MigrationRefused, run_migrations

    with pytest.raises(MigrationRefused) as exc:
        await run_migrations(object())
    message = str(exc.value)
    assert "must not apply migrations" in message
    assert "scripts.migrate" in message, "the refusal must name the replacement"


def test_the_application_startup_path_does_not_apply_migrations():
    """Parsed, not grepped: a comment mentioning `run_migrations` would defeat a
    string search, and re-adding the call is the single change that would undo
    this phase."""
    source = (IRA_ROOT / "app" / "lifespan.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "run_migrations" not in called, (
        "app/lifespan.py calls run_migrations again. The application applying "
        "its own migrations is what forced the runtime role to own every table "
        "(D-18, R-50)."
    )
    assert "migration_status" in called, (
        "startup no longer inspects migration state at all — it must still "
        "report a pending schema rather than starting silently"
    )


def test_readiness_fails_when_migrations_are_pending(monkeypatch):
    """The replacement for auto-applying: an instance that cannot fix the schema
    must take itself out of the load balancer instead of serving against it."""
    import asyncio

    from core.runtime import readiness

    class _Pool:
        async def fetchval(self, *_a, **_k):
            return True

    async def _pending(_conn):
        return {"total": 3, "applied": 1, "pending": ["041_x.sql", "042_y.sql"],
                "up_to_date": False}

    monkeypatch.setattr("utils.db.get_pool", lambda: _Pool())
    monkeypatch.setattr("utils.migrations.migration_status", _pending)

    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        readiness.check_migrations(required=True))
    assert result.status is readiness.CheckStatus.FAILED
    assert result.blocks_readiness, "a pending migration must block readiness"


# ── The migration command refuses the wrong caller ───────────────────────────

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

pytestmark_db = pytest.mark.skipif(
    not DSN or not ADMIN_DSN, reason="database DSNs not set")


def _dsn_as(role: str) -> str:
    scheme, rest = DSN.split("://", 1)
    _, tail = rest.split("@", 1) if "@" in rest else ("", rest)
    return f"{scheme}://{role}@{tail}"


@pytestmark_db
@pytest.mark.parametrize("role", ["ira_app", "ira_worker"])
async def test_the_migration_command_refuses_a_runtime_role(role):
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import MigrationRefused, assert_may_apply

    try:
        conn = await asyncpg.connect(_dsn_as(role))
    except Exception:  # noqa: BLE001
        pytest.skip(f"cannot connect as {role}")
    try:
        with pytest.raises(MigrationRefused) as exc:
            await assert_may_apply(conn, authorised_for_production=True)
        assert role in str(exc.value)
        assert "D-18" in str(exc.value), "the refusal should say why, not just no"
    finally:
        await conn.close()


@pytestmark_db
async def test_the_migration_command_refuses_a_superuser():
    """A superuser bypasses row-level security, so a backfill behaves
    differently under it than in production — which is precisely how two
    defects survived a phase of 'verified' migrations."""
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import MigrationRefused, assert_may_apply

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        is_super = await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not is_super:
            pytest.skip("the admin DSN is not a superuser on this database")
        with pytest.raises(MigrationRefused) as exc:
            await assert_may_apply(conn, authorised_for_production=True)
        assert "SUPERUSER" in str(exc.value)
    finally:
        await conn.close()


@pytestmark_db
async def test_the_migration_command_accepts_the_migrator():
    """The positive control. Without it, every refusal above could be explained
    by the check refusing everybody."""
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import assert_may_apply

    try:
        conn = await asyncpg.connect(_dsn_as("ira_migrator"))
    except Exception:  # noqa: BLE001
        pytest.skip("cannot connect as ira_migrator")
    try:
        identity = await assert_may_apply(conn, authorised_for_production=False)
        assert identity["role"] == "ira_migrator"
    finally:
        await conn.close()


@pytestmark_db
async def test_production_requires_explicit_authorisation(monkeypatch):
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import MigrationRefused, assert_may_apply

    try:
        conn = await asyncpg.connect(_dsn_as("ira_migrator"))
    except Exception:  # noqa: BLE001
        pytest.skip("cannot connect as ira_migrator")
    try:
        from core.runtime.environment import Environment
        monkeypatch.setattr("core.runtime.environment.get_environment",
                            lambda *a, **k: Environment.PRODUCTION)
        with pytest.raises(MigrationRefused) as exc:
            await assert_may_apply(conn, authorised_for_production=False)
        assert "authorised" in str(exc.value)
    finally:
        await conn.close()


@pytestmark_db
async def test_production_refuses_an_unexpected_database(monkeypatch):
    """The cheapest possible guard against migrating a database that merely has
    a similar name to the intended one."""
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import MigrationRefused, assert_may_apply

    try:
        conn = await asyncpg.connect(_dsn_as("ira_migrator"))
    except Exception:  # noqa: BLE001
        pytest.skip("cannot connect as ira_migrator")
    try:
        from core.runtime.environment import Environment
        monkeypatch.setattr("core.runtime.environment.get_environment",
                            lambda *a, **k: Environment.PRODUCTION)
        monkeypatch.setenv("IRA_MIGRATION_DATABASE", "some_other_database")
        with pytest.raises(MigrationRefused) as exc:
            await assert_may_apply(conn, authorised_for_production=True)
        assert "similar name" in str(exc.value)
    finally:
        await conn.close()


@pytestmark_db
async def test_a_second_migration_process_is_refused_the_lock():
    """Two rollouts at once would interleave DDL. The second must fail fast."""
    asyncpg = pytest.importorskip("asyncpg")
    from utils.migrations import MIGRATION_LOCK_KEY

    try:
        first = await asyncpg.connect(_dsn_as("ira_migrator"))
        second = await asyncpg.connect(_dsn_as("ira_migrator"))
    except Exception:  # noqa: BLE001
        pytest.skip("cannot connect as ira_migrator")
    try:
        assert await first.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY)
        assert not await second.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY), (
            "a second migration process acquired the lock while the first held it")
    finally:
        await first.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        await first.close()
        await second.close()


# ── The command never leaks a credential ─────────────────────────────────────

def test_the_migration_command_redacts_dsn_passwords():
    import sys

    sys.path.insert(0, str(IRA_ROOT / "scripts"))
    from migrate import redact_dsn

    dsn = "postgresql://ira_migrator:" + "hunter2" + "@db.internal:5432/ira"
    out = redact_dsn(dsn)
    assert "hunter2" not in out
    assert "ira_migrator" in out and "db.internal" in out, (
        "redaction removed more than the credential — the host and role are what "
        "make the output useful for diagnosing a wrong-target run")
