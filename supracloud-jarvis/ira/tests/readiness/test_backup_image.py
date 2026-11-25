"""Backups cannot silently stop (REMEDIATION G4).

The backup container used to install openssl from the Alpine mirror each time it started and
ignore a failure: with no network at start, every nightly backup failed and nothing said so.
openssl is now installed when the image is built, the scripts refuse at once without it, and
a failed backup ends the container's loop so the restart shows."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "deploy"


def _backup_service() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]["backup"]


def test_the_backup_service_is_built_with_openssl_not_installed_at_start():
    svc = _backup_service()
    assert svc.get("build", {}).get("dockerfile") == "deploy/Dockerfile.backup"
    runtime = " ".join(str(svc.get(k, "")) for k in ("command", "entrypoint"))
    assert "apk add" not in runtime and "apt-get" not in runtime, "nothing is installed when the container starts"
    dockerfile = (DEPLOY / "Dockerfile.backup").read_text()
    assert any(line.startswith("RUN") and "apk add" in line and "openssl" in line for line in dockerfile.splitlines())
    assert "|| exit 1" in dockerfile, "a failed backup ends the loop, so the restart is visible"


def _run_without_openssl(script: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    empty = tmp_path / "bin"
    empty.mkdir()
    out = tmp_path / "backups"
    env = {"PATH": str(empty), "BACKUP_PASSPHRASE": "test-only", "BACKUP_DIR": str(out), "PGDATABASE": "x"}
    return subprocess.run(["/bin/sh", str(DEPLOY / script), *args], env=env, capture_output=True, text=True, timeout=30)


def test_backup_refuses_at_once_without_openssl_and_writes_nothing(tmp_path):
    r = _run_without_openssl("backup.sh", tmp_path)
    assert r.returncode == 1
    assert "openssl not found" in r.stderr
    assert not (tmp_path / "backups").exists(), "not even the backup directory is created"


def test_restore_refuses_at_once_without_openssl(tmp_path):
    r = _run_without_openssl("restore.sh", tmp_path, str(tmp_path / "none.enc"), "postgresql://unused")
    assert r.returncode == 1
    assert "openssl not found" in r.stderr
