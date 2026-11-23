"""Local, never-committed state for the demo and simulation scripts.

Every secret these scripts need is generated on the machine that runs them:
the sealing key for access notes (``IRA_CONNECTOR_ENCRYPTION_KEY``) and the
placeholder values the server refuses to start without. Nothing here is a
fixed value in the repository, so nothing here can leak by being committed.

The key lives in ``.demo/connector.key`` (mode 0600, and ``.demo/`` is ignored
by git). ``seed_demo.py`` replaces it on every run, because the notes it seals
are reseeded at the same time; the other scripts read whatever is there.
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = ROOT / ".demo"
KEY_FILE = DEMO_DIR / "connector.key"
PLACEHOLDER_KEYS = ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY")


def fresh_key() -> str:
    """32 random bytes, base64: the format the server expects."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def fresh_password(words: int = 3) -> str:
    """A login password for a seeded demo account: random on every run."""
    return "-".join(secrets.token_hex(3) for _ in range(words))


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text)


def connector_key(*, renew: bool = False) -> str:
    """The local sealing key, created (or with ``renew`` replaced) on demand."""
    if os.environ.get("IRA_CONNECTOR_ENCRYPTION_KEY") and not renew:
        return os.environ["IRA_CONNECTOR_ENCRYPTION_KEY"]
    if renew or not KEY_FILE.exists():
        write_private(KEY_FILE, fresh_key() + "\n")
    return KEY_FILE.read_text().strip()


def prepare_environment(*, renew_key: bool = False) -> None:
    """Fill the environment with local-only values so the server modules import.

    Each placeholder is random for this process: none of them is a credential
    anything else accepts, and none of them appears in the repository.
    """
    for name in PLACEHOLDER_KEYS:
        os.environ.setdefault(name, secrets.token_urlsafe(16))
    os.environ["IRA_CONNECTOR_ENCRYPTION_KEY"] = connector_key(renew=renew_key)
    os.environ.setdefault("IRA_CARE_MODEL", "fake")
    os.environ.setdefault("IRA_EDITION", "cloud")
