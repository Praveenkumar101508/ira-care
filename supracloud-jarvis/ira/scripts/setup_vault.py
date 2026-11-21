"""
scripts/setup_vault.py — first-run provisioning for the Armory Vault.

Run once to stand the vault up:

    python -m scripts.setup_vault           # interactive (prompts for the key)
    python -m scripts.setup_vault --force    # re-seal, overwriting an existing vault

What it does:
  1. Prompts you (twice, hidden) for a Master Key — or takes ``--key`` for
     non-interactive/CI provisioning.
  2. Seals the shipped template files (manifest.json, whitelist.txt, the compose
     file) into ``data/armory/vault.sealed`` with AES-256-GCM.
  3. Writes ONLY a salted PBKDF2 verifier of the key to ``.env`` as
     ``ARMORY_MASTER_KEY_HASH`` — never the plaintext key.

The plaintext key is never written to disk or logged. Losing it means the vault
cannot be opened (there is no recovery path — that is the point).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Allow ``python scripts/setup_vault.py`` as well as ``-m scripts.setup_vault``.
_IRA_ROOT = Path(__file__).resolve().parent.parent
if str(_IRA_ROOT) not in sys.path:
    sys.path.insert(0, str(_IRA_ROOT))

from armory import vault as vaultmod  # noqa: E402
from armory import vault_encryption as venc  # noqa: E402

ENV_VAR = "ARMORY_MASTER_KEY_HASH"
DEFAULT_ENV = _IRA_ROOT.parent / ".env"          # supracloud-jarvis/.env
DEFAULT_VAULT_DIR = _IRA_ROOT / "data" / "armory"


def upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Set or replace ``key=value`` in a .env file, preserving other lines."""
    line = f"{key}={value}\n"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        replaced = False
        for i, existing in enumerate(lines):
            stripped = existing.lstrip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                lines[i] = line
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(line)
        env_path.write_text("".join(lines), encoding="utf-8")
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(line, encoding="utf-8")


def _prompt_key() -> str:
    key = getpass.getpass("Set the Armory Master Key: ").strip()
    if len(key) < 8:
        print("✗ Master Key must be at least 8 characters.", file=sys.stderr)
        sys.exit(2)
    confirm = getpass.getpass("Confirm the Armory Master Key: ").strip()
    if key != confirm:
        print("✗ Keys do not match.", file=sys.stderr)
        sys.exit(2)
    return key


def provision(master_key: str, *, vault_dir: Path, env_path: Path,
              overwrite: bool = False) -> Path:
    """Seal the vault and record the verifier. Returns the vault file path."""
    sealed = vaultmod.create_from_template(vault_dir, master_key, overwrite=overwrite)
    upsert_env_var(env_path, ENV_VAR, venc.hash_master_key(master_key))
    return sealed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the IRA Armory Vault.")
    parser.add_argument("--key", help="Master Key (non-interactive; avoid in shared shells).")
    parser.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR),
                        help="Directory to hold vault.sealed.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV),
                        help="Path to the .env file to update.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing sealed vault.")
    args = parser.parse_args(argv)

    vault_dir = Path(args.vault_dir)
    env_path = Path(args.env_file)

    if vaultmod.exists(vault_dir) and not args.force:
        print(f"✗ A vault already exists at {vaultmod.vault_path(vault_dir)}.\n"
              f"  Use --force to re-seal it (this invalidates the old Master Key).",
              file=sys.stderr)
        return 1

    master_key = (args.key or "").strip() or _prompt_key()
    if len(master_key) < 8:
        print("✗ Master Key must be at least 8 characters.", file=sys.stderr)
        return 2

    try:
        sealed = provision(master_key, vault_dir=vault_dir, env_path=env_path,
                           overwrite=args.force)
    except Exception as exc:  # noqa: BLE001 — a friendly failure beats a stack trace
        print(f"✗ Failed to provision the vault: {exc}", file=sys.stderr)
        return 1
    finally:
        master_key = ""  # drop the plaintext key reference promptly

    print(f"✅ Armory sealed at {sealed}")
    print(f"✅ Verifier written to {env_path} as {ENV_VAR} (no plaintext key stored).")
    print("   Set ARMORY_ENABLED=true and restart IRA, then unlock with your Master Key.")
    if os.environ.get(ENV_VAR):
        print("   Note: an existing ARMORY_MASTER_KEY_HASH is set in your environment; "
              "the process env overrides .env until you update it too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
