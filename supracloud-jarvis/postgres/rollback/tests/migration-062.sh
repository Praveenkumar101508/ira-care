#!/usr/bin/env bash
#
# Migration 062 refuses to make a biometric template unopenable
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 061: refusal and reversal both proved on a
# disposable database, so neither is assumed.
#
# What this guard protects is unusual, and it is why the refusal is on
# the FIRST wrapped key rather than on a subset. `wrapped_dek` IS the
# key that opens a voice template. Dropping it destroys the only route
# to the plaintext — a crypto-shred of every enrolment at once — and it
# SUCCEEDS: the row survives, the ciphertext survives, migration_status
# still reads ENCRYPTED, and nothing reports a loss until somebody tries
# to authenticate.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest062
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

for C in template_nonce wrapped_dek wrap_nonce kek_id; do
  [ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='${C}'")" = "1" ] \
    || { echo "::error::voice_profiles.${C} is missing after 062"; exit 1; }
done
# 062 adds capacity. It must NOT perform the contract stage.
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
  || { echo "::error::062 dropped the plaintext template column. Dropping it is 063, and only once contract_ready is true."; exit 1; }
echo "✅ 062: a per-record key has somewhere to live, and the plaintext is untouched"

${PSQL} -d ${DB} -q -c "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('55555555-0000-0000-0000-000000000062','V','password','active')"
${PSQL} -d ${DB} -q -c "INSERT INTO voice_profiles (owner_name,embedding,user_id,template_ciphertext,template_nonce,wrapped_dek,wrap_nonce,kek_id,encryption_key_version,encrypted_at,migration_status) VALUES ('v','plain','55555555-0000-0000-0000-000000000062','\\xaa'::bytea,'\\xbb'::bytea,'\\xcc'::bytea,'\\xdd'::bytea,'kek-ci',1,NOW(),'ENCRYPTED')"
[ "$(q "SELECT count(*) FROM voice_profiles WHERE wrapped_dek IS NOT NULL")" = "1" ] \
  || { echo "::error::the fixture enrolment was not inserted, so the refusal below would prove nothing"; exit 1; }

# An ENCRYPTED row with no key is ciphertext nobody can open, and 063
# would drop the last plaintext copy of it. Unrepresentable, not
# merely discouraged.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO voice_profiles (owner_name,embedding,migration_status,template_ciphertext,encryption_key_version,encrypted_at) VALUES ('x','p','ENCRYPTED','\\xaa'::bytea,1,NOW())" 2>/tmp/c062.err; then
  echo "::error::an ENCRYPTED voice_profiles row with no wrapped key was accepted"
  exit 1
fi
grep -q "encrypted_is_complete" /tmp/c062.err \
  || { echo "::error::the keyless-ENCRYPTED insert failed for the wrong reason:"; cat /tmp/c062.err; exit 1; }
# A shred that leaves the key behind shredded nothing.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE voice_profiles SET deletion_status='crypto_shredded' WHERE kek_id='kek-ci'" 2>/tmp/s062.err; then
  echo "::error::a crypto_shredded row kept its wrapped key"
  exit 1
fi
grep -q "shredded_holds_no_key" /tmp/s062.err \
  || { echo "::error::the shred guard failed for the wrong reason:"; cat /tmp/s062.err; exit 1; }
echo "✅ 062 refuses an unopenable row and a shred that kept its key"

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/062_voiceprint_encryption_down.sql 2>/tmp/r062.err; then
  echo "::error::rollback/062 succeeded while an enrolment held a wrapped key. That is a silent crypto-shred of every voice template, performed by a schema change."
  exit 1
fi
grep -q "062-down REFUSING" /tmp/r062.err \
  || { echo "::error::rollback/062 failed for the wrong reason:"; cat /tmp/r062.err; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='wrapped_dek'")" = "1" ] \
  || { echo "::error::rollback/062 refused but still dropped the key columns"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.voice_profiles'::regclass")" = "t" ] \
  || { echo "::error::rollback/062 refused but left voice_profiles without FORCE RLS — its guard lifts FORCE to count and must put it back"; exit 1; }
echo "✅ rollback/062 refuses while an enrolment holds a wrapped key, atomically"

${PSQL} -d ${DB} -q -c "UPDATE voice_profiles SET wrapped_dek=NULL, wrap_nonce=NULL, template_nonce=NULL, migration_status='PLAINTEXT', template_ciphertext=NULL, encryption_key_version=NULL, encrypted_at=NULL"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/062_voiceprint_encryption_down.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='wrapped_dek'")" = "0" ] \
  || { echo "::error::rollback/062 left the key columns behind"; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='embedding'")" = "1" ] \
  || { echo "::error::rollback/062 destroyed the plaintext template"; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_migration_status'")" = "7" ] \
  || { echo "::error::rollback/062 did not restore 037's contract-gate view"; exit 1; }
echo "✅ rollback/062 reverses when no enrolment holds a key"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/062_voiceprint_encryption.sql
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='voice_profiles' AND column_name='wrapped_dek'")" = "1" ] \
  || { echo "::error::062 re-applied without its key columns"; exit 1; }
echo "✅ 062 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
