#!/usr/bin/env bash
#
# Migration 059 refuses to strand a federated-only account
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 058: refusal and reversal both proved on a
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

DB=iratest059
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

# The identity is (issuer, subject) and NEVER an email address. Both
# halves asserted: the key exists, and no unique index covers the
# claimed address — enforcing that would re-impose email-as-identity
# through an index (CLOUD-ADR-0008).
[ "$(q "SELECT to_regclass('public.external_identities_issuer_subject_key') IS NOT NULL")" = "t" ] \
  || { echo "::error::the (issuer, subject) unique index is missing — the same provider identity could map to several accounts"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_index i WHERE i.indrelid='public.external_identities'::regclass AND i.indisunique AND pg_get_indexdef(i.indexrelid) LIKE '%claimed_email%'")" = "0" ] \
  || { echo "::error::a UNIQUE index covers claimed_email — two people may control the same address at different providers, and enforcing otherwise is the account-merging hazard this table refuses"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.external_identities'::regclass AND polcmd='a' AND (coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.user_id%' OR coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.oidc_issuer%' OR coalesce(pg_get_expr(polwithcheck,polrelid),'') NOT LIKE '%app.oidc_subject%')")" = "0" ] \
  || { echo "::error::the external_identities INSERT policy does not require BOTH the proved identity and the target user — naming one side lets a caller link an identity to an account they do not control, or claim one they never proved"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgrelid='public.external_identities'::regclass AND tgname='external_identity_pin_key_trg'")" = "1" ] \
  || { echo "::error::the immutability trigger is missing — an UPDATE could repoint a link at another user"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_worker','external_identities','SELECT')")" = "f" ] \
  || { echo "::error::ira_worker can read external_identities"; exit 1; }
echo "✅ 059: identity is (issuer, subject), and never an email"

q "INSERT INTO users (id,email,display_name,auth_provider,status,email_verified_at) VALUES ('44444444-0000-0000-0000-000000000059','fed@ci.example','F','https://accounts.google.com','active',NOW())"
${PSQL} -d ${DB} -q -c "INSERT INTO external_identities (user_id,issuer,subject,provider) VALUES ('44444444-0000-0000-0000-000000000059','https://accounts.google.com','sub-ci-059','google')"
[ "$(q "SELECT count(*) FROM external_identities")" = "1" ] \
  || { echo "::error::the fixture identity was not inserted, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/059_external_identities_down.sql 2>/tmp/r059.err; then
  echo "::error::rollback/059 succeeded while an account could only sign in through a provider. That is a permanent lockout no self-service flow can repair."
  exit 1
fi
grep -q "059-down REFUSING" /tmp/r059.err \
  || { echo "::error::rollback/059 failed for the wrong reason:"; cat /tmp/r059.err; exit 1; }
[ "$(q "SELECT to_regclass('public.external_identities') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/059 refused but still dropped the table"; exit 1; }
echo "✅ rollback/059 refuses while an account is federated-only, atomically"

${PSQL} -d ${DB} -q -c "INSERT INTO user_credentials (user_id,password_hash) VALUES ('44444444-0000-0000-0000-000000000059','\$2b\$12\$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLM')"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/059_external_identities_down.sql
[ "$(q "SELECT to_regclass('public.external_identities') IS NULL")" = "t" ] \
  || { echo "::error::rollback/059 left external_identities behind"; exit 1; }
echo "✅ rollback/059 reverses when every account has another way in"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/059_external_identities.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.external_identities'::regclass")" = "4" ] \
  || { echo "::error::059 re-applied with the wrong policy count"; exit 1; }
echo "✅ 059 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
