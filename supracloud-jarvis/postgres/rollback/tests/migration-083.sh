#!/usr/bin/env bash
#
# Migration 083 widens an audit vocabulary without rewriting it
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# P12-04 finding I. `083` adds one value — `consent_missing` — to the
# controlled vocabulary `041` put on `biometric_audit.failure_reason`.
#
# Two properties, proved here rather than in the shared cycle because
# the second one needs a row the shared database must not be left
# holding:
#
# 1. the value is genuinely accepted afterwards, and genuinely was not
# before. Without the second half this step would pass against a
# chain where 041 never constrained anything;
# 2. the rollback REFUSES while a row still records it. Narrowing the
# vocabulary under existing rows does not fail cleanly — it changes
# what an audit trail is allowed to say — and a guard nobody has
# executed is a comment.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest083
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# Everything UP TO 083, so the vocabulary is in its pre-fix shape.
for f in postgres/init.sql postgres/0[0-8][0-9]_*.sql; do
  case "$f" in *083_*) continue ;; esac
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
row() {
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
    "INSERT INTO biometric_audit
         (session_id, similarity, threshold, result, failure_reason)
     VALUES ('083-probe', 0, 0.75, false, 'consent_missing')"
}

# 1a. The negative control. If this SUCCEEDS, 041's constraint is not
#     on this database and everything below proves nothing.
if row 2>/dev/null; then
  echo "::error::biometric_audit accepted consent_missing BEFORE 083; 041's vocabulary is not in force and this step is testing nothing"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — consent_missing is rejected before 083"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/083_biometric_consent_reason.sql

# 2. It reverses cleanly while no row records the value. Proved BEFORE
#    planting one, and that ordering is the whole lesson of this step.
#
#    The first version deleted the probe row and re-tried the rollback,
#    to show the refusal was "a guard rather than a one-way door". CI
#    answered:
#
#      ERROR: biometric_audit is append-only: DELETE is refused
#
#    `041`'s trigger refuses DELETE and refuses any UPDATE that touches
#    `failure_reason`. So the door really does close: once a deployment
#    records `consent_missing`, 083 is permanent. The step now proves
#    reversal first, on the state where it is genuinely possible.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
  -f postgres/rollback/083_biometric_consent_reason_down.sql
if row 2>/dev/null; then
  echo "::error::rollback/083 ran and consent_missing is still accepted"; exit 1
fi
echo "✅ 083 reverses cleanly while nothing has recorded the value"

# 3. Re-apply — and NOW the value is accepted, which is 1b. It is
#    asserted here rather than immediately after the first apply
#    because planting the row is what closes the door in step 4, and
#    the reversal above has to happen while the door is still open.
#    Together with 1a this is the full sequence: refused, applied,
#    reversed, refused again, re-applied, accepted.
#
#    This is the value `_record_biometric_attempt` writes when an
#    enrolled voiceprint has no recorded consent. If the CHECK rejects
#    it the INSERT lands in a non-fatal `except` and the refused
#    attempt leaves no audit row at all.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/083_biometric_consent_reason.sql
row
echo "✅ 083 admits consent_missing to the vocabulary, and only after it is applied"

# 4. The rollback now refuses — for good.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/083_biometric_consent_reason_down.sql 2>/dev/null; then
  echo "::error::rollback/083 narrowed the vocabulary while a row still recorded consent_missing"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 083-down will not invalidate audit history"

# And the refusal is permanent, not a prompt to tidy up: the row it
# names cannot be removed. Asserting this is what stops a future
# operator — or a future edit to this step — from "fixing" the refusal
# with a DELETE that silently cannot work.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
     "DELETE FROM biometric_audit WHERE failure_reason = 'consent_missing'" 2>/dev/null; then
  echo "::error::biometric_audit permitted a DELETE; 041's append-only trigger is gone and 083's guard is now merely inconvenient"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — the blocking rows cannot be deleted either"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
