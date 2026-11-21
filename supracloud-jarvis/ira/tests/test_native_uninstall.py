"""scripts/uninstall-native.sh: changes nothing unless told to, and never without a backup.

Each test runs the real script against a throwaway copy of an install's layout
(.env, .venv, .ira-run, frontend build) in a temporary folder. The .env points
PostgreSQL at port 1, where nothing listens, so no database is ever touched and
the backup step fails the way it would on a broken machine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = IRA_ROOT.parent                      # supracloud-jarvis/
UNINSTALL = APP_ROOT / "scripts" / "uninstall-native.sh"
BACKUP = APP_ROOT / "scripts" / "ira-backup.sh"

LOCAL_STATE = [".venv", ".ira-run", "frontend/node_modules", "frontend/.next"]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def fake_install(tmp_path: Path) -> Path:
    app = tmp_path / "supracloud-jarvis"
    (app / "scripts").mkdir(parents=True)
    shutil.copy2(UNINSTALL, app / "scripts" / UNINSTALL.name)
    shutil.copy2(BACKUP, app / "scripts" / BACKUP.name)
    for rel in LOCAL_STATE:
        (app / rel).mkdir(parents=True)
        (app / rel / "keep.txt").write_text("synthetic\n")
    (app / ".env").write_text(
        "POSTGRES_DB=ira_uninstall_test\n"
        "POSTGRES_PORT=1\n"                      # nothing listens here
        f"BACKUP_DIR={tmp_path / 'backups'}\n"
        "OLLAMA_MODEL_DEEP=qwen3:4b\n"
    )
    (app / ".env.backup-20261019").write_text("an older .env a restore kept\n")
    return app


def fake_psql(tmp_path: Path, reachable: bool) -> Path:
    """A `psql` first on PATH. Reachable: it answers `SELECT 1` and finds no IRA
    database. Unreachable: every call fails, as `sudo -n` does without cached sudo."""
    bin_dir = tmp_path / ("bin-up" if reachable else "bin-down")
    bin_dir.mkdir()
    body = ('case "$*" in *"FROM pg_database"*) exit 0;; *"SELECT 1"*) echo 1; exit 0;; esac\nexit 1\n'
            if reachable else "exit 2\n")
    (bin_dir / "psql").write_text("#!/bin/sh\n" + body)
    (bin_dir / "psql").chmod(0o755)
    return bin_dir


def run(app: Path, *args: str, path_first: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(PG_SUPERUSER="ira_uninstall_test_nobody", PGCONNECT_TIMEOUT="2", HOME=str(app.parent))
    if path_first is not None:
        env["PATH"] = f"{path_first}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(["bash", str(app / "scripts" / UNINSTALL.name), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def everything_still_there(app: Path) -> None:
    for rel in [*LOCAL_STATE, ".env", ".env.backup-20261019"]:
        assert (app / rel).exists(), f"{rel} was removed"


def test_the_script_parses_and_is_executable() -> None:
    assert subprocess.run(["bash", "-n", str(UNINSTALL)]).returncode == 0
    assert os.access(UNINSTALL, os.X_OK)


def test_without_yes_it_is_a_dry_run_that_lists_what_would_go(fake_install: Path) -> None:
    result = run(fake_install)
    assert result.returncode == 0, result.stdout + result.stderr
    for rel in [*LOCAL_STATE, ".env"]:
        assert f"remove {rel}" in result.stdout
    assert "drop database ira_uninstall_test" in result.stdout
    assert "keeping .env.backup-20261019" in result.stdout
    assert "nothing was changed" in result.stdout
    everything_still_there(fake_install)


def test_a_failed_backup_removes_nothing(fake_install: Path, tmp_path: Path) -> None:
    # PostgreSQL reachable, so the run gets as far as the backup, which fails.
    result = run(fake_install, "--yes", path_first=fake_psql(tmp_path, reachable=True))
    assert result.returncode != 0
    assert "backup failed" in result.stdout and "nothing was removed" in result.stdout
    everything_still_there(fake_install)


def test_an_unreachable_database_stops_the_run_before_anything_is_removed(
        fake_install: Path, tmp_path: Path) -> None:
    """It used to read "absent", skip the drop, remove .env and say "uninstalled"."""
    down = fake_psql(tmp_path, reachable=False)
    dry = run(fake_install, path_first=down)
    assert "not reachable" in dry.stdout and "will stop before removing anything" in dry.stdout
    result = run(fake_install, "--yes", path_first=down)
    assert result.returncode != 0
    assert "cannot tell whether database ira_uninstall_test exists" in result.stdout
    assert "nothing was removed" in result.stdout
    assert "IRA is uninstalled" not in result.stdout
    everything_still_there(fake_install)


def test_opt_in_extras_are_only_planned_when_asked(fake_install: Path) -> None:
    plain = run(fake_install).stdout
    assert "drop roles" not in plain and "remove chat model" not in plain
    extras = run(fake_install, "--remove-roles", "--remove-model").stdout
    assert "drop roles ira_app ira_worker ira_migrator ira_schema_owner" in extras
    assert "remove chat model qwen3:4b" in extras
    everything_still_there(fake_install)


def test_an_unknown_option_is_refused_before_anything_happens(fake_install: Path) -> None:
    result = run(fake_install, "--yes", "--force")
    assert result.returncode != 0
    assert "unknown option: --force" in result.stdout
    everything_still_there(fake_install)


def test_backups_and_shared_tools_are_never_targets() -> None:
    text = UNINSTALL.read_text(encoding="utf-8")
    removed = text.split('LOCAL_PATHS="', 1)[1].split('"', 1)[0].split()
    assert removed == [".venv", ".ira-run", "frontend/node_modules", "frontend/.next", ".env"]
    for line in text.splitlines():
        if "rm -rf" in line and not line.lstrip().startswith("#"):
            assert "BACKUP_DIR" not in line and "$HOME" not in line, line
