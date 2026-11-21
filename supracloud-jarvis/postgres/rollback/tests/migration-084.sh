#!/usr/bin/env bash
#
# Migration 084 widens the realtime end-reason vocabulary
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# P14-01b finding X. `084` adds one value — `unknown` — to the
# controlled vocabulary `027` put on `realtime_sessions.end_reason`.
#
# Same shape as the `083` step below and for the same reason: the
# negative control needs a database the shared cycle must not be left
# holding. The properties proved here are
#
# 1. `unknown` is genuinely REFUSED before 084 and genuinely accepted
# after. Without the first half this step would pass against a
# chain where 027 never constrained the column at all;
# 2. the rollback refuses while a row records the value, and — unlike
# 083 — the blocking row can then be cleared, so the guard is a
# guard and not a one-way door. That difference is asserted rather
# than asserted-about: `realtime_sessions` is not append-only.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest084
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# Everything UP TO 084, so the vocabulary is in its pre-fix shape.
for f in postgres/init.sql postgres/0[0-8][0-9]_*.sql; do
  case "$f" in *084_*) continue ;; esac
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
# A tenant to hang the session off: `realtime_sessions.tenant_id` is
# NOT NULL and references `tenants`, so the CHECK cannot be probed
# without one. `023` seeds the private tenant unconditionally, so this
# uses that rather than inserting a fixture whose required columns
# would have to be kept in step with every later tenants migration.
TENANT=e91aceeb-89fb-5ad2-b43c-4eee2bad079c
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM tenants WHERE id = '${TENANT}'" | grep -q 1 \
  || { echo "::error::023's seeded private tenant is missing; this step has nothing to attach a session to"; exit 1; }
row() {
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO realtime_sessions
         (tenant_id, state, end_reason, ended_at)
     VALUES ('${TENANT}', 'failed', 'unknown', NOW())"
}

# 1a. The negative control. If this SUCCEEDS, 027's constraint is not
#     on this database and everything below proves nothing.
if row 2>/dev/null; then
  echo "::error::realtime_sessions accepted end_reason='unknown' BEFORE 084; 027's vocabulary is not in force and this step is testing nothing"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 'unknown' is rejected before 084"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/084_realtime_unknown_end_reason.sql

# 2. It reverses cleanly while no row records the value — proved
#    BEFORE planting one, as 083 learned to do.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
  -f postgres/rollback/084_realtime_unknown_end_reason_down.sql
if row 2>/dev/null; then
  echo "::error::rollback/084 ran and 'unknown' is still accepted"; exit 1
fi
echo "✅ 084 reverses cleanly while nothing has recorded the value"

# 3. Re-apply, and NOW the value is accepted. Together with 1a that is
#    the full sequence: refused, applied, reversed, refused again,
#    re-applied, accepted.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/084_realtime_unknown_end_reason.sql
row
echo "✅ 084 admits 'unknown' to the vocabulary, and only after it is applied"

# 4. The rollback refuses while the row exists.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/084_realtime_unknown_end_reason_down.sql 2>/dev/null; then
  echo "::error::rollback/084 narrowed the vocabulary while a row still recorded 'unknown'"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 084-down will not invalidate existing rows"

# 5. …and the refusal IS actionable, which is the one place this
#    differs from 083. `rollback/084` tells an operator the rows can
#    be reclassified or aged out; if that were false the message would
#    be worse than no message. So it is executed.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "DELETE FROM realtime_sessions WHERE end_reason = 'unknown'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
  -f postgres/rollback/084_realtime_unknown_end_reason_down.sql
echo "✅ 084's guard is a guard, not a one-way door — the named rows can be cleared"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
