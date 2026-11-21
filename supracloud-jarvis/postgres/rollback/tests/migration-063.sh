#!/usr/bin/env bash
#
# Migration 063 refuses to drop the only copy of a secret
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# The contract stage. Its guard is the most consequential in the chain:
# `secret` and `embedding` are the ONLY copy of a second factor and a
# voiceprint for any row that is not encrypted, and DROP COLUMN does not
# ask twice.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest063
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

# 063 is NOT in the chain — it lives in `postgres/contract/` because
# dropping the only copy of a secret is an operator decision, on the
# same grounds that keep `provisioning/` outside the runner. Applied
# here by hand, which is also how an operator runs it. A fresh chain
# has no rows, so both gates are ready.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/contract/063_contract_stage.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='secret'")" = "0" ] \
  || { echo "::error::totp_secrets.secret survived 063"; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "0" ] \
  || { echo "::error::voice_profiles.embedding survived 063"; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='template_ciphertext'")" = "1" ] \
  || { echo "::error::063 destroyed the ciphertext it was completing"; exit 1; }
echo "✅ 063: the plaintext is gone and the ciphertext is not"

# Put the columns back and plant an unencrypted row. This is the case
# the guard exists for, and it is proved rather than assumed.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/contract/rollback/063_contract_stage_down.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
  || { echo "::error::rollback/063 did not restore the plaintext column"; exit 1; }
echo "✅ rollback/063 restores the columns"

${PSQL} -d ${DB} -q -c "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('66666666-0000-0000-0000-000000000063','V','password','active')"
${PSQL} -d ${DB} -q -c "INSERT INTO voice_profiles (owner_name,embedding,user_id,migration_status) VALUES ('unmigrated','[0.9]','66666666-0000-0000-0000-000000000063','PLAINTEXT')"
[ "$(q "SELECT contract_ready FROM voice_migration_status")" = "f" ] \
  || { echo "::error::the fixture did not make the gate false, so the refusal below would prove nothing"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/contract/063_contract_stage.sql 2>/tmp/g063.err; then
  echo "::error::063 dropped the plaintext while a voiceprint was still unencrypted. That template is destroyed."
  exit 1
fi
grep -q "063 REFUSING" /tmp/g063.err \
  || { echo "::error::063 failed for the wrong reason:"; cat /tmp/g063.err; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
  || { echo "::error::063 refused but still dropped the column"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.voice_profiles'::regclass")" = "t" ] \
  || { echo "::error::063 refused but left voice_profiles without FORCE RLS — its gate lifts FORCE to read and must put it back"; exit 1; }
echo "✅ 063 refuses while a template is still plaintext, atomically"

# Encrypt it, and the same migration now proceeds.
${PSQL} -d ${DB} -q -c "UPDATE voice_profiles SET migration_status='ENCRYPTED', template_ciphertext='\\xaa'::bytea, template_nonce='\\xbb'::bytea, wrapped_dek='\\xcc'::bytea, wrap_nonce='\\xdd'::bytea, kek_id='k', encryption_key_version=1, encrypted_at=NOW(), embedding='' WHERE owner_name='unmigrated'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/contract/063_contract_stage.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "0" ] \
  || { echo "::error::063 did not apply once every template was encrypted"; exit 1; }
[ "$(q "SELECT count(*) FROM voice_profiles WHERE template_ciphertext IS NOT NULL")" = "1" ] \
  || { echo "::error::063 took the ciphertext with it"; exit 1; }
echo "✅ 063 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
