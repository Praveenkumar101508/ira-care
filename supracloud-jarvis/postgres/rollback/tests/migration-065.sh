#!/usr/bin/env bash
#
# Migration 065 lets a user hold a second factor in each of their tenants
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 064: the guarantee and the refusal both
# proved on a disposable database, so neither is assumed.
#
# 036 closed R-26 — a GLOBAL unique on totp_secrets.username — and in
# the same file added a global unique on user_id, because at the time a
# user could only be in one tenant. 035 and 055 made that false and
# nothing revisited the index, so a user with two memberships could
# enrol a second factor in ONE tenant, was not challenged in the other,
# and could not turn one on there.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest065
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

[ "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='totp_secrets' AND indexname='totp_secrets_user_key'")" = "0" ] \
  || { echo "::error::totp_secrets_user_key survived 065. One second factor per user ACROSS ALL TENANTS leaves a multi-tenant user unprotected in one of them."; exit 1; }
[ "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='totp_secrets' AND indexname='totp_secrets_tenant_user_key'")" = "1" ] \
  || { echo "::error::065 dropped the global index without adding the per-tenant one — nothing now constrains enrolments"; exit 1; }

${PSQL} -d ${DB} -q -c "INSERT INTO tenants (id,name,slug,edition) VALUES ('66666666-0000-0000-0000-000000000001','A','ci-065-a','cloud'),('66666666-0000-0000-0000-000000000002','B','ci-065-b','cloud')"
${PSQL} -d ${DB} -q -c "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('66666666-1111-0000-0000-000000000001','M','password','active')"
${PSQL} -d ${DB} -q -c "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,migration_status,enabled) VALUES ('66666666-0000-0000-0000-000000000001','66666666-1111-0000-0000-000000000001','ci065','S','PLAINTEXT',TRUE)"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,migration_status,enabled) VALUES ('66666666-0000-0000-0000-000000000002','66666666-1111-0000-0000-000000000001','ci065','S','PLAINTEXT',TRUE)" \
  || { echo "::error::a user still cannot enrol a second factor in their second tenant"; exit 1; }
echo "✅ 065: a user can hold a second factor in each of their tenants"

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,migration_status,enabled) VALUES ('66666666-0000-0000-0000-000000000001','66666666-1111-0000-0000-000000000001','ci065b','S','PLAINTEXT',TRUE)" 2>/tmp/d065.err; then
  echo "::error::065 widened the index into nothing — two live enrolments in the SAME tenant were accepted, and the login path cannot resolve which seed is authoritative"
  exit 1
fi
grep -q "totp_secrets_tenant_user_key" /tmp/d065.err \
  || { echo "::error::the duplicate-enrolment insert failed for the wrong reason:"; cat /tmp/d065.err; exit 1; }
echo "✅ 065 still refuses two live enrolments in one tenant"

# The rollback refuses rather than destroying somebody's second factor.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/065_totp_enrolment_is_per_tenant_down.sql 2>/tmp/r065.err; then
  echo "::error::rollback/065 succeeded while a user held a live second factor in two tenants. That silently locks somebody out of an organisation."
  exit 1
fi
grep -q "065-down REFUSING" /tmp/r065.err \
  || { echo "::error::rollback/065 failed for the wrong reason:"; cat /tmp/r065.err; exit 1; }
[ "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='totp_secrets' AND indexname='totp_secrets_tenant_user_key'")" = "1" ] \
  || { echo "::error::rollback/065 refused but still dropped the per-tenant index"; exit 1; }
echo "✅ rollback/065 refuses while it would cost somebody their second factor, atomically"

${PSQL} -d ${DB} -q -c "UPDATE totp_secrets SET revoked_at = NOW() WHERE tenant_id = '66666666-0000-0000-0000-000000000002'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/065_totp_enrolment_is_per_tenant_down.sql
[ "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='totp_secrets' AND indexname='totp_secrets_user_key'")" = "1" ] \
  || { echo "::error::rollback/065 did not restore 036's index"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/065_totp_enrolment_is_per_tenant.sql
[ "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='totp_secrets' AND indexname='totp_secrets_tenant_user_key'")" = "1" ] \
  || { echo "::error::065 re-applied without its index"; exit 1; }
echo "✅ 065 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
