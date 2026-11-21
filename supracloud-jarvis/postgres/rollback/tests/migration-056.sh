#!/usr/bin/env bash
#
# Migration 056 refuses to strand a locked-out account
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as the 054 and 055 steps: refusal and reversal are both
# real, each proved on a disposable database so neither is assumed.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest056
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }
q "INSERT INTO tenants (id,name,slug,edition) VALUES ('11111111-0000-0000-0000-000000000056','R56','r56','cloud')"
q "INSERT INTO users (id,email,display_name,auth_provider,status,email_verified_at) VALUES ('22222222-0000-0000-0000-000000000056','r56@ci.example','R','password','active',NOW())"
${PSQL} -d ${DB} -q -c "INSERT INTO mfa_recovery_codes (tenant_id,user_id,code_hash) VALUES ('11111111-0000-0000-0000-000000000056','22222222-0000-0000-0000-000000000056','\$2b\$12\$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLM')"
[ "$(q "SELECT count(*) FROM mfa_recovery_codes WHERE consumed_at IS NULL")" = "1" ] \
  || { echo "::error::the fixture recovery code was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/056_mfa_recovery_down.sql 2>/tmp/r056.err; then
  echo "::error::rollback/056 succeeded while an unused recovery code existed. It must refuse — those codes are the only way back for an account whose second factor is lost, and the plaintext was shown once and never stored."
  exit 1
fi
grep -q "056-down REFUSING" /tmp/r056.err \
  || { echo "::error::rollback/056 failed for the wrong reason:"; cat /tmp/r056.err; exit 1; }
[ "$(q "SELECT to_regclass('public.mfa_recovery_codes') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/056 refused but still dropped mfa_recovery_codes"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.mfa_recovery_codes'::regclass")" = "t" ] \
  || { echo "::error::rollback/056 refused but left mfa_recovery_codes without FORCE RLS"; exit 1; }
echo "✅ rollback/056 refuses while an unused recovery code exists, atomically"

${PSQL} -d ${DB} -q -c "UPDATE mfa_recovery_codes SET consumed_at = NOW()"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/056_mfa_recovery_down.sql
[ "$(q "SELECT to_regclass('public.mfa_recovery_codes') IS NULL")" = "t" ] \
  || { echo "::error::rollback/056 left mfa_recovery_codes behind"; exit 1; }
echo "✅ rollback/056 reverses when every code is spent"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/056_mfa_recovery.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.mfa_recovery_codes'::regclass")" = "4" ] \
  || { echo "::error::056 re-applied with the wrong policy count"; exit 1; }
echo "✅ 056 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
