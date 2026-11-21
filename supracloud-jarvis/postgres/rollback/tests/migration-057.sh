#!/usr/bin/env bash
#
# Migration 057 refuses to reopen the session boundary
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054, 055 and 056: refusal and reversal both proved on a
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

DB=iratest057
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

# The property 057 exists to establish, asserted before anything else
# is claimed about it. 023's tenant-only policy must be GONE: PostgreSQL
# ORs permissive policies, so leaving it beside the new four would make
# every other assertion here decorative.
for T in user_sessions user_devices; do
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND polname='tenant_isolation'")" = "0" ] \
    || { echo "::error::${T} still carries 023's tenant-only policy after 057"; exit 1; }
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass")" = "4" ] \
    || { echo "::error::${T} does not have exactly 4 policies after 057"; exit 1; }
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND (coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%' OR coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%')")" = "0" ] \
    || { echo "::error::a ${T} policy does not constrain BOTH app.user_id and app.tenant_id — every member of a tenant could read and revoke every other member's sessions"; exit 1; }
  [ "$(q "SELECT has_table_privilege('ira_worker','${T}','SELECT')")" = "f" ] \
    || { echo "::error::ira_worker can read ${T}"; exit 1; }
done
echo "✅ 057 replaced the tenant-only policy with both-GUC policies"

q "INSERT INTO tenants (id,name,slug,edition) VALUES ('11111111-0000-0000-0000-000000000057','R57','r57','cloud')"
q "INSERT INTO users (id,email,display_name,auth_provider,status,email_verified_at) VALUES ('22222222-0000-0000-0000-000000000057','r57@ci.example','R','password','active',NOW())"
${PSQL} -d ${DB} -q -c "INSERT INTO user_sessions (tenant_id,user_id,token_hash,expires_at) VALUES ('11111111-0000-0000-0000-000000000057','22222222-0000-0000-0000-000000000057','deadbeef',NOW()+interval '1 day')"
[ "$(q "SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL")" = "1" ] \
  || { echo "::error::the fixture session was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/057_session_isolation_down.sql 2>/tmp/r057.err; then
  echo "::error::rollback/057 succeeded while a live session existed. It must refuse — reversing it restores 023's tenant-only policy, under which every member of a tenant can read and revoke every other member's sessions, and erasing one account deletes them all."
  exit 1
fi
grep -q "057-down REFUSING" /tmp/r057.err \
  || { echo "::error::rollback/057 failed for the wrong reason:"; cat /tmp/r057.err; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_sessions'::regclass")" = "4" ] \
  || { echo "::error::rollback/057 refused but still changed the policies"; exit 1; }
echo "✅ rollback/057 refuses while a live session exists, atomically"

${PSQL} -d ${DB} -q -c "UPDATE user_sessions SET revoked_at = NOW()"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/057_session_isolation_down.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_sessions'::regclass AND polname='tenant_isolation'")" = "1" ] \
  || { echo "::error::rollback/057 did not restore 023's policy — a table under FORCE RLS with no policy denies everything silently"; exit 1; }
echo "✅ rollback/057 reverses when no session is live"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/057_session_isolation.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_sessions'::regclass")" = "4" ] \
  || { echo "::error::057 re-applied with the wrong policy count"; exit 1; }
echo "✅ 057 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
