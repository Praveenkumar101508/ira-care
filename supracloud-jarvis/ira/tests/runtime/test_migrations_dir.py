"""The migrator finds its files wherever the deployment puts them, and never calls an
empty database up to date.

Found by running the deployment image for the first time (docs/evidence/staging/CONTAINERS.md):
the image copies the migration files to /app/postgres-migrations, but utils/migrations.py
looked beside the repository (/postgres in the image), found nothing, printed "nothing to
apply" and let the server start on an empty database. Every care request then failed."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from utils import migrations

ROOT = Path(__file__).resolve().parents[4]


class _Conn:
    """Records every statement; answers 'no schema_migrations table yet'."""

    def __init__(self):
        self.calls = []

    async def fetchval(self, sql, *a):
        self.calls.append(sql)
        return False

    async def fetch(self, sql, *a):
        self.calls.append(sql)
        return []

    async def execute(self, sql, *a):
        self.calls.append(sql)


def test_the_directory_comes_from_the_setting_and_init_runs_first(tmp_path, monkeypatch):
    for name in ("010_b.sql", "init.sql", "002_a.sql", "notes.txt"):
        (tmp_path / name).write_text("SELECT 1;")
    monkeypatch.setenv("IRA_MIGRATIONS_DIR", str(tmp_path))
    assert migrations.migrations_dir() == tmp_path.resolve()
    assert [p.name for p in migrations.migration_files()] == ["init.sql", "002_a.sql", "010_b.sql"]


def test_without_the_setting_it_is_the_repositorys_directory(monkeypatch):
    monkeypatch.delenv("IRA_MIGRATIONS_DIR", raising=False)
    assert migrations.migrations_dir() == (ROOT / "supracloud-jarvis/postgres").resolve()
    assert any(p.name == "init.sql" for p in migrations.migration_files())


@pytest.mark.asyncio
async def test_no_files_is_refused_before_anything_is_touched(tmp_path, monkeypatch):
    monkeypatch.setenv("IRA_MIGRATIONS_DIR", str(tmp_path / "not-here"))

    async def allowed(conn, **kw):
        return {"role": "ira_migrator", "database": "x", "environment": "staging"}

    monkeypatch.setattr(migrations, "assert_may_apply", allowed)
    conn = _Conn()
    with pytest.raises(migrations.MigrationRefused, match="no migration files found"):
        await migrations.apply_migrations(conn, authorised_for_production=True)
    assert conn.calls == [], "nothing was locked, created or applied"


@pytest.mark.asyncio
async def test_an_empty_directory_is_never_reported_as_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setenv("IRA_MIGRATIONS_DIR", str(tmp_path))
    status = await migrations.migration_status(_Conn())
    assert status["total"] == 0 and status["up_to_date"] is False


def test_the_image_points_the_migrator_at_the_files_it_copies():
    dockerfile = (ROOT / "deploy/Dockerfile.api").read_text()
    copied = re.search(r"^COPY supracloud-jarvis/postgres (\S+)$", dockerfile, re.M)
    setting = re.search(r"^ENV IRA_MIGRATIONS_DIR=(\S+)$", dockerfile, re.M)
    assert copied and setting and copied.group(1) == setting.group(1)
