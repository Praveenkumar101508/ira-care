#!/bin/sh
# Restore an encrypted backup into a database.
#   restore.sh <file.sql.gz.enc> <target dsn or database name>
# The target must exist and be EMPTY (or be a scratch database): the dump
# carries CREATE statements and no DROPs. Checks the sha256 first when present,
# decrypts to a temporary file and stops on any failure before touching the
# database, so a wrong passphrase or a damaged file restores nothing.
set -eu
# Refuse at once without openssl: a backup that cannot be encrypted must fail loudly, never write
# nothing and look like it ran (deploy/Dockerfile.backup installs it at build time).
command -v openssl >/dev/null 2>&1 || { echo "restore: openssl not found; nothing written" >&2; exit 1; }
: "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE}"
file="${1:?backup file}"
target="${2:?target database or DSN}"
if [ -f "$file.sha256" ]; then
  (cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null) || { echo "checksum mismatch: refusing"; exit 2; }
fi
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE -in "$file" -out "$tmp" 2>/dev/null; then
  echo "decryption failed (wrong passphrase or damaged file): refusing"; exit 3
fi
if ! gunzip -t "$tmp" 2>/dev/null; then
  echo "the decrypted file is not a valid archive: refusing"; exit 4
fi
gunzip -c "$tmp" | psql --set ON_ERROR_STOP=1 --quiet "$target" >/dev/null
echo "restored $file into $target"
