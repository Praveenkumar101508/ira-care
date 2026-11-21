"""
armory/vault.py — the on-disk sealed vault and the decrypt-to-RAM open path.

The vault is a single sealed file (default ``data/armory/vault.sealed``). At rest
it is AES-256-GCM ciphertext under a small plaintext header — "unreadable
gibberish when locked", as the spec requires. Its cleartext is a JSON bundle of
the three vault files:

    {"files": {"manifest.json": "...", "whitelist.txt": "...",
               "docker-compose.armory.yml": "..."}}

``create_from_template`` builds the first sealed vault from the shipped template
files. ``open`` decrypts the blob **into memory** and returns parsed
:class:`VaultContents` — it never writes plaintext to disk. ``reseal`` writes an
updated bundle back (used when an owner edits the manifest/whitelist).

Only this module touches the vault file; the gatekeeper goes through it so the
"decrypt to RAM, never to disk" rule lives in exactly one place.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from armory import vault_encryption as venc
from armory.manifest import ManifestError, load_manifest
from armory.session import VaultContents
from armory.whitelist import parse_whitelist

logger = logging.getLogger("ira.armory.vault")

VAULT_FILENAME = "vault.sealed"
TEMPLATE_DIR = Path(__file__).parent / "vault_template"

#: The files that make up a vault bundle.
MANIFEST_FILE = "manifest.json"
WHITELIST_FILE = "whitelist.txt"
COMPOSE_FILE = "docker-compose.armory.yml"
BUNDLE_FILES = (MANIFEST_FILE, WHITELIST_FILE, COMPOSE_FILE)


class VaultError(RuntimeError):
    """Raised for vault I/O or integrity problems (fail-closed)."""


def vault_path(vault_dir: str | Path) -> Path:
    return Path(vault_dir).expanduser() / VAULT_FILENAME


def exists(vault_dir: str | Path) -> bool:
    return vault_path(vault_dir).is_file()


def _pack(files: dict[str, str]) -> bytes:
    return json.dumps({"files": files}, ensure_ascii=False).encode("utf-8")


def _unpack(plaintext: bytes) -> dict[str, str]:
    try:
        doc = json.loads(plaintext.decode("utf-8"))
        files = doc["files"]
        if not isinstance(files, dict):
            raise ValueError("'files' is not an object")
        return {str(k): str(v) for k, v in files.items()}
    except Exception as exc:  # noqa: BLE001
        raise VaultError(f"vault bundle is corrupt: {exc}") from exc


def _parse_contents(files: dict[str, str]) -> VaultContents:
    """Validate + parse a decrypted bundle into typed contents (fail-closed).

    A malformed manifest surfaces as a :class:`VaultError` (not the lower-level
    ManifestError), so every caller — open, reseal, rotate, the CLI import — sees
    one consistent vault-layer exception when a bundle is invalid.
    """
    for name in (MANIFEST_FILE, WHITELIST_FILE):
        if name not in files:
            raise VaultError(f"vault is missing required file: {name}")
    try:
        manifest = load_manifest(files[MANIFEST_FILE])
        whitelist = parse_whitelist(files[WHITELIST_FILE])
    except ManifestError as exc:
        raise VaultError(f"invalid manifest in vault bundle: {exc}") from exc
    return VaultContents(
        manifest=manifest,
        whitelist=whitelist,
        compose_yaml=files.get(COMPOSE_FILE, ""),
    )


def _read_template() -> dict[str, str]:
    files: dict[str, str] = {}
    for name in BUNDLE_FILES:
        path = TEMPLATE_DIR / name
        if not path.is_file():
            raise VaultError(f"template file missing: {path}")
        files[name] = path.read_text(encoding="utf-8")
    return files


def create_from_template(vault_dir: str | Path, master_key: str,
                         *, overwrite: bool = False) -> Path:
    """Create the first sealed vault from the shipped template files.

    Validates the template before sealing so a broken bundle can never become a
    vault. Refuses to clobber an existing vault unless ``overwrite=True``.
    """
    target = vault_path(vault_dir)
    if target.exists() and not overwrite:
        raise VaultError(f"vault already exists at {target}; refusing to overwrite")
    files = _read_template()
    _parse_contents(files)  # fail before we seal anything invalid
    blob = venc.encrypt(_pack(files), master_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, blob)
    logger.info("armory vault sealed at %s (%d files)", target, len(files))
    return target


def open(vault_dir: str | Path, master_key: str) -> VaultContents:  # noqa: A003
    """Decrypt the sealed vault into RAM and return parsed contents.

    Raises :class:`VaultError` if the vault is missing, the key is wrong, or the
    bundle is corrupt. Never writes plaintext to disk.
    """
    target = vault_path(vault_dir)
    if not target.is_file():
        raise VaultError(f"no vault at {target}; run setup_vault first")
    blob = target.read_bytes()
    try:
        plaintext = venc.decrypt(blob, master_key)
    except VaultError:
        raise
    except Exception as exc:  # wrong key / tamper → GCM raises; stay locked
        raise VaultError("failed to open vault (wrong master key or tampered vault)") from exc
    return _parse_contents(_unpack(plaintext))


def reseal(vault_dir: str | Path, master_key: str, files: dict[str, str]) -> Path:
    """Re-encrypt an updated bundle. Validates before writing (fail-closed)."""
    _parse_contents(files)
    blob = venc.encrypt(_pack(files), master_key)
    target = vault_path(vault_dir)
    _atomic_write(target, blob)
    logger.info("armory vault resealed at %s", target)
    return target


def rotate_key(vault_dir: str | Path, old_key: str, new_key: str) -> Path:
    """Re-seal the vault under a new master key, preserving its contents.

    Decrypts with ``old_key`` and re-encrypts the SAME bundle with ``new_key`` (a
    fresh salt/nonce). Fail-closed: a wrong old key or corrupt vault raises before
    anything is written, so a bad rotation can never brick the vault.
    """
    if not new_key or not new_key.strip():
        raise VaultError("new master key must not be empty")
    files = read_bundle(vault_dir, old_key)   # raises on wrong old key
    return reseal(vault_dir, new_key, files)


def read_bundle(vault_dir: str | Path, master_key: str) -> dict[str, str]:
    """Return the raw decrypted file bundle (for editing then reseal)."""
    target = vault_path(vault_dir)
    if not target.is_file():
        raise VaultError(f"no vault at {target}")
    try:
        plaintext = venc.decrypt(target.read_bytes(), master_key)
    except Exception as exc:  # noqa: BLE001
        raise VaultError("failed to open vault (wrong master key or tampered vault)") from exc
    return _unpack(plaintext)


def is_sealed(vault_dir: str | Path) -> bool:
    """True if the vault file exists and is a well-formed sealed blob."""
    target = vault_path(vault_dir)
    if not target.is_file():
        return False
    return venc.looks_encrypted(target.read_bytes())


def _atomic_write(target: Path, data: bytes) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written vault."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        tmp.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX perms
    tmp.replace(target)


__all__ = [
    "VAULT_FILENAME", "TEMPLATE_DIR", "BUNDLE_FILES",
    "MANIFEST_FILE", "WHITELIST_FILE", "COMPOSE_FILE",
    "VaultError", "vault_path", "exists", "is_sealed",
    "create_from_template", "open", "reseal", "read_bundle", "rotate_key",
]
