#!/usr/bin/env bash
# Apply the migration chain as a NON-SUPERUSER OWNER, then re-apply it.
#
# Why this exists
# ---------------
# A superuser bypasses row-level security entirely. Verifying migrations as one
# proves only that the SQL parses: every migration-time policy failure is
# invisible. Two defects sat in `main`-bound code for a whole phase because the
# verification database ran as `postgres`:
#
#   * 035_memberships.sql could not migrate a database that had any memberships
#     in it — its backfill UPDATE was filtered by forced RLS, matched zero rows
#     WITHOUT failing, and the SET NOT NULL two statements later failed;
#   * totp_migration_status.contract_ready reported `true` while a plaintext
#     TOTP secret existed, because a view runs as its owner and the owner is
#     subject to FORCE.
#
# Neither was findable by any test running as a superuser.
#
# Usage
# -----
#   PGHOST=... PGPORT=... ./verify_migrations_as_owner.sh [dbname]
#
# Requires a superuser connection to create the role, the database and the
# extensions; everything after that runs as the non-superuser owner.
set -euo pipefail

DB="${1:-iraowner}"
ROLE="${IRA_MIGRATION_ROLE:-ira_migrator}"
PSQL="${PSQL:-psql}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_DIR="$(cd "$HERE/../../../postgres" && pwd)"

$PSQL -q -d postgres -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$ROLE') THEN
    CREATE ROLE $ROLE LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF; END \$\$;"
$PSQL -q -d postgres -c "DROP DATABASE IF EXISTS $DB"
$PSQL -q -d postgres -c "CREATE DATABASE $DB OWNER $ROLE"
$PSQL -q -d "$DB" -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'
$PSQL -q -d "$DB" -c "GRANT ALL ON SCHEMA public TO $ROLE"

# The assertion this whole script rests on.
subverted=$($PSQL -tA -d "$DB" -c \
  "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='$ROLE'")
if [ "$subverted" != "f" ]; then
  echo "REFUSING: $ROLE is a superuser or holds BYPASSRLS, so it bypasses every"
  echo "policy and this verification would prove nothing." >&2
  exit 1
fi

echo "── applying the chain as $ROLE (non-superuser owner) ──"
for f in "$PG_DIR"/init.sql "$PG_DIR"/0*.sql; do
  $PSQL -q -v ON_ERROR_STOP=1 -U "$ROLE" -d "$DB" -f "$f" >/dev/null
done
echo "first apply: OK"

# Re-apply only the migrations this branch owns. init.sql and 023_tenancy.sql
# seed rows into tables that later gain forced RLS and cannot be re-applied by a
# non-superuser owner — recorded as R-51, pre-existing, out of scope here.
echo "── re-applying 033+ (idempotence) ──"
for f in "$PG_DIR"/0[3-9]*.sql; do
  $PSQL -q -v ON_ERROR_STOP=1 -U "$ROLE" -d "$DB" -f "$f" >/dev/null
done
echo "re-apply: OK"

unforced=$($PSQL -tA -U "$ROLE" -d "$DB" -c \
  "SELECT coalesce(string_agg(c.relname, ','), '') FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname='public' AND c.relkind='r'
      AND c.relrowsecurity AND NOT c.relforcerowsecurity")
if [ "$unforced" != "legacy_identity_map" ]; then
  echo "FAIL: tables with RLS enabled but not forced: '$unforced'" >&2
  echo "A migration lifted FORCE for a backfill and did not restore it." >&2
  exit 1
fi
echo "FORCE restored everywhere except legacy_identity_map (intended)"
echo "── verification complete ──"
