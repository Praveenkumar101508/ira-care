#!/usr/bin/env bash
#
# Migration 061 refuses to delete the record of how accounts were accessed
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 060: refusal and reversal both proved on a
# disposable database, so neither is assumed.
#
# The refusal being proved here is BLUNTER than every other one in this
# chain, and deliberately. The others refuse on the subset whose loss
# costs something — an accepted invitation, a live credential, an
# outstanding reset link — and let the harmless rows through. This one
# refuses on the FIRST row, because the table holds history rather than
# state, and history has no harmless subset: which rows an investigation
# needs is decided later, by an event that has not happened yet.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest061
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q() { ${PSQL} -d ${DB} -tAc "$1" | head -1; }

# Exactly three policies, and the missing fourth is the point: with
# FORCE RLS and no DELETE policy, no row is deletable through the
# application whatever the trigger says.
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass")" = "4" ] \
  || { echo "::error::auth_events does not have exactly 4 policies after 061"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass AND polcmd='d'")" = "0" ] \
  || { echo "::error::auth_events has a DELETE policy. The first thing an intruder does is remove the entry that shows them arriving."; exit 1; }
# The SELECT policy must key on the SUBJECT, never on the tenant. A
# tenant-wide read would hand every administrator their members'
# sign-in history, which is the movement log 057 refused.
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass AND polcmd='r' AND coalesce(pg_get_expr(polqual,polrelid),'') NOT LIKE '%app.user_id%'")" = "0" ] \
  || { echo "::error::the auth_events SELECT policy does not key on app.user_id"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','auth_events','DELETE')")" = "f" ] \
  || { echo "::error::ira_app holds DELETE on auth_events"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_worker','auth_events','SELECT')")" = "f" ] \
  || { echo "::error::ira_worker can read auth_events — a job that can read this can reconstruct who signed in from where"; exit 1; }
echo "✅ 061: the authentication trail is append-only and the subject's own"

${PSQL} -d ${DB} -q -c "INSERT INTO auth_events (event, detail) VALUES ('sign_in_refused','ci fixture')"
[ "$(q "SELECT count(*) FROM auth_events")" = "1" ] \
  || { echo "::error::the fixture event was not inserted, so the refusal below would prove nothing"; exit 1; }

# The trigger, proved as the OWNER — the role a rollback runs as, and
# therefore the only one whose refusal matters here. `ira_app` is
# refused a step earlier by the missing grant, which would mask this.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "DELETE FROM auth_events" 2>/tmp/d061.err; then
  echo "::error::auth_events rows are deletable. A log that can be deleted is not evidence."
  exit 1
fi
grep -q "not deletable" /tmp/d061.err \
  || { echo "::error::the auth_events DELETE failed for the wrong reason:"; cat /tmp/d061.err; exit 1; }
# Reassignment is worse than deletion — a deleted row is visibly
# missing — so it is refused separately and checked separately.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "UPDATE auth_events SET event='sign_in_succeeded'" 2>/tmp/u061.err; then
  echo "::error::auth_events events are rewritable. The evidence is what happened, and it does not change."
  exit 1
fi
grep -q "append-only" /tmp/u061.err \
  || { echo "::error::the auth_events UPDATE failed for the wrong reason:"; cat /tmp/u061.err; exit 1; }
echo "✅ 061 refuses deletion and rewriting"

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/061_auth_events_down.sql 2>/tmp/r061.err; then
  echo "::error::rollback/061 succeeded while the authentication log held events. Reversing it silently is how the record of an intrusion disappears along with the schema change that was blamed for it."
  exit 1
fi
grep -q "061-down REFUSING" /tmp/r061.err \
  || { echo "::error::rollback/061 failed for the wrong reason:"; cat /tmp/r061.err; exit 1; }
[ "$(q "SELECT to_regclass('public.auth_events') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/061 refused but still dropped auth_events"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.auth_events'::regclass")" = "t" ] \
  || { echo "::error::rollback/061 refused but left auth_events without FORCE RLS — its guard lifts FORCE to count and must put it back"; exit 1; }
echo "✅ rollback/061 refuses while the log holds events, atomically"

# TRUNCATE, not DELETE: the trigger is FOR EACH ROW and TRUNCATE does
# not fire it. That is exactly the deliberate operator action the
# rollback's HINT describes — export the log, then truncate it.
${PSQL} -d ${DB} -q -c "TRUNCATE auth_events"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/061_auth_events_down.sql
[ "$(q "SELECT to_regclass('public.auth_events') IS NULL")" = "t" ] \
  || { echo "::error::rollback/061 left auth_events behind"; exit 1; }
# A rollback of an audit migration is most likely to take the OTHER
# audit tables with it, so that is asserted rather than assumed.
[ "$(q "SELECT to_regclass('public.biometric_audit') IS NOT NULL")" = "t" ] \
  || { echo "::error::rollback/061 destroyed biometric_audit"; exit 1; }
echo "✅ rollback/061 reverses when the log is empty"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/061_auth_events.sql
[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='public.auth_events'::regclass")" = "4" ] \
  || { echo "::error::061 re-applied with the wrong policy count"; exit 1; }
echo "✅ 061 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
