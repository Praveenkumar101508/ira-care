#!/usr/bin/env bash
#
# Migration 076 stops the runtime role editing its own disclosure
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# `api/routes/models.py` says there is no endpoint that changes a
# provider's disclosure, because "a retention claim is a fact about a
# third party's terms, not a user setting". True of the API, and it was
# the whole of the control: the database let `ira_app` INSERT, UPDATE
# and DELETE `model_providers`.
#
# 067 and 068 each revoked deliberately on their own tables. 029
# predates both, nothing revoked its table, and it kept the DML that
# 042's default privileges hand to everything `ira_schema_owner` owns.
#
# Built by the MIGRATOR, not by postgres, because 042's default
# privileges are the source of the grant and a database built by
# postgres cannot observe them — the same reason P7's staging script
# gives for its own role choice.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest076
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

for role in ira_app ira_worker; do
  for p in INSERT UPDATE DELETE TRUNCATE; do
    [ "$(q "SELECT has_table_privilege('${role}','model_providers','$p')")" = "f" ] \
      || { echo "::error::${role} holds $p on model_providers — the process that answers requests can edit the record of where it sent them"; exit 1; }
  done
  [ "$(q "SELECT has_table_privilege('${role}','model_providers','SELECT')")" = "t" ] \
    || { echo "::error::${role} cannot read model_providers; the Privacy Centre cannot render"; exit 1; }
done
echo "✅ the disclosure catalogue is readable and not writable by the runtime roles"

# The tables that were always right, checked beside the fix so a
# regression in either is visible in the same place.
for t in model_registry model_licence_register model_pull_allowlist; do
  [ "$(q "SELECT has_table_privilege('ira_app','$t','INSERT')")" = "f" ] \
    || { echo "::error::ira_app can INSERT into $t"; exit 1; }
done
echo "✅ the registry, the licence register and the allowlist stay read-only to ira_app"

# And what 076 must NOT have taken away. Metering is the application's
# job; a revoke here would stop all of it while looking like a
# tightening.
for t in model_usage usage_quotas; do
  [ "$(q "SELECT has_table_privilege('ira_app','$t','INSERT')")" = "t" ] \
    || { echo "::error::ira_app cannot write $t, so nothing is metered at all"; exit 1; }
done
echo "✅ metering still works"

# The rollback restores the grant, and re-applying takes it away
# again. There is nothing to refuse over — no row records a human
# decision here — so unlike 069/070/075 this one composes freely.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/076_provider_disclosure_is_not_writable_down.sql
[ "$(q "SELECT has_table_privilege('ira_app','model_providers','INSERT')")" = "t" ] \
  || { echo "::error::rollback/076 did not restore the grant it removed"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/076_provider_disclosure_is_not_writable.sql
[ "$(q "SELECT has_table_privilege('ira_app','model_providers','INSERT')")" = "f" ] \
  || { echo "::error::076 did not re-apply"; exit 1; }
echo "✅ 076 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
