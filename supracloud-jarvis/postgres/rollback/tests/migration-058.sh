#!/usr/bin/env bash
#
# Migration 058 refuses to strand an outstanding reset link
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 057: refusal and reversal both proved on a
# disposable database, so neither is assumed.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest058
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

# Possession is the whole predicate, and `app.reset_user_id` must
# reach DELETE and nothing else. A user id is guessable where a token
# hash is not, so a SELECT accepting it would turn a harmless
# cancellation into watching resets happen.
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass")" = "4" ] \
  || { echo "::error::password_reset_tokens does not have exactly 4 policies"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.reset_token_hash%'")" = "0" ] \
  || { echo "::error::a password_reset_tokens policy does not key on possession of the token"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass AND polcmd IN ('a','w') AND coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') LIKE '%app.reset_user_id%'")" = "0" ] \
  || { echo "::error::a password_reset_tokens INSERT/UPDATE policy accepts app.reset_user_id — that lets a caller point somebody else's pending reset at a hash whose preimage they chose, which is account takeover rather than a leak. Minting and spending stay possession-only."; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_worker','password_reset_tokens','SELECT')")" = "f" ] \
  || { echo "::error::ira_worker can read password_reset_tokens — a job that can read this table can take over any account"; exit 1; }
echo "✅ 058: minting and spending are possession-only"

q "INSERT INTO users (id,email,display_name,auth_provider,status) VALUES ('33333333-0000-0000-0000-000000000058','r58@ci.example','R','password','active')"
${PSQL} -d ${DB} -q -c "INSERT INTO password_reset_tokens (user_id,token_hash,email,expires_at) VALUES ('33333333-0000-0000-0000-000000000058',repeat('a',64),'r58@ci.example',NOW()+interval '1 hour')"
[ "$(q "SELECT count(*) FROM password_reset_tokens WHERE consumed_at IS NULL AND expires_at > NOW()")" = "1" ] \
  || { echo "::error::the fixture reset token was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/058_password_reset_down.sql 2>/tmp/r058.err; then
  echo "::error::rollback/058 succeeded while an unexpired reset link was outstanding. It must refuse — each one is somebody who has lost their password and is holding a link that would stop working silently."
  exit 1
fi
grep -q "058-down REFUSING" /tmp/r058.err \
  || { echo "::error::rollback/058 failed for the wrong reason:"; cat /tmp/r058.err; exit 1; }
[ "$(q "SELECT to_regclass('public.password_reset_tokens') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/058 refused but still dropped the table"; exit 1; }
echo "✅ rollback/058 refuses while a reset link is outstanding, atomically"

${PSQL} -d ${DB} -q -c "UPDATE password_reset_tokens SET consumed_at = NOW()"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/058_password_reset_down.sql
[ "$(q "SELECT to_regclass('public.password_reset_tokens') IS NULL")" = "t" ] \
  || { echo "::error::rollback/058 left password_reset_tokens behind"; exit 1; }
echo "✅ rollback/058 reverses when no link is outstanding"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/058_password_reset.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.password_reset_tokens'::regclass")" = "4" ] \
  || { echo "::error::058 re-applied with the wrong policy count"; exit 1; }
echo "✅ 058 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
