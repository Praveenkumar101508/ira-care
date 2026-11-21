#!/usr/bin/env bash
#
# Migration 066 records that background work happened
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 065.
#
# The rollback REFUSES while any run is unfinished, because dropping the
# table then destroys the only record that the pass began — and that is
# exactly the case where somebody needs it, since "did last night's
# retention finish" becomes unanswerable.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest066
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

[ "$(q "SELECT to_regclass('public.job_runs') IS NOT NULL")" = "t" ] \
  || { echo "::error::066 did not create job_runs"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.job_runs'::regclass")" = "t" ] \
  || { echo "::error::job_runs does not FORCE row level security"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','job_runs','DELETE')")" = "f" ] \
  || { echo "::error::ira_app can DELETE job history; truncating it is an operator action"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_worker','job_runs','DELETE')")" = "f" ] \
  || { echo "::error::ira_worker can DELETE job history"; exit 1; }
echo "✅ 066: job_runs exists, forces RLS, and neither role can delete history"

# A run must not be able to claim it finished while still 'running'.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO job_runs (job,runner,status,finished_at) VALUES ('retention','ci/1','succeeded',NULL)" 2>/tmp/c066.err; then
  echo "::error::a completed run was accepted with no finished_at, so a pass that died is indistinguishable from one that succeeded"
  exit 1
fi
grep -q "job_runs_finished_when_not_running" /tmp/c066.err \
  || { echo "::error::the unfinished-run insert failed for the wrong reason:"; cat /tmp/c066.err; exit 1; }
echo "✅ 066 refuses a finished status with no finish time"

# The rollback refuses while a run is in flight.
${PSQL} -d ${DB} -q -c "INSERT INTO job_runs (job,runner) VALUES ('retention','ci/2')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/066_job_runs_down.sql 2>/tmp/r066.err; then
  echo "::error::rollback/066 dropped the table while a run had not finished, destroying the only record that it started"
  exit 1
fi
grep -q "066-down REFUSING" /tmp/r066.err \
  || { echo "::error::rollback/066 failed for the wrong reason:"; cat /tmp/r066.err; exit 1; }
echo "✅ rollback/066 refuses while a run is in flight"

${PSQL} -d ${DB} -q -c "UPDATE job_runs SET status='interrupted', finished_at=NOW() WHERE finished_at IS NULL"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/066_job_runs_down.sql
[ "$(q "SELECT to_regclass('public.job_runs') IS NULL")" = "t" ] \
  || { echo "::error::rollback/066 refused but left the table"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/066_job_runs.sql
[ "$(q "SELECT to_regclass('public.job_runs') IS NOT NULL")" = "t" ] \
  || { echo "::error::066 did not re-apply"; exit 1; }
echo "✅ 066 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
