#!/usr/bin/env bash
#
# Migration 064 expires an append-only log without making it deletable
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 062: the guarantee and the reversal both
# proved on a disposable database, so neither is assumed.
#
# What 064 must NOT do is the whole point. 061 made `auth_events`
# append-only for every role — no DELETE policy, DELETE revoked from
# `ira_app`, and a trigger that raises whoever asks — because the first
# thing an intruder does is remove the entry that shows them arriving.
# 064 adds a retention PATH, not a retention PERMISSION, and this step
# asserts all three of those defences are still standing afterwards.
# Checking them separately matters: a single "the delete failed"
# assertion cannot tell which one is holding, so it cannot notice two of
# them being removed.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest064
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

[ "$(q "SELECT count(*) FROM pg_policy WHERE polrelid='auth_events'::regclass AND polcmd='d'")" = "0" ] \
  || { echo "::error::064 added a DELETE policy to auth_events. It must add a path, not a permission."; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','auth_events','DELETE')")" = "f" ] \
  || { echo "::error::064 granted DELETE on auth_events to ira_app"; exit 1; }
[ "$(q "SELECT tgenabled FROM pg_trigger WHERE tgrelid='auth_events'::regclass AND tgname='auth_events_append_only_trg'")" = "O" ] \
  || { echo "::error::the auth_events append-only trigger is missing or disabled after 064"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef AND pg_get_functiondef(p.oid) ILIKE '%auth_events%' AND pg_get_functiondef(p.oid) ILIKE '%delete%'")" = "0" ] \
  || { echo "::error::a SECURITY DEFINER function can delete auth_events. That is the same capability wearing a different name."; exit 1; }
echo "✅ 064 left all three of 061's defences standing"

${PSQL} -d ${DB} -q -c "INSERT INTO auth_events (event, detail) VALUES ('log_expired','ci probe')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "INSERT INTO auth_events (event) VALUES ('anything_at_all')" 2>/tmp/c064.err; then
  echo "::error::064 widened the event CHECK into a free-text column"
  exit 1
fi
grep -q "auth_events_event_check" /tmp/c064.err \
  || { echo "::error::the invented-event insert failed for the wrong reason:"; cat /tmp/c064.err; exit 1; }
echo "✅ 064 accepts log_expired and still refuses an invented event"

# The rollback restores 061's sixteen-value CHECK as NOT VALID. With a
# `log_expired` row present a validated constraint CANNOT be added,
# and that row cannot be removed first because the table is
# append-only for this role too. Both halves are asserted: the
# validated form fails, the shipped rollback succeeds.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -c "ALTER TABLE auth_events DROP CONSTRAINT auth_events_event_check" \
     -c "ALTER TABLE auth_events ADD CONSTRAINT auth_events_event_check CHECK (event <> 'log_expired')" 2>/tmp/v064.err; then
  echo "::error::a validated narrow CHECK was accepted while a log_expired row exists. The NOT VALID in rollback/064 is then unexplained — check the probe."
  exit 1
fi
grep -q "is violated by some row" /tmp/v064.err \
  || { echo "::error::the validated-constraint probe failed for the wrong reason:"; cat /tmp/v064.err; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/064_auth_event_expiry_down.sql
[ "$(q "SELECT count(*) FROM pg_constraint WHERE conrelid='auth_events'::regclass AND conname='auth_events_event_check'")" = "1" ] \
  || { echo "::error::rollback/064 left auth_events without an event CHECK"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname='auth_events_created_idx'")" = "0" ] \
  || { echo "::error::rollback/064 left its index behind"; exit 1; }
[ "$(q "SELECT tgenabled FROM pg_trigger WHERE tgrelid='auth_events'::regclass AND tgname='auth_events_append_only_trg'")" = "O" ] \
  || { echo "::error::rollback/064 left the append-only trigger disabled"; exit 1; }
echo "✅ rollback/064 reverses without needing audit history deleted first"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/064_auth_event_expiry.sql
${PSQL} -d ${DB} -q -c "INSERT INTO auth_events (event, detail) VALUES ('log_expired','recompose probe')"
[ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname='auth_events_created_idx'")" = "1" ] \
  || { echo "::error::064 re-applied without its index"; exit 1; }
echo "✅ 064 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
