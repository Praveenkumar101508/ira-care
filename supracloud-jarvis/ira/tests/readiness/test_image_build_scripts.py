"""The scripts the deployment image runs at build time work where the image runs them.

deploy/Dockerfile.api copies scripts/training/fetch_encoder.py to /tmp and runs it with
--dest. It used to work out its default destination (three directories up) before reading
--dest, so from /tmp it failed with IndexError and the API image could not be built."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _run_from_tmp(source: Path, *args: str) -> subprocess.CompletedProcess:
    copy = Path("/tmp") / f"fetch_encoder_{uuid.uuid4().hex}.py"   # exactly where the image puts it
    shutil.copy(source, copy)
    try:
        return subprocess.run([sys.executable, str(copy), *args], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"})
    finally:
        copy.unlink(missing_ok=True)


def test_the_encoder_fetch_runs_from_tmp_with_a_destination():
    result = _run_from_tmp(ROOT / "scripts/training/fetch_encoder.py", "--help")
    assert result.returncode == 0, result.stderr
    assert "IndexError" not in result.stderr


def test_the_dockerfile_still_runs_it_from_tmp():
    """If the image stops copying it to /tmp, this test no longer proves anything: say so."""
    dockerfile = (ROOT / "deploy/Dockerfile.api").read_text()
    assert "COPY scripts/training/fetch_encoder.py /tmp/fetch_encoder.py" in dockerfile
    assert "python /tmp/fetch_encoder.py --dest" in dockerfile
