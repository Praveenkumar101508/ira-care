#!/usr/bin/env bash
#
# Migration 054 refuses to destroy live credentials
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 054's rollback drops `user_credentials`, which holds the only copy of
# every registered account's password hash. A hash cannot be
# reconstructed and there is no password-reset flow yet, so dropping the
# table permanently locks those accounts out — that is data loss, not a
# rollback.
#
# The cycle below clears the credentials before peeling 054, so it
# proves the reversal and can never reach the refusal. This is the other
# half.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest054
PSQL="psql -h localhost -U postgres"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }
q "INSERT INTO users (id,email,display_name,auth_provider,status) VALUES (gen_random_uuid(),'r54@ci.example','R','password','active')"
UID_=$(q "SELECT id FROM users WHERE email='r54@ci.example'")
# A bcrypt-shaped value; the guard counts rows, and what is under test
# is the refusal rather than the hash.
${PSQL} -d ${DB} -q -c "INSERT INTO user_credentials (user_id,password_hash) VALUES ('${UID_}','\$2b\$12\$abcdefghijklmnopqrstuvwx')"
[ "$(q "SELECT count(*) FROM user_credentials")" = "1" ] \
  || { echo "::error::the fixture credential was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/054_registration_down.sql 2>/tmp/r054.err; then
  echo "::error::rollback/054 succeeded while an account held a credential. It must refuse — the hash is unreconstructable and there is no password-reset flow."
  exit 1
fi
grep -q "054-down REFUSING" /tmp/r054.err \
  || { echo "::error::rollback/054 failed for the wrong reason:"; cat /tmp/r054.err; exit 1; }
# Atomic, and FORCE RLS restored — the guard lifts it to count.
[ "$(q "SELECT to_regclass('public.user_credentials') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/054 refused but still dropped user_credentials"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.user_credentials'::regclass")" = "t" ] \
  || { echo "::error::rollback/054 refused but left user_credentials without FORCE RLS"; exit 1; }
echo "✅ rollback/054 refuses while credentials exist, atomically"

${PSQL} -d ${DB} -q -c "DELETE FROM user_credentials"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/054_registration_down.sql
[ "$(q "SELECT to_regclass('public.user_credentials') IS NULL")" = "t" ] \
  || { echo "::error::rollback/054 left user_credentials behind"; exit 1; }
echo "✅ rollback/054 reverses when nothing is registered"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/054_registration.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_credentials'::regclass")" = "4" ] \
  || { echo "::error::054 re-applied with the wrong policy count"; exit 1; }
echo "✅ 054 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
