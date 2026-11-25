"""Off-site backup copies (register G2.h) and a checksum that travels with its file (REMEDIATION G5).

The real deploy scripts are run with stand-ins on PATH: `pg_dump` and `psql` that print a
tiny dump, and a `curl` that keeps objects in a local folder instead of a bucket. So this
needs no database and no network; the end-to-end drill against an S3 server is
scripts/staging/offsite_drill.sh, recorded in docs/evidence/staging/OFFSITE.md.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "deploy"

FAKE_CURL = r'''#!PYTHON
import os, sys, shutil, urllib.parse
store = os.environ["FAKE_S3_DIR"]
args = sys.argv[1:]
src = dst = None
url = args[-1]
for i, a in enumerate(args):
    if a == "-T": src = args[i + 1]
    if a == "-o": dst = args[i + 1]
sys.stdin.read()
path = urllib.parse.urlparse(url)
key = path.path.rsplit("/", 1)[-1]
if src:
    shutil.copyfile(src, os.path.join(store, key)); sys.exit(0)
if "list-type=2" in (path.query or ""):
    print("<ListBucketResult>" + "".join(f"<Key>{k}</Key>" for k in sorted(os.listdir(store))) + "</ListBucketResult>")
    sys.exit(0)
p = os.path.join(store, key)
if not os.path.exists(p):
    sys.exit(22)
shutil.copyfile(p, dst); sys.exit(0)
'''


def _tool(bin_dir: Path, name: str, body: str):
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path):
    if not shutil.which("openssl") or not shutil.which("sha256sum"):
        pytest.skip("needs openssl and sha256sum")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _tool(bin_dir, "psql", "#!/bin/sh\necho 160004\n")
    _tool(bin_dir, "pg_dump", "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'pg_dump (PostgreSQL) 16.4'; "
                              "else echo 'CREATE TABLE fake_table (id int);'; fi\n")
    _tool(bin_dir, "curl", FAKE_CURL.replace("#!PYTHON", "#!" + sys.executable))
    store = tmp_path / "bucket"
    store.mkdir()
    e = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "BACKUP_PASSPHRASE": "test-only-passphrase",
         "BACKUP_DIR": str(tmp_path / "backups"), "BACKUP_DSN": "postgresql://unused/fake",
         "FAKE_S3_DIR": str(store), "OFFSITE_S3_ENDPOINT": "https://s3.example.invalid",
         "OFFSITE_S3_BUCKET": "ira-backups", "OFFSITE_S3_ACCESS_KEY": "test-key", "OFFSITE_S3_SECRET_KEY": "test-secret"}
    return tmp_path, e, store


def _sh(script, e, *args):
    return subprocess.run(["/bin/sh", str(DEPLOY / script), *args], env=e, capture_output=True, text=True, timeout=60)


def test_a_backup_is_copied_off_site_encrypted_with_its_checksum(env):
    tmp, e, store = env
    r = _sh("backup.sh", e)
    assert r.returncode == 0, r.stderr
    names = sorted(os.listdir(store))
    assert len(names) == 2 and names[0].endswith(".sql.gz.enc") and names[1].endswith(".sql.gz.enc.sha256")
    data = (store / names[0]).read_bytes()
    assert data.startswith(b"Salted__"), "what leaves the machine is the openssl ciphertext"
    assert b"CREATE TABLE" not in data


def test_no_bucket_means_no_off_site_copy(env):
    tmp, e, store = env
    e = dict(e)
    e.pop("OFFSITE_S3_BUCKET")
    assert _sh("backup.sh", e).returncode == 0
    assert os.listdir(store) == []


def test_a_fetched_copy_verifies_and_restores_from_another_folder(env):
    """REMEDIATION G5: the checksum used to name the absolute path it was written at, so a
    copy anywhere else was refused by restore.sh as a 'checksum mismatch'."""
    tmp, e, store = env
    assert _sh("backup.sh", e).returncode == 0
    shutil.rmtree(tmp / "backups")  # the machine is gone
    latest = _sh("offsite.sh", e, "latest")
    name = latest.stdout.strip()
    assert latest.returncode == 0 and name.startswith("ira-care-")
    r = _sh("offsite.sh", e, "fetch", name, str(tmp / "elsewhere"))
    assert r.returncode == 0, r.stderr
    check = subprocess.run(["sha256sum", "-c", f"{name}.sha256"], cwd=tmp / "elsewhere", capture_output=True, text=True)
    assert check.returncode == 0, "the checksum verifies in the folder it was copied to"


def test_a_copy_altered_in_the_bucket_is_not_restored(env):
    tmp, e, store = env
    assert _sh("backup.sh", e).returncode == 0
    name = next(n for n in os.listdir(store) if n.endswith(".enc"))
    with open(store / name, "ab") as f:
        f.write(b"tampered")
    r = _sh("offsite.sh", e, "fetch", name, str(tmp / "elsewhere"))
    assert r.returncode == 4 and "checksum mismatch" in r.stderr
    assert not (tmp / "elsewhere" / name).exists(), "the altered file is removed, not left to be restored"


def test_only_encrypted_backups_ever_leave(env):
    tmp, e, store = env
    plain = tmp / "dump.sql"
    plain.write_text("CREATE TABLE secrets ();")
    r = _sh("offsite.sh", e, "push", str(plain))
    assert r.returncode == 3 and "only encrypted backups" in r.stderr
    assert os.listdir(store) == []


def test_a_plain_http_endpoint_is_refused_unless_a_local_drill_says_so(env):
    tmp, e, store = env
    e = dict(e, OFFSITE_S3_ENDPOINT="http://s3.example.invalid")
    assert _sh("offsite.sh", e, "latest").returncode == 2
    assert _sh("offsite.sh", dict(e, OFFSITE_ALLOW_HTTP="1"), "latest").returncode == 0


def test_the_keys_are_never_on_the_command_line():
    text = (DEPLOY / "offsite.sh").read_text()
    assert "-K -" in text and "--user" not in text, "keys go to curl on stdin, not where `ps` shows them"
