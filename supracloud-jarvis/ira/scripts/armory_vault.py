"""
scripts/armory_vault.py — owner-side admin for an existing Armory Vault.

The vault ships with starter files, but real use means editing them: adding
tools, widening the target scope, rotating the Master Key. All of that needs the
key (to decrypt) and happens ON THE HOST — never remotely — so it lives in this
CLI rather than the API.

Subcommands (all prompt for the Master Key unless --key is given):

  show                    print the tool ids and whitelist entries in the vault
  export --out DIR        write the decrypted files to DIR for editing
  import --in DIR         validate the edited files and re-seal the vault
  rotate-key              re-seal under a new Master Key (updates the .env verifier)

Typical edit loop:
  python -m scripts.armory_vault export --out /tmp/vault
  $EDITOR /tmp/vault/manifest.json          # add tools / edit whitelist.txt
  python -m scripts.armory_vault import --in /tmp/vault
  rm -rf /tmp/vault                         # the plaintext copy is yours to clean up

Editing always re-validates the manifest/whitelist before writing, so a broken
edit is rejected rather than sealed.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

_IRA_ROOT = Path(__file__).resolve().parent.parent
if str(_IRA_ROOT) not in sys.path:
    sys.path.insert(0, str(_IRA_ROOT))

from armory import vault as vaultmod  # noqa: E402
from armory import vault_encryption as venc  # noqa: E402
from armory.manifest import load_manifest  # noqa: E402
from scripts.setup_vault import ENV_VAR, upsert_env_var  # noqa: E402

DEFAULT_VAULT_DIR = _IRA_ROOT / "data" / "armory"
DEFAULT_ENV = _IRA_ROOT.parent / ".env"


def _prompt(label: str) -> str:
    key = getpass.getpass(label).strip()
    if not key:
        print("✗ key must not be empty.", file=sys.stderr)
        sys.exit(2)
    return key


def _key(args) -> str:
    return (getattr(args, "key", None) or "").strip() or _prompt("Master Key: ")


def cmd_show(args) -> int:
    bundle = vaultmod.read_bundle(args.vault_dir, _key(args))
    manifest = load_manifest(bundle[vaultmod.MANIFEST_FILE])
    print(f"Vault: {vaultmod.vault_path(args.vault_dir)}")
    print(f"Tools ({len(manifest)}): {', '.join(manifest.ids())}")
    print("\nwhitelist.txt:")
    for line in bundle[vaultmod.WHITELIST_FILE].splitlines():
        if line.strip() and not line.strip().startswith("#"):
            print(f"  {line.strip()}")
    return 0


def cmd_export(args) -> int:
    bundle = vaultmod.read_bundle(args.vault_dir, _key(args))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, content in bundle.items():
        (out / name).write_text(content, encoding="utf-8")
    print(f"✅ Exported {len(bundle)} files to {out}")
    print("   Edit them, then: python -m scripts.armory_vault import --in", out)
    print("   ⚠ these are PLAINTEXT — delete the directory when you're done.")
    return 0


def cmd_import(args) -> int:
    src = Path(args.inp)
    files: dict[str, str] = {}
    for name in vaultmod.BUNDLE_FILES:
        path = src / name
        if path.is_file():
            files[name] = path.read_text(encoding="utf-8")
    if vaultmod.MANIFEST_FILE not in files or vaultmod.WHITELIST_FILE not in files:
        print(f"✗ {src} must contain at least {vaultmod.MANIFEST_FILE} and "
              f"{vaultmod.WHITELIST_FILE}.", file=sys.stderr)
        return 1
    try:
        vaultmod.reseal(args.vault_dir, _key(args), files)   # validates before writing
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Refused to seal invalid vault: {exc}", file=sys.stderr)
        return 1
    print(f"✅ Re-sealed {vaultmod.vault_path(args.vault_dir)} from {src}")
    return 0


def cmd_rotate(args) -> int:
    old_key = (getattr(args, "key", None) or "").strip() or _prompt("Current Master Key: ")
    new_key = _prompt("New Master Key: ")
    if len(new_key) < 8:
        print("✗ New Master Key must be at least 8 characters.", file=sys.stderr)
        return 2
    if _prompt("Confirm New Master Key: ") != new_key:
        print("✗ New keys do not match.", file=sys.stderr)
        return 2
    try:
        vaultmod.rotate_key(args.vault_dir, old_key, new_key)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Rotation failed (vault unchanged): {exc}", file=sys.stderr)
        return 1
    upsert_env_var(Path(args.env_file), ENV_VAR, venc.hash_master_key(new_key))
    print(f"✅ Master Key rotated; verifier updated in {args.env_file}.")
    print("   Restart IRA so the new verifier is loaded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Administer an existing Armory Vault.")
    parser.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR))
    parser.add_argument("--key", help="Master Key (non-interactive; avoid in shared shells).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="List tools and whitelist entries.")
    p_exp = sub.add_parser("export", help="Write decrypted files to a directory.")
    p_exp.add_argument("--out", required=True)
    p_imp = sub.add_parser("import", help="Re-seal from an edited directory.")
    p_imp.add_argument("--in", dest="inp", required=True)
    p_rot = sub.add_parser("rotate-key", help="Re-seal under a new Master Key.")
    p_rot.add_argument("--env-file", default=str(DEFAULT_ENV))

    args = parser.parse_args(argv)
    if not vaultmod.exists(args.vault_dir):
        print(f"✗ No vault at {vaultmod.vault_path(args.vault_dir)}. Run setup_vault first.",
              file=sys.stderr)
        return 1

    try:
        if args.cmd == "show":
            return cmd_show(args)
        if args.cmd == "export":
            return cmd_export(args)
        if args.cmd == "import":
            return cmd_import(args)
        if args.cmd == "rotate-key":
            return cmd_rotate(args)
    except vaultmod.VaultError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
