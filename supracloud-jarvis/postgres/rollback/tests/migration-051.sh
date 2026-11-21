#!/usr/bin/env bash
#
# Migration 051 reverses, and refuses to, each where it is real
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 051 gives `notifications` two foreign keys to `workspaces`, so it has
# to come off before 033's rollback can drop that table. But
# `rollback/051` REFUSES while the deployment holds more than one
# tenant, and by the time the cycle below runs the suites above have
# created several thousand. That refusal is correct and stays: reversing
# 051 strips the tenancy columns and RLS from `notifications` for every
# FUTURE row as well as the existing ones, which is finding 3C-10
# re-opened rather than a rollback.
#
# So the script has two real behaviours and they are verified where each
# one is true: the reversal here, on a single-tenant database of its
# own, and the refusal in the multi-tenant cycle below. Verifying only
# the refusal would leave the reversal a rollback plan rather than a
# rollback, which is exactly what these steps exist to prevent.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest051
PSQL="psql -h localhost -U postgres"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
# Roles are cluster-wide and already exist from the provisioning step
# above; this grants them on the new database and installs the
# extensions. Applied as the migrator for the same reason the main
# chain is — a superuser bypasses RLS and would mask a backfill that
# matched nothing.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/provisioning/001_database_roles.sql
( cd ira && python -m scripts.migrate apply \
    --dsn "postgresql://ira_migrator:ira_migrator@localhost:5432/${DB}" )

q()    { ${PSQL} -d ${DB} -tAc "$1"; }
cols() { q "SELECT count(*) FROM information_schema.columns
             WHERE table_name='notifications'
               AND column_name IN ('tenant_id','workspace_id')"; }
pols() { q "SELECT count(*) FROM pg_policy
             WHERE polrelid='public.notifications'::regclass"; }

if [ "$(cols)" != "2" ] || [ "$(pols)" != "4" ]; then
  echo "::error::051 is not applied on the fresh chain — nothing below tests anything"
  exit 1
fi

# ── The refusal, and that it is atomic ──────────────────────────────
q "INSERT INTO tenants (id, name, slug)
   VALUES ('00000000-0000-0000-0000-0000000000aa','ci-probe','ci-probe')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/051_notifications_tenancy_down.sql 2>/tmp/r051.err; then
  echo "::error::rollback/051 succeeded with two tenants. It must refuse — reversing it re-opens 3C-10."
  exit 1
fi
if ! grep -q "051-down REFUSING" /tmp/r051.err; then
  echo "::error::rollback/051 failed for the wrong reason:"; cat /tmp/r051.err; exit 1
fi
# A refusal that had already dropped half the objects would be worse
# than either end state.
if [ "$(cols)" != "2" ] || [ "$(pols)" != "4" ]; then
  echo "::error::rollback/051 refused but did not roll back cleanly"
  exit 1
fi
echo "✅ rollback/051 refuses on a multi-tenant database, atomically"

# ── The reversal ───────────────────────────────────────────────────
q "DELETE FROM tenants WHERE slug='ci-probe'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
  -f postgres/rollback/051_notifications_tenancy_down.sql
if [ "$(cols)" != "0" ] || [ "$(pols)" != "0" ]; then
  echo "::error::rollback/051 left $(cols) column(s) and $(pols) policy/policies behind"
  exit 1
fi
echo "✅ rollback/051 reverses on a single-tenant database"

# ── …and the two compose ───────────────────────────────────────────
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/051_notifications_tenancy.sql
if [ "$(cols)" != "2" ] || [ "$(pols)" != "4" ]; then
  echo "::error::051 did not re-apply over its own rollback"
  exit 1
fi
if [ "$(q "SELECT relforcerowsecurity FROM pg_class
            WHERE oid='public.notifications'::regclass")" != "t" ]; then
  echo "::error::FORCE ROW LEVEL SECURITY was not restored on notifications"
  exit 1
fi
echo "✅ 051 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
