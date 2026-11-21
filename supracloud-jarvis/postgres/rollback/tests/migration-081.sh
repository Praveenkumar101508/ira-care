#!/usr/bin/env bash
#
# Migration 081 destroys legacy briefings, and says so
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# P12-03 finding G. `081` is one of only two migrations in this chain
# that DELETE data, and the only one that deletes customer content, so
# its destructive half is proved here on a disposable database rather
# than in the shared rollback cycle — where the table is empty by then
# and the delete would be a no-op that proves nothing.
#
# Three properties, in order of how badly each would fail:
#
# 1. the legacy rows are actually gone — a migration that reported a
# count and left the rows would be the worst possible outcome;
# 2. it WARNS while doing it. The line was a NOTICE first, which sits
# below the default `client_min_messages` in several tooling paths
# — so the one message an operator most needs was the one most
# likely to be filtered out;
# 3. the boundary it installs actually refuses an unbound write,
# which is what makes provenance mandatory rather than encouraged.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest081
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# Everything UP TO 081, so the table is in its pre-fix shape and a
# legacy row can be written the way the old generator wrote one.
for f in postgres/init.sql postgres/0[0-8][0-9]_*.sql; do
  case "$f" in *081_*) continue ;; esac
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
q() { ${PSQL} -d ${DB} -tAc "$1"; }

[ "$(q "SELECT count(*) FROM information_schema.columns
         WHERE table_name='briefings' AND column_name='tenant_id'")" = "0" ] \
  || { echo "::error::briefings already carries tenant_id before 081; this step is testing nothing"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO briefings (briefing_type, content) VALUES ('morning','LEGACY CONFIDENTIAL')"

# 1 + 2. Apply it, capturing stderr, where a WARNING goes.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -f postgres/081_briefings_tenancy.sql \
  > /tmp/081.out 2> /tmp/081.err
grep -q "IS ABOUT TO DELETE" /tmp/081.err \
  || { echo "::error::081 deleted customer content without warning about it"; cat /tmp/081.err; exit 1; }
grep -q "IRREVERSIBLY" /tmp/081.err \
  || { echo "::error::081's warning does not say the deletion is irreversible"; exit 1; }
[ "$(q "SELECT count(*) FROM briefings")" = "0" ] \
  || { echo "::error::081 reported a deletion and left the rows behind"; exit 1; }
echo "✅ 081 deleted the unprovable legacy rows and warned while doing it"

# 3. The boundary is real: NOT NULL defaults from unbound GUCs give
#    NULL, and NULL is refused. Loud at the write, not silent.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
     "INSERT INTO briefings (briefing_type, content) VALUES ('morning','no provenance')" 2>/dev/null; then
  echo "::error::081 accepted a briefing with no tenant or workspace"; exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — a briefing with no provenance is unrepresentable"

# And the rollback refuses on a multi-tenant deployment, because
# reversing this IS the disclosure.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenants (name, slug) VALUES ('g-a','g-a'), ('g-b','g-b')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/081_briefings_tenancy_down.sql 2>/dev/null; then
  echo "::error::rollback/081 re-opened finding G on a multi-tenant deployment"; exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 081-down refuses while a second tenant exists"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
