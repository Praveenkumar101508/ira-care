#!/usr/bin/env bash
#
# Migration 085 makes a redelivered webhook one event
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# P14-R3, FINDING AQ. `085` is the constraint the webhook intake relies
# on, and the intake cannot be trusted without it: `ON CONFLICT
# (tenant_id, event_type, dedupe_key)` with no matching index does not
# quietly insert twice, it RAISES — so a schema missing this index turns
# every booking webhook into a 500.
#
# Two properties, and the SECOND is the one a careless fix would break:
#
# 1. a repeated (tenant, type, key) is refused, which is what makes a
# provider redelivery a no-op instead of a second owner alert;
# 2. rows with a NULL `dedupe_key` are NOT constrained. The index is
# partial on purpose — a website lead form has no stable id, and a
# full index would make every un-keyed source collide with the
# first one ever stored. An index that forgot the WHERE clause
# passes property 1 perfectly and breaks lead intake completely.
#
# And the rollback is proved to be a guard rather than a one-way door,
# the distinction `084` established: re-applying over duplicate rows
# FAILS, and says so, and clearing them lets it through.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest085
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# Everything UP TO 085, so the table is in its pre-fix shape.
for f in postgres/init.sql postgres/0[0-8][0-9]_*.sql; do
  case "$f" in *085_*) continue ;; esac
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done

TENANT=e91aceeb-89fb-5ad2-b43c-4eee2bad079c
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM tenants WHERE id = '${TENANT}'" | grep -q 1 \
  || { echo "::error::023's seeded private tenant is missing; this step has nothing to attach an event to"; exit 1; }

event() {   # $1 = dedupe_key SQL literal
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO business_events
         (id, tenant_id, event_type, title, payload, status, dedupe_key)
     VALUES (gen_random_uuid(), '${TENANT}', 'booking', 'x', '{}', 'confirmed', $1)"
}

# 1a. The negative control. Before 085 the column does not exist, so
#     the intake has nothing to deduplicate ON — which is precisely
#     the state FINDING AQ was found in.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tAc \
     "SELECT 1 FROM information_schema.columns
       WHERE table_name='business_events' AND column_name='dedupe_key'" \
     | grep -q 1; then
  echo "::error::business_events already has dedupe_key BEFORE 085; this step is testing nothing"
  exit 1
fi
echo "✅ EXPECTED / PASS — no dedupe_key before 085"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/085_business_events_dedupe_key.sql

# 2. A repeated key is refused.
event "'calcom-1'"
if event "'calcom-1'" 2>/dev/null; then
  echo "::error::085 applied and the same booking id was stored twice — FINDING AQ is not fixed"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — a redelivered booking id is refused"

# 3. THE PARTIAL HALF. Un-keyed rows must still be free to repeat.
event "NULL"
event "NULL"
test "$(${PSQL} -d ${DB} -tAc "SELECT count(*) FROM business_events WHERE dedupe_key IS NULL")" = "2" \
  || { echo "::error::085's index constrained rows with NO dedupe key. It is not partial, and every un-keyed source (a website lead form) now collides with the first row ever stored"; exit 1; }
echo "✅ un-keyed events are NOT constrained — the index is partial, as lead intake requires"

# 4. It reverses, and the duplicate then goes in.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
  -f postgres/rollback/085_business_events_dedupe_key_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO business_events (id, tenant_id, event_type, title, payload, status)
   VALUES (gen_random_uuid(), '${TENANT}', 'booking', 'x', '{}', 'confirmed')"
echo "✅ 085 reverses — and the protection is what is lost, not a record"

# 5. Re-applying over DUPLICATES fails, and the failure is the honest
#    one: an index cannot be created over rows that violate it. This
#    is the guard-not-a-one-way-door property from 084, in the
#    direction that matters here.
# The column is added back BY HAND and the duplicates planted in it,
# because the rollback above dropped it — an UPDATE here would fail
# on "column does not exist" and the step would go red for a reason
# that has nothing to do with the property. (It did, the first time
# this step was written.) `085` is then run against a table that
# already holds violating rows, which is the real situation an
# operator re-applying after a rollback would be in.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "ALTER TABLE business_events ADD COLUMN dedupe_key TEXT"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "UPDATE business_events SET dedupe_key = 'dup' WHERE event_type = 'booking'"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/085_business_events_dedupe_key.sql 2>/dev/null; then
  echo "::error::085 created a UNIQUE index over rows that already violate it"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 085 will not claim uniqueness over duplicate rows"

# …and clearing them lets it through, so the refusal is actionable.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM business_events WHERE dedupe_key = 'dup'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/085_business_events_dedupe_key.sql
echo "✅ 085's refusal is actionable — the duplicate rows are what block it"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
