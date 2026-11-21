#!/usr/bin/env bash
#
# Migration 055 refuses to destroy an accepted invitation
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as the 054 step above and for the same reason: the refusal
# path and the reversal path are both real, and each is proved on a
# disposable database so neither is assumed from the other.
#
# `working-directory` is NOT optional and is the reason the first
# version of this step failed in zero seconds: every path below is
# relative to `supracloud-jarvis`, and the four migration steps above
# all set it. Omitting it made `postgres/provisioning/...` unresolvable,
# which psql reports instantly — a failure that looks nothing like a
# database problem and was invisible to a local run that supplied the
# directory by hand instead of reading it from this file.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest055
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }
q "INSERT INTO tenants (id,name,slug,edition) VALUES ('11111111-1111-1111-1111-111111111155','R55','r55','cloud')"
q "INSERT INTO users (id,email,display_name,auth_provider,status,email_verified_at) VALUES ('22222222-0000-0000-0000-000000000155','r55@ci.example','R','password','active',NOW())"
${PSQL} -d ${DB} -q -c "INSERT INTO tenant_invitations (tenant_id,email,role,token_hash,invited_by,expires_at,accepted_at,accepted_by) VALUES ('11111111-1111-1111-1111-111111111155','x@ci.example','member',repeat('b',64),'22222222-0000-0000-0000-000000000155',NOW()+interval '1 day',NOW(),'22222222-0000-0000-0000-000000000155')"
[ "$(q "SELECT count(*) FROM tenant_invitations WHERE accepted_at IS NOT NULL")" = "1" ] \
  || { echo "::error::the fixture invitation was not accepted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/055_invitations_down.sql 2>/tmp/r055.err; then
  echo "::error::rollback/055 succeeded while an accepted invitation existed. It must refuse — the membership it produced survives, so the audit trail for live access would be destroyed."
  exit 1
fi
grep -q "055-down REFUSING" /tmp/r055.err \
  || { echo "::error::rollback/055 failed for the wrong reason:"; cat /tmp/r055.err; exit 1; }
# Atomic, and FORCE RLS restored — the guard lifts it to count.
[ "$(q "SELECT to_regclass('public.tenant_invitations') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/055 refused but still dropped tenant_invitations"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.tenant_invitations'::regclass")" = "t" ] \
  || { echo "::error::rollback/055 refused but left tenant_invitations without FORCE RLS"; exit 1; }
echo "✅ rollback/055 refuses while an accepted invitation exists, atomically"

${PSQL} -d ${DB} -q -c "UPDATE tenant_invitations SET accepted_at=NULL, accepted_by=NULL"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/055_invitations_down.sql
[ "$(q "SELECT to_regclass('public.tenant_invitations') IS NULL")" = "t" ] \
  || { echo "::error::rollback/055 left tenant_invitations behind"; exit 1; }
echo "✅ rollback/055 reverses when no invitation has been accepted"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/055_invitations.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.tenant_invitations'::regclass")" = "5" ] \
  || { echo "::error::055 re-applied with the wrong policy count"; exit 1; }
echo "✅ 055 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
