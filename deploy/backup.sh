#!/bin/sh
# Nightly encrypted database backup.
#   pg_dump | gzip | openssl enc (AES-256-CBC, PBKDF2) -> $BACKUP_DIR/ira-care-YYYYmmddTHHMMZ.sql.gz.enc
# Needs: pg_dump (PG* variables or a DSN in $BACKUP_DSN), openssl, $BACKUP_PASSPHRASE.
# Keeps $BACKUP_KEEP_DAYS days (14). Prints the file written; never prints the passphrase.
set -eu
# Private to the service account: the dump is ciphertext, but its name, size and timing still say
# something about the people in it, and the checksum file is written beside it. Without this the
# files took the process umask (0644 on a stock host; found by the round 10 runbook drill, docs/evidence/ops/).
umask 077
# Refuse at once without openssl: a backup that cannot be encrypted must fail loudly, never write
# nothing and look like it ran (deploy/Dockerfile.backup installs it at build time).
command -v openssl >/dev/null 2>&1 || { echo "backup: openssl not found; nothing written" >&2; exit 1; }
: "${BACKUP_PASSPHRASE:?set BACKUP_PASSPHRASE}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%MZ)"
out="$BACKUP_DIR/ira-care-$stamp.sql.gz.enc"
tmp="$out.part"
if [ -n "${BACKUP_DSN:-}" ]; then src="$BACKUP_DSN"; else src="${PGDATABASE:?set PGDATABASE or BACKUP_DSN}"; fi
# A dump written by a newer pg_dump than the server can carry settings the server does not
# know, and the restore would then stop on the first of them. Refuse the mismatch here, at
# backup time, where it is cheap to notice.
server_major="$(psql -At "$src" -c 'show server_version_num' | cut -c1-2)"
dump_major="$(pg_dump --version | sed -E 's/.* ([0-9]+)[.].*/\1/')"
if [ -z "$server_major" ] || [ "$dump_major" -gt "$server_major" ]; then
  echo "pg_dump $dump_major is newer than the server ($server_major): refusing; use the server's own client tools" >&2
  exit 4
fi
pg_dump --no-owner --no-privileges --format=plain "$src" \
  | gzip -9 \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -out "$tmp"
mv "$tmp" "$out"
# The checksum names the file relative to its own folder, so it still verifies wherever the
# pair is copied to (an off-site copy, another machine), not only at this path.
(cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
find "$BACKUP_DIR" -name 'ira-care-*.sql.gz.enc' -mtime "+$KEEP" -delete
find "$BACKUP_DIR" -name 'ira-care-*.sql.gz.enc.sha256' -mtime "+$KEEP" -delete
echo "backup written: $out ($(du -k "$out" | cut -f1) KB)"
# Off-site: when a bucket is configured, the encrypted file and its checksum are copied there
# (deploy/offsite.sh). A copy that fails fails the backup, so the container's restart shows it.
if [ -n "${OFFSITE_S3_BUCKET:-}" ]; then
  sh "$(dirname "$0")/offsite.sh" push "$out"
fi
