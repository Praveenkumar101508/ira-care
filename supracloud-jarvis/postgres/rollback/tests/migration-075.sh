#!/usr/bin/env bash
#
# Migration 075 lets the register record the refusals it was dropping
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# `029` closed `model_usage.outcome` to four values and the gateway has
# written a fifth — `refused_policy` — since Phase 5. `record_usage` is
# fail-soft, so every one of those INSERTs raised, logged a warning and
# vanished. The two events an operator most wants ("we blocked an
# unapproved model", "a provider substituted one") have never been in
# the usage table.
#
# This step proves the widened vocabulary accepts what the code writes,
# and that the invariants which must NOT widen did not.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest075
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done
T=$(q "SELECT id FROM tenants LIMIT 1")
[ -n "${T}" ] || { echo "::error::no tenant to test model_usage with"; exit 1; }

ins() {
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO model_usage (tenant_id, provider_id, locality, tier, sensitivity, privacy_mode, outcome$3) VALUES ('${T}','ollama-local','local','fast','normal','local_first','$1'$4)" 2>/tmp/u075.err
}

for outcome in ok refused_privacy refused_quota error refused_policy abandoned; do
  ins "${outcome}" "" "" "" \
    || { echo "::error::model_usage rejects outcome '${outcome}', which the gateway writes"; cat /tmp/u075.err; exit 1; }
done
echo "✅ every outcome the gateway writes is representable"

# The vocabulary grew by exactly two. A step that only checked the new
# values would pass just as happily on a CHECK that had been dropped.
n=$(q "SELECT length(pg_get_constraintdef(oid)) - length(replace(pg_get_constraintdef(oid), '''', '')) FROM pg_constraint WHERE conname='model_usage_outcome_check'")
[ "${n}" = "12" ] \
  || { echo "::error::model_usage.outcome accepts $((n/2)) values, expected exactly 6"; exit 1; }
echo "✅ the vocabulary grew by exactly two"

# A refusal that consumed tokens is still impossible — including the
# newly-accepted one.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO model_usage (tenant_id, provider_id, locality, tier, sensitivity, privacy_mode, outcome, prompt_tokens) VALUES ('${T}','ollama-local','local','fast','normal','local_first','refused_policy',10)" 2>/dev/null; then
  echo "::error::a policy refusal was allowed to record consumed tokens"; exit 1
fi
echo "✅ a policy refusal still cannot claim to have consumed tokens"

# …and an abandoned stream CAN, because the provider really did work.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO model_usage (tenant_id, provider_id, locality, tier, sensitivity, privacy_mode, outcome, prompt_tokens) VALUES ('${T}','ollama-local','local','fast','normal','local_first','abandoned',10)" \
  || { echo "::error::an abandoned stream cannot record the work it consumed"; exit 1; }
echo "✅ an abandoned stream records the work it consumed"

# The two guarantees 029 wrote into the billing record are untouched.
for c in credentials_never_left_the_machine local_only_stayed_local; do
  [ "$(q "SELECT count(*) FROM pg_constraint WHERE conname='${c}'")" = "1" ] \
    || { echo "::error::${c} did not survive 075"; exit 1; }
done
echo "✅ the credential and local_only guarantees are unchanged"

# The rollback refuses while a row carries one of the new values.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/075_usage_outcomes_down.sql 2>/tmp/r075.err; then
  echo "::error::rollback/075 discarded usage rows recording a refusal"; exit 1
fi
grep -q "075-down REFUSING" /tmp/r075.err \
  || { echo "::error::rollback/075 failed for the wrong reason:"; cat /tmp/r075.err; exit 1; }
echo "✅ rollback/075 refuses to discard the record of a refusal"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "DELETE FROM model_usage WHERE outcome IN ('refused_policy','abandoned')"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/075_usage_outcomes_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/075_usage_outcomes.sql
echo "✅ 075 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
