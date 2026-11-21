"""
In-app backups and the restore's temporary SQL are private to the service account.

`worker/backup.py` writes the nightly dump, the on-demand dump
(`POST /backup/create`), the pre-restore snapshot and the restore's decompressed
SQL; `api/routes/backup.py` writes the uploaded archive. Every one of them is a
plain `pg_dump` of the whole database, and until this test existed they were
created with the process umask: 0644 in a 0755 directory on a native install
(`$HOME/.ira/backups`), so any other account on the host could read them.
`scripts/ira-backup.sh` already does the right thing (`umask 077`, `chmod 600`,
`chmod 700`); this makes the in-app path match it.

The tests run under umask 022 on purpose: that is the default on the hosts
where the defect shows, and a test that inherits a stricter umask proves
nothing. `tests/conftest.py` stubs `worker.backup`, so the real module is loaded
from its file, the same way conftest itself loads `worker/notifier.py`.

Negative controls (each planted and watched fail before this file was trusted):
drop `opener=` from the dump write → `test_nightly_dump_is_private` sees 0644;
drop the `chmod` from `ensure_private_dir` → `test_an_existing_open_directory_is_tightened`
sees 0755; drop `opener=` from the restore's temp file → the psql-time check
sees 0644; drop it from the upload → the route test sees 0644.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import io
import os
import pathlib
import stat
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits; Windows ACLs are a separate check")

_IRA = pathlib.Path(__file__).resolve().parents[1]


def _load_real_backup_module():
    spec = importlib.util.spec_from_file_location(
        "worker._backup_real", _IRA / "worker" / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class _Settings:
    def __init__(self, backup_dir: pathlib.Path):
        self.backup_dir = str(backup_dir)
        self.backup_keep = 7
        self.postgres_host = "localhost"
        self.postgres_port = 5432
        self.postgres_user = "ira"
        self.postgres_db = "ira"
        self.postgres_password = "not-a-real-password"


class _FakeProc:
    """Stands in for pg_dump / psql: succeeds, and can look at the files it was given."""

    def __init__(self, argv, stdout=b"", on_run=None):
        self.argv = argv
        self.returncode = 0
        self._stdout = stdout
        self._on_run = on_run

    async def communicate(self):
        if self._on_run is not None:
            self._on_run(self.argv)
        return self._stdout, b""


def _fake_exec(stdout=b"", on_run=None):
    async def fake(*argv, **_kw):
        return _FakeProc(argv, stdout=stdout, on_run=on_run)
    return fake


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


# ── the helpers themselves ────────────────────────────────────────────────────

def test_private_opener_creates_0600_and_tightens_an_existing_file(tmp_path, umask_022):
    from utils.private_files import private_opener

    fresh = tmp_path / "fresh"
    with open(fresh, "wb", opener=private_opener) as fh:
        fh.write(b"x")
    assert _mode(fresh) == 0o600

    existing = tmp_path / "existing"
    existing.write_bytes(b"old")
    os.chmod(existing, 0o644)
    with open(existing, "wb", opener=private_opener) as fh:
        fh.write(b"new")
    assert _mode(existing) == 0o600, "a file left over at 0644 must not stay that way"
    assert existing.read_bytes() == b"new"


def test_ensure_private_dir_creates_0700_and_is_idempotent(tmp_path, umask_022):
    from utils.private_files import ensure_private_dir

    d = tmp_path / "a" / "b"
    assert ensure_private_dir(d) == d
    assert d.is_dir() and _mode(d) == 0o700
    assert ensure_private_dir(d) == d and _mode(d) == 0o700


# ── worker/backup.py ──────────────────────────────────────────────────────────

def test_nightly_dump_is_private(tmp_path, monkeypatch, umask_022):
    mod = _load_real_backup_module()
    backup_dir = tmp_path / "backups"          # does not exist yet: the code creates it
    monkeypatch.setattr(mod, "get_settings", lambda: _Settings(backup_dir))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec(stdout=b"-- dump\n"))

    out = asyncio.run(mod.run_database_backup())

    assert out is not None and out.exists(), "the fake pg_dump succeeded; a file was expected"
    assert _mode(out) == 0o600, f"the dump is {oct(_mode(out))}: other accounts can read it"
    assert _mode(backup_dir) == 0o700, f"the backup directory is {oct(_mode(backup_dir))}"
    with gzip.open(out, "rb") as fh:
        assert fh.read() == b"-- dump\n", "the private write changed the archive's content"


def test_an_existing_open_directory_is_tightened(tmp_path, monkeypatch, umask_022):
    """Native installs already have `$HOME/.ira/backups` at 0755; the next dump fixes it."""
    mod = _load_real_backup_module()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    os.chmod(backup_dir, 0o755)
    monkeypatch.setattr(mod, "get_settings", lambda: _Settings(backup_dir))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec(stdout=b"-- dump\n"))

    assert asyncio.run(mod.run_database_backup()) is not None
    assert _mode(backup_dir) == 0o700


def test_restore_temp_sql_is_private_while_psql_reads_it(tmp_path, monkeypatch, umask_022):
    mod = _load_real_backup_module()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(mod, "get_settings", lambda: _Settings(backup_dir))

    archive = tmp_path / "in.sql.gz"
    with gzip.open(archive, "wb") as fh:
        fh.write(b"SELECT 1;\n")

    seen: dict = {}

    def psql_looks_at_its_input(argv):
        sql = pathlib.Path(argv[argv.index("-f") + 1])
        seen["mode"] = _mode(sql)
        seen["content"] = sql.read_bytes()

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec(on_run=psql_looks_at_its_input))

    ok, message = asyncio.run(mod.restore_from_file(archive))

    assert ok, message
    assert seen["content"] == b"SELECT 1;\n"
    assert seen["mode"] == 0o600, (
        f"the decompressed dump was {oct(seen['mode'])} for the whole psql run")
    assert not list(backup_dir.glob("restore_temp_*.sql")), "the temp SQL must be removed afterwards"


# ── api/routes/backup.py ──────────────────────────────────────────────────────

# The route checks lockdown first; with no Redis here the state reads UNKNOWN,
# which refuses a restore. This test is about the files, so lockdown is off.
@pytest.mark.usefixtures("lockdown_inactive")
def test_uploaded_archive_is_private(tmp_path, monkeypatch, umask_022):
    from fastapi import UploadFile

    import api.routes.backup as route

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(route, "get_settings", lambda: _Settings(backup_dir))

    seen: dict = {}

    async def restore_looks_at_the_upload(path):
        seen["mode"] = _mode(path)
        seen["content"] = path.read_bytes()
        return True, "ok"

    async def no_snapshot():
        return None

    monkeypatch.setattr(route, "restore_from_file", restore_looks_at_the_upload)
    monkeypatch.setattr(route, "run_database_backup", no_snapshot)
    # The password check is not the subject here (tests/test_destructive_action_
    # approvals.py is); the restore needs one since P0-03, so answer it.
    monkeypatch.setattr("security.delegated.verify_owner_password",
                        lambda password: password == "owner-password")

    payload = gzip.compress(b"SELECT 1;\n")

    async def stage_then_confirm():
        # Two requests since P0-03: the first stages and returns a token for
        # this archive, the second carries the same archive with that token.
        staged = await route.restore_backup(
            file=UploadFile(file=io.BytesIO(payload), filename="backup.sql.gz"),
            confirm_token=None, owner_password=None, _user="owner")
        assert staged["status"] == "confirmation_required"
        assert not backup_dir.exists() or not list(backup_dir.glob("restore_*")), (
            "staging writes nothing")
        return await route.restore_backup(
            file=UploadFile(file=io.BytesIO(payload), filename="backup.sql.gz"),
            confirm_token=staged["token"], owner_password="owner-password",
            _user="owner")

    result = asyncio.run(stage_then_confirm())

    assert result["status"] == "restored"
    assert seen["content"] == payload
    assert seen["mode"] == 0o600, f"the uploaded archive was {oct(seen['mode'])}"
    assert _mode(backup_dir) == 0o700
    assert not list(backup_dir.glob("restore_*.sql.gz")), "the upload must be removed afterwards"


@pytest.mark.usefixtures("lockdown_inactive")
def test_restore_route_still_refuses_a_corrupt_archive(tmp_path, monkeypatch, umask_022):
    """Found while writing the test above: the route used to open the temp file
    *before* writing it, so every upload — valid or not — was refused as corrupt
    and `POST /backup/restore` could never succeed. The in-memory check is the
    one that matters, and it stays."""
    from fastapi import HTTPException, UploadFile

    import api.routes.backup as route

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(route, "get_settings", lambda: _Settings(backup_dir))

    async def must_not_run(*_a):
        raise AssertionError("a corrupt archive reached the database")

    monkeypatch.setattr(route, "restore_from_file", must_not_run)
    monkeypatch.setattr(route, "run_database_backup", must_not_run)

    upload = UploadFile(file=io.BytesIO(b"this is not gzip"), filename="backup.sql.gz")
    with pytest.raises(HTTPException) as err:
        asyncio.run(route.restore_backup(file=upload, confirm_token=None,
                                         owner_password=None, _user="owner"))
    assert err.value.status_code == 400
    assert "corrupt" in err.value.detail
    assert not list(backup_dir.glob("restore_*.sql.gz")), "nothing is written for a bad upload"
