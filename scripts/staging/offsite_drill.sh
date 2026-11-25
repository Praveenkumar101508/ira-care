#!/usr/bin/env bash
# The off-site restore drill (register G2.h): back up, copy off-site, lose the machine's
# copy, fetch it back, restore into an empty database, and compare.
#
#   scripts/staging/offsite_drill.sh <admin dsn base> <s3 endpoint> <bucket> <out.json>
#
# The real scripts are used: deploy/backup.sh, deploy/offsite.sh (curl, AWS Signature V4)
# and deploy/restore.sh. Two scratch databases are created and dropped:
# ira_offsite_src (the full migration chain applied) and ira_offsite_dst (empty). Keys and
# the passphrase are throwaway values made for this run only.
set -euo pipefail
ADMIN_BASE="${1:?admin dsn base, e.g. postgresql://postgres:postgres@localhost:5432}"
ENDPOINT="${2:?s3 endpoint}"; BUCKET="${3:?bucket}"; OUT="${4:?output json}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
SRC=ira_offsite_src; DST=ira_offsite_dst
export BACKUP_PASSPHRASE="drill-$(openssl rand -hex 16)"
export BACKUP_DIR="$WORK/backups" BACKUP_DSN="$ADMIN_BASE/$SRC"
export OFFSITE_S3_ENDPOINT="$ENDPOINT" OFFSITE_S3_BUCKET="$BUCKET" OFFSITE_S3_REGION=us-east-1
export OFFSITE_S3_ACCESS_KEY="drill-key-$(openssl rand -hex 4)" OFFSITE_S3_SECRET_KEY="drill-$(openssl rand -hex 16)"
case "$ENDPOINT" in http://127.0.0.1*|http://localhost*) export OFFSITE_ALLOW_HTTP=1 ;; esac

psql -q "$ADMIN_BASE/postgres" -c "DROP DATABASE IF EXISTS $SRC" -c "DROP DATABASE IF EXISTS $DST" \
     -c "CREATE DATABASE $SRC" -c "CREATE DATABASE $DST" >/dev/null
psql -q "$ADMIN_BASE/$SRC" -v ON_ERROR_STOP=1 -f "$ROOT/supracloud-jarvis/postgres/provisioning/001_database_roles.sql" >/dev/null
(cd "$ROOT/supracloud-jarvis/ira" && python3 -m scripts.migrate apply --dsn "${ADMIN_BASE/postgres:postgres/ira_migrator:ira_migrator}/$SRC" >/dev/null)

counts() {  # tables, migrations applied, rows across all public tables
  psql -At "$ADMIN_BASE/$1" -c "SELECT count(*) FROM pg_tables WHERE schemaname='public'" \
       -c "SELECT count(*) FROM schema_migrations" \
       -c "SELECT coalesce(sum((xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', schemaname, tablename), false, true, '')))[1]::text::bigint), 0) FROM pg_tables WHERE schemaname='public'" \
    | paste -sd' '
}
before="$(counts $SRC)"

# Create the bucket (a signed PUT on the bucket itself).
printf 'user = "%s:%s"\n' "$OFFSITE_S3_ACCESS_KEY" "$OFFSITE_S3_SECRET_KEY" \
  | curl -fsS --aws-sigv4 "aws:amz:us-east-1:s3" -K - -X PUT "${ENDPOINT%/}/$BUCKET" >/dev/null

t0=$(date +%s)
sh "$ROOT/deploy/backup.sh" > "$WORK/backup.log"
rm -rf "$BACKUP_DIR"                                   # the machine's own copy is gone
name="$(sh "$ROOT/deploy/offsite.sh" latest)"
sh "$ROOT/deploy/offsite.sh" fetch "$name" "$WORK/fetched" > "$WORK/fetch.log"
sh "$ROOT/deploy/restore.sh" "$WORK/fetched/$name" "$ADMIN_BASE/$DST" > "$WORK/restore.log"
t1=$(date +%s)
after="$(counts $DST)"

# The same file with the wrong passphrase restores nothing.
psql -q "$ADMIN_BASE/postgres" -c "DROP DATABASE IF EXISTS ${DST}_wrong" -c "CREATE DATABASE ${DST}_wrong" >/dev/null
set +e
BACKUP_PASSPHRASE=not-the-passphrase sh "$ROOT/deploy/restore.sh" "$WORK/fetched/$name" "$ADMIN_BASE/${DST}_wrong" >/dev/null 2>&1
wrong_rc=$?
set -e
wrong_tables="$(psql -At "$ADMIN_BASE/${DST}_wrong" -c "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
size=$(stat -c %s "$WORK/fetched/$name")
psql -q "$ADMIN_BASE/postgres" -c "DROP DATABASE $SRC" -c "DROP DATABASE $DST" -c "DROP DATABASE ${DST}_wrong" >/dev/null

python3 - "$OUT" "$before" "$after" "$name" "$size" "$((t1-t0))" "$wrong_rc" "$wrong_tables" "$ENDPOINT" <<'PY'
import json, sys, datetime, subprocess
out, before, after, name, size, secs, wrong_rc, wrong_tables, endpoint = sys.argv[1:]
b, a = before.split(), after.split()
rec = {"measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
       "revision": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
       "s3_endpoint": endpoint.split("//")[0] + "//<local>", "object": name, "object_bytes": int(size),
       "seconds_backup_to_restored": int(secs),
       "source": {"tables": int(b[0]), "migrations": int(b[1]), "rows": int(b[2])},
       "restored": {"tables": int(a[0]), "migrations": int(a[1]), "rows": int(a[2])},
       "identical": b == a,
       "wrong_passphrase": {"exit_code": int(wrong_rc), "tables_written": int(wrong_tables)}}
json.dump(rec, open(out, "w"), indent=2)
print(json.dumps(rec, indent=2))
sys.exit(0 if rec["identical"] and rec["wrong_passphrase"]["exit_code"] != 0 and rec["wrong_passphrase"]["tables_written"] == 0 else 1)
PY
