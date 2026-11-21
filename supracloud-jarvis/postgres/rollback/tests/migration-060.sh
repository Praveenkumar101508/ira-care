#!/usr/bin/env bash
#
# Migration 060 refuses to reopen the credential boundary
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#

set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest060
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

for T in connector_accounts companion_preferences; do
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND polname='tenant_isolation'")" = "0" ] \
    || { echo "::error::${T} still carries 023's tenant-only policy after 060"; exit 1; }
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass")" = "4" ] \
    || { echo "::error::${T} does not have exactly 4 policies after 060"; exit 1; }
  [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.${T}'::regclass AND (coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%' OR coalesce(pg_get_expr(polqual,polrelid),'')||coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.tenant_id%')")" = "0" ] \
    || { echo "::error::a ${T} policy does not constrain BOTH app.user_id and app.tenant_id"; exit 1; }
  [ "$(q "SELECT has_table_privilege('ira_worker','${T}','SELECT')")" = "f" ] \
    || { echo "::error::ira_worker can read ${T} — a job holding OAuth refresh tokens can act as any user at any connected provider"; exit 1; }
done
echo "✅ 060: a member's OAuth credentials are their own"

q "INSERT INTO tenants (id,name,slug,edition) VALUES ('11111111-0000-0000-0000-000000000060','R60','r60','cloud')"
q "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('22222222-0000-0000-0000-000000000060','R','password','active')"
${PSQL} -d ${DB} -q -c "INSERT INTO connector_accounts (tenant_id,user_id,connector,provider_account,granted_scopes,credential_ciphertext,encryption_key_id,credential_nonce) VALUES ('11111111-0000-0000-0000-000000000060','22222222-0000-0000-0000-000000000060','gmail','r60@ci.example','{}','\\xdeadbeef'::bytea,'k1','\\x010101010101010101010101'::bytea)"
[ "$(q "SELECT count(*) FROM connector_accounts WHERE credential_ciphertext IS NOT NULL AND revoked_at IS NULL")" = "1" ] \
  || { echo "::error::the fixture credential was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/060_user_scoped_credentials_down.sql 2>/tmp/r060.err; then
  echo "::error::rollback/060 succeeded while a live OAuth credential existed. Reversing it lets every member of a tenant read every other member's refresh token."
  exit 1
fi
grep -q "060-down REFUSING" /tmp/r060.err \
  || { echo "::error::rollback/060 failed for the wrong reason:"; cat /tmp/r060.err; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.connector_accounts'::regclass")" = "4" ] \
  || { echo "::error::rollback/060 refused but still changed the policies"; exit 1; }
echo "✅ rollback/060 refuses while a live credential exists, atomically"

# Both columns together: `revoked_accounts_hold_no_credential` refuses
# a revoked row that still holds ciphertext, and rightly — a dead
# grant has no business keeping the secret that opened it.
${PSQL} -d ${DB} -q -c "UPDATE connector_accounts SET revoked_at = NOW(), credential_ciphertext = NULL, credential_nonce = NULL, encryption_key_id = NULL"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/060_user_scoped_credentials_down.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.connector_accounts'::regclass AND polname='tenant_isolation'")" = "1" ] \
  || { echo "::error::rollback/060 did not restore 023's policy — a table under FORCE RLS with no policy denies everything silently"; exit 1; }
echo "✅ rollback/060 reverses when no credential is live"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/060_user_scoped_credentials.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.connector_accounts'::regclass")" = "4" ] \
  || { echo "::error::060 re-applied with the wrong policy count"; exit 1; }
echo "✅ 060 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
