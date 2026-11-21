#!/usr/bin/env bash
#
# Migration 053 refuses to strand an encrypted second factor
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 053's rollback drops `user_mfa_keys`, which holds the ONLY material
# that can open an encrypted TOTP seed. Once a row is ENCRYPTED its
# plaintext has been cleared — that is the point of the migration — so
# dropping the keys is not a rollback: it permanently locks those users
# out of their own second factor with no recovery path.
#
# The cycle below clears the enrolments before peeling 053, so it proves
# the reversal and can never reach the refusal. This step is the other
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

DB=iratest053
PSQL="psql -h localhost -U postgres"
TA=11111111-1111-1111-1111-1111111111c3
UA=aaaaaaaa-0000-0000-0000-0000000000c3

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1"; }
q "INSERT INTO tenants (id,name,slug) VALUES ('${TA}','C3','c3')"
q "INSERT INTO users (id,display_name,auth_provider,status) VALUES ('${UA}','C3','local','active')"
# An ENCRYPTED row: no plaintext, ciphertext and nonce present. The
# bytes are not a real seal — the guard counts status, and what is
# being tested is the refusal, not the cryptography.
q "INSERT INTO totp_secrets (username,secret,secret_ciphertext,secret_nonce,encryption_key_version,encrypted_at,migration_status,enabled,user_id,tenant_id) VALUES ('c3-alice',NULL,'\\x00'::bytea,'\\x00'::bytea,1,NOW(),'ENCRYPTED',TRUE,'${UA}','${TA}')"
q "INSERT INTO user_mfa_keys (tenant_id,user_id,key_version,wrapped_dek,wrap_nonce,kek_id) VALUES ('${TA}','${UA}',1,'\\x00'::bytea,'\\x00'::bytea,'default')"

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/053_mfa_key_material_down.sql 2>/tmp/r053.err; then
  echo "::error::rollback/053 succeeded while a seed was encrypted. It must refuse — dropping user_mfa_keys destroys the only material that opens it, and the plaintext is already gone."
  exit 1
fi
if ! grep -q "053-down REFUSING" /tmp/r053.err; then
  echo "::error::rollback/053 failed for the wrong reason:"; cat /tmp/r053.err; exit 1
fi
# Atomic: the refusal must leave the schema exactly as it found it.
if [ "$(q "SELECT to_regclass('public.user_mfa_keys') IS NOT NULL")" != "t" ]; then
  echo "::error::rollback/053 refused but still dropped user_mfa_keys"; exit 1
fi
if [ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.totp_secrets'::regclass")" != "t" ]; then
  echo "::error::rollback/053 refused but left totp_secrets without FORCE RLS — the guard lifts it to count and must restore it"; exit 1
fi
echo "✅ rollback/053 refuses while a seed is encrypted, atomically"

# Now the legitimate case: nothing encrypted, so it reverses.
q "DELETE FROM totp_secrets"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/053_mfa_key_material_down.sql
if [ "$(q "SELECT to_regclass('public.user_mfa_keys') IS NULL")" != "t" ]; then
  echo "::error::rollback/053 left user_mfa_keys behind"; exit 1
fi
if [ "$(q "SELECT is_nullable FROM information_schema.columns WHERE table_name='totp_secrets' AND column_name='secret'")" != "NO" ]; then
  echo "::error::rollback/053 did not restore secret NOT NULL"; exit 1
fi
echo "✅ rollback/053 reverses when nothing is encrypted"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/053_mfa_key_material.sql
if [ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.user_mfa_keys'::regclass")" != "4" ]; then
  echo "::error::053 re-applied with the wrong policy count"; exit 1
fi
echo "✅ 053 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
