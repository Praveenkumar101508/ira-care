#!/bin/sh
# Nightly encrypted database backup.
#   pg_dump | gzip | openssl enc (AES-256-CBC, PBKDF2) -> $BACKUP_DIR/ira-care-YYYYmmddTHHMMZ.sql.gz.enc
# Needs: pg_dump (PG* variables or a DSN in $BACKUP_DSN), openssl, $BACKUP_PASSPHRASE.
# Keeps $BACKUP_KEEP_DAYS days (14). Prints the file written; never prints the passphrase.
set -eu
: "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%MZ)"
out="$BACKUP_DIR/ira-care-$stamp.sql.gz.enc"
tmp="$out.part"
if [ -n "${BACKUP_DSN:-}" ]; then src="$BACKUP_DSN"; else src="${PGDATABASE:?set PGDATABASE or BACKUP_DSN}"; fi
pg_dump --no-owner --no-privileges --format=plain "$src" \
  | gzip -9 \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -out "$tmp"
mv "$tmp" "$out"
sha256sum "$out" > "$out.sha256"
find "$BACKUP_DIR" -name 'ira-care-*.sql.gz.enc' -mtime "+$KEEP" -delete
find "$BACKUP_DIR" -name 'ira-care-*.sql.gz.enc.sha256' -mtime "+$KEEP" -delete
echo "backup written: $out ($(du -k "$out" | cut -f1) KB)"
