"""The deployment's nightly backup (deploy/backup.sh) writes files only its owner can read (G2.a).

The round 10 runbook drill (scripts/ops/runbook_drills.py) ran deploy/backup.sh on a stock host
and found the encrypted dump and its checksum at 0644, readable by every account on the machine:
the script took the process umask. tests/test_backup_files_are_private.py covers the in-app
backups (worker/backup.py); this runs the deployment's own script against a real PostgreSQL, under
umask 022 on purpose (a stricter inherited umask would prove nothing), and checks the folder is
0700 and every file 0600, and that the file still restores (deploy/restore.sh).
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)
if not (shutil.which("pg_dump") and shutil.which("openssl") and shutil.which("psql")):
    pytest.skip("pg_dump, psql and openssl are needed", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[4]


def _umask_022(): os.umask(0o022)


def test_the_deployment_backup_is_private_and_still_restores(tmp_path):
    where = tmp_path / "backups"
    env = {**os.environ, "BACKUP_PASSPHRASE": secrets.token_urlsafe(24), "BACKUP_DIR": str(where), "BACKUP_DSN": ADMIN_DSN}
    r = subprocess.run(["sh", str(ROOT / "deploy" / "backup.sh")], env=env, capture_output=True, text=True,
                       preexec_fn=_umask_022, timeout=600)
    if r.returncode == 4:
        pytest.skip(f"this machine's pg_dump is newer than the server: {r.stderr.strip()}")
    assert r.returncode == 0, r.stderr
    files = sorted(where.glob("ira-care-*"))
    assert len(files) == 2, files   # the dump and its checksum
    assert stat.S_IMODE(where.stat().st_mode) == 0o700, oct(stat.S_IMODE(where.stat().st_mode))
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, (f.name, oct(stat.S_IMODE(f.stat().st_mode)))

    target = f"ira_backup_private_{uuid.uuid4().hex[:8]}"
    base = ADMIN_DSN.rsplit("/", 1)[0]
    subprocess.run(["psql", ADMIN_DSN, "-qc", f"CREATE DATABASE {target}"], check=True, capture_output=True)
    try:
        dump = next(f for f in files if f.name.endswith(".enc"))
        r = subprocess.run(["sh", str(ROOT / "deploy" / "restore.sh"), str(dump), f"{base}/{target}"], env=env,
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        restored = subprocess.run(["psql", f"{base}/{target}", "-Atc", "SELECT count(*) FROM schema_migrations"],
                                  capture_output=True, text=True).stdout.strip()
        original = subprocess.run(["psql", ADMIN_DSN, "-Atc", "SELECT count(*) FROM schema_migrations"],
                                  capture_output=True, text=True).stdout.strip()
        assert restored == original and int(original) > 100, (restored, original)
    finally:
        subprocess.run(["psql", ADMIN_DSN, "-qc", f"DROP DATABASE IF EXISTS {target}"], capture_output=True)
