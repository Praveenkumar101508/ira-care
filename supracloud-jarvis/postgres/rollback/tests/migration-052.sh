#!/usr/bin/env bash
#
# Migration 052 refuses to strip a live tenant boundary
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 052's rollback drops `totp_secrets.tenant_id`. Where more than one
# tenant holds an enrolment that column is separating real second
# factors, and removing it is the finding rather than a rollback — so
# the script refuses, atomically.
#
# The cycle below cannot prove that: it clears the fixture enrolments
# first (so the reversal can be genuinely executed there), which leaves
# nothing for the guard to refuse. Both halves are therefore real
# somewhere, and neither is assumed.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest052
PSQL="psql -h localhost -U postgres"
TA=11111111-1111-1111-1111-111111111111
TB=22222222-2222-2222-2222-222222222222
UA=aaaaaaaa-0000-0000-0000-0000000000a1
UB=bbbbbbbb-0000-0000-0000-0000000000b1

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1"; }
# Two tenants, each with an enrolment for a user called `alice`. The
# shared username is the point: 036 dropped this table's PRIMARY KEY
# on `username` precisely so two tenants could each hold one.
q "INSERT INTO tenants (id,name,slug) VALUES ('${TA}','A','ci-a'),('${TB}','B','ci-b')"
q "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('${UA}','A','local','active'),('${UB}','B','local','active')"
q "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id) VALUES ('alice','S-A',TRUE,'${UA}','${TA}'),('alice','S-B',TRUE,'${UB}','${TB}')"

# ── The refusal, and that it is atomic ─────────────────────────────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/052_totp_tenancy_down.sql 2>/tmp/r052.err; then
  echo "::error::rollback/052 succeeded while two tenants held enrolments. It must refuse — dropping tenant_id removes a live boundary between second factors."
  exit 1
fi
if ! grep -q "052-down REFUSING" /tmp/r052.err; then
  echo "::error::rollback/052 failed for the wrong reason:"; cat /tmp/r052.err; exit 1
fi
if [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass")" != "4" ] \
|| [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='tenant_id'")" != "1" ]; then
  echo "::error::rollback/052 refused but did not roll back cleanly"; exit 1
fi
echo "✅ rollback/052 refuses while a tenant boundary is live, atomically"

# ── The reversal, once only one tenant is enrolled ─────────────────
q "DELETE FROM totp_secrets WHERE tenant_id='${TB}'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/052_totp_tenancy_down.sql
if [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='tenant_id'")" != "0" ]; then
  echo "::error::rollback/052 left tenant_id behind"; exit 1
fi
# 036's policy must come back, and FORCE with it. A table left
# ENABLE-but-not-FORCE would let the schema owner read every secret.
if [ "$(q "SELECT polname FROM pg_policy WHERE polrelid='public.totp_secrets'::regclass")" != "user_private" ] \
|| [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.totp_secrets'::regclass")" != "t" ]; then
  echo "::error::rollback/052 did not restore 036's user_private policy under FORCE RLS"; exit 1
fi
echo "✅ rollback/052 reverses, restoring 036's policy under FORCE RLS"

# ── …and the backfill derives the tenant rather than guessing ──────
# The surviving row's user has no membership yet, and the deployment
# holds three tenants, so 052 must REFUSE rather than pick one.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/052_totp_tenancy.sql 2>/tmp/u052.err; then
  echo "::error::052 attributed a second factor to a tenant it could not derive. Guessing an owner for an MFA secret is an authentication bypass."
  exit 1
fi
grep -q "052 REFUSING" /tmp/u052.err || { echo "::error::052 failed for the wrong reason:"; cat /tmp/u052.err; exit 1; }
echo "✅ 052 refuses to guess a tenant for an enrolment"

q "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) VALUES ('${TA}','${UA}','member','active')"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/052_totp_tenancy.sql
if [ "$(q "SELECT tenant_id::text FROM totp_secrets")" != "${TA}" ]; then
  echo "::error::052 attributed the enrolment to the wrong tenant"; exit 1
fi
echo "✅ 052 derives the tenant from tenant_memberships, and 052/rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
