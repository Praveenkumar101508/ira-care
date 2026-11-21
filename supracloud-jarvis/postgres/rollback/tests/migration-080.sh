#!/usr/bin/env bash
#
# Migration 080 keeps the payment mapping out of the runtime's hands
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 080 attributes an inbound payment to a tenant. The property that earns
# a disposable database is the one an earlier draft got WRONG.
#
# `tenants` is the tenancy ROOT: no RLS, no policies, and `ira_app` holds
# table-level UPDATE. It must be readable before scope exists — that is
# what a root is for, and attribution depends on it — but that visibility
# must not become the ability to write the mapping. A first version put
# the column there with nothing guarding it, and the runtime could have
# pointed any provider customer at any tenant.
#
# The guard is a TRIGGER, not column-level privileges, and this step
# proves the reason: it re-applies 042 — whose `GRANT … ON ALL TABLES`
# supersedes column grants — and checks the refusal STILL fires.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest080
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
q() { ${PSQL} -d ${DB} -tAc "$1"; }
asapp() { PGPASSWORD=ira_app psql -h localhost -U ira_app -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }

# ── the shape that keeps the intake table honestly unscoped ───────
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='payment_events' AND column_name='tenant_id'")" = "0" ] \
  || { echo "::error::payment_events carries tenant_id; it would be a scoped table with no protection"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','payment_events','DELETE')")" = "f" ] \
  || { echo "::error::ira_app can DELETE a payment event"; exit 1; }
[ "$(q "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef")" = "0" ] \
  || { echo "::error::080 introduced a SECURITY DEFINER function"; exit 1; }

# ── a tenant with a mapping, set by an operator ───────────────────
TENANT="$(${PSQL} -d ${DB} -tAqc \
  "INSERT INTO tenants (name, slug, provider_customer_ref)
   VALUES ('p11-080','p11-080','cus_ci') RETURNING id")"

# ── the runtime cannot re-point it, cannot squat a free one, and
#    cannot create a tenant that arrives already carrying one ──────
if asapp "UPDATE tenants SET provider_customer_ref='cus_hijack' WHERE id='${TENANT}'" 2>/dev/null; then
  echo "::error::the runtime re-pointed a tenant's payment mapping"; exit 1
fi
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenants (name, slug) VALUES ('p11-080b','p11-080b')"
if asapp "UPDATE tenants SET provider_customer_ref='cus_squat' WHERE slug='p11-080b'" 2>/dev/null; then
  echo "::error::the runtime claimed an unused provider customer reference (squatting)"; exit 1
fi
if asapp "INSERT INTO tenants (name, slug, provider_customer_ref) VALUES ('p11-080c','p11-080c','cus_sneak')" 2>/dev/null; then
  echo "::error::the runtime created a tenant already carrying a payment mapping"; exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — the runtime cannot write the payment mapping"

# ── …and the guard refuses ONLY that. A guard that refused every
#    write to `tenants` would pass every check above and break the
#    product, which is the failure mode "maximally safe" hides.
asapp "UPDATE tenants SET name='renamed by ci' WHERE id='${TENANT}'"
[ "$(q "SELECT provider_customer_ref FROM tenants WHERE id='${TENANT}'")" = "cus_ci" ] \
  || { echo "::error::the mapping changed during an unrelated update"; exit 1; }

# ── the read attribution depends on, with NOTHING bound ───────────
FOUND="$(PGPASSWORD=ira_app psql -h localhost -U ira_app -d ${DB} -tAqc \
  "SELECT id FROM tenants WHERE provider_customer_ref = 'cus_ci'")"
[ "${FOUND}" = "${TENANT}" ] \
  || { echo "::error::the unbound intake path cannot resolve the mapping; every payment would be unattributable"; exit 1; }

# ── the guard cannot be switched off by the guarded party ─────────
if asapp "ALTER TABLE tenants DISABLE TRIGGER tenants_provider_customer_guard" 2>/dev/null; then
  echo "::error::the runtime disabled the guard"; exit 1
fi
if asapp "SET session_replication_role = replica" 2>/dev/null; then
  echo "::error::the runtime can set session_replication_role and bypass every trigger"; exit 1
fi

# ── THE ONE THIS STEP EXISTS FOR: 042 cannot undo a trigger ───────
# A column-privilege design would be silently unprotected right here.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/042_role_ownership_separation.sql >/dev/null
if asapp "UPDATE tenants SET provider_customer_ref='cus_after042' WHERE id='${TENANT}'" 2>/dev/null; then
  echo "::error::re-applying 042 restored the runtime's ability to write the payment mapping"; exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 042's blanket GRANT does not undo the guard"

# ── used: the rollback declines once anything has been acted on ───
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO payment_events (provider, provider_event_id, event_type,
                               customer_ref, payload, outcome, processed_at)
   VALUES ('test','evt_ci','payment.succeeded','cus_ci','{}'::jsonb,
           'applied', NOW())"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/080_payment_events_down.sql 2>/dev/null; then
  echo "::error::rollback/080 dropped the intake ledger after an event was acted on"; exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 080-down refuses once an event has been acted on"

# ── unused, it composes ──────────────────────────────────────────
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "SET session_replication_role = replica; DELETE FROM payment_events"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/080_payment_events_down.sql
[ "$(q "SELECT to_regclass('public.payment_events') IS NULL")" = "t" ] \
  || { echo "::error::rollback/080 left its table behind"; exit 1; }
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='tenants' AND column_name='provider_customer_ref'")" = "0" ] \
  || { echo "::error::rollback/080 left provider_customer_ref on tenants"; exit 1; }
# …and the GUARD came off with it. Dropping the column while leaving
# the trigger behind puts a landmine on the TENANCY ROOT: the trigger
# refers to a field that no longer exists, so every INSERT or UPDATE
# on `tenants` fails with `record "new" has no field ...` and the
# deployment cannot create a tenant at all. The first version of
# `rollback/080` did exactly that. The existence check is not enough
# on its own — WRITE to the table, because that is the operation that
# actually breaks.
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='tenants_provider_customer_guard' AND NOT tgisinternal")" = "0" ] \
  || { echo "::error::rollback/080 left the guard trigger on tenants, referring to a column it just dropped"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenants (name, slug) VALUES ('p11-080-after','p11-080-after')" \
  || { echo "::error::tenants is unwritable after rollback/080 — the rollback for a billing migration broke sign-up"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/080_payment_events.sql
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='tenants_provider_customer_guard' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::080 did not re-apply its guard"; exit 1; }

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
echo "✅ 080 keeps the payment mapping operator-set and its rollback composes"
