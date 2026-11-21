#!/usr/bin/env bash
#
# Migration 078 keeps the price out of the runtime's hands
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 078 adds the commercial layer above metering. Two properties are worth
# a disposable database of their own, and neither is provable on the
# shared one.
#
# FIRST: the migration seeds NO PRICE and NO PLAN. Setting a subscription
# price is an OWNER action (COST_AND_CAPACITY_REGISTER.md, phase 11) and
# "prices are not to be finalised". A migration that quietly seeded a
# zero price would make every customer free and look like it worked.
#
# SECOND: the rollback REFUSES once a tenant is subscribed, and proving
# that needs a subscription to exist — which the shared database must not
# carry into the assertions after this step.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest078
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
q() { ${PSQL} -d ${DB} -tAc "$1"; }

# ── the owner boundary ────────────────────────────────────────────
#
# This was a bare "no price row exists anywhere in the chain", and B2
# made it false for the right reason: 086 records the owner's IRA Pro
# decision, so the chain now carries exactly one. The property worth
# holding is not "no price" — it is that the ONLY price came from the
# owner, and that 078 itself still seeds nothing. Both halves are
# asserted, because either alone is satisfiable by the wrong database.
#
# First half: the whole chain carries one price, at the owner's
# figure. A second row appearing anywhere is a migration deciding what
# a customer pays — and it would be the row a later
# `ORDER BY effective_from DESC LIMIT 1` might pick.
[ "$(q "SELECT count(*) FROM billing_plan_prices")" = "1" ] \
  || { echo "::error::the migration chain carries $(q "SELECT count(*) FROM billing_plan_prices") price rows. A price is an owner decision, recorded once, in 086."; exit 1; }
[ "$(q "SELECT plan_id || ':' || unit_amount_minor || ':' || currency || ':' || recurrence FROM billing_plan_prices")" = "ira_pro:1500:GBP:monthly" ] \
  || { echo "::error::the chain's only price is not the owner's recorded IRA Pro decision (1500 GBP monthly)"; exit 1; }

# Second half: peel the commercial layer back off — newest first, as
# everywhere else — and what remains is 078's own position. It seeds
# nothing. Neither rollback has anything to refuse yet: no checkout
# session is open and nobody is subscribed.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/087_billing_provider_lifecycle_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/086_ira_pro_plan_and_price_down.sql
[ "$(q "SELECT count(*) FROM billing_plan_prices")" = "0" ] \
  || { echo "::error::a price survives with 086 peeled, so something at or below 078 seeds one. Setting a price is an owner action."; exit 1; }
[ "$(q "SELECT count(*) FROM billing_plans")" = "0" ] \
  || { echo "::error::a plan survives with 086 peeled. Which plans exist is a product decision."; exit 1; }

# ── the runtime cannot decide what it charges or is owed ──────────
[ "$(q "SELECT has_table_privilege('ira_app','billing_plan_prices','INSERT')")" = "f" ] \
  || { echo "::error::ira_app can write billing_plan_prices"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','tenant_entitlement_overrides','INSERT')")" = "f" ] \
  || { echo "::error::ira_app can raise its own caller's limits"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','tenant_subscriptions','DELETE')")" = "f" ] \
  || { echo "::error::ira_app can delete a subscription; cancelled and never-subscribed would look identical"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='tenant_subscriptions'")" = "t" ] \
  || { echo "::error::FORCE RLS is not set on tenant_subscriptions"; exit 1; }

# ── unused: the rollback is harmless and must work ────────────────
#
# 079 comes off FIRST. `invoices.subscription_id` references
# `tenant_subscriptions`, so from 079 onwards `rollback/078` is no
# longer standalone — peeling 078 by itself fails on the foreign key,
# which is how this step went red the first time 079 existed. The
# newest layer comes off first here for exactly the reason it does in
# the main rollback cycle.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/079_invoices_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/078_billing_plans_and_entitlements_down.sql
[ "$(q "SELECT to_regclass('public.tenant_subscriptions') IS NULL")" = "t" ] \
  || { echo "::error::rollback/078 left its tables behind"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/078_billing_plans_and_entitlements.sql

# ── used: a subscription exists, and the rollback must decline ────
# `-q` matters: without it psql appends its "INSERT 0 1" command tag
# to the RETURNING value and the uuid arrives with a second line glued
# to it. The insert below then fails on a malformed uuid, no
# subscription exists, the rollback succeeds — and the step reports
# "the rollback did not refuse" about a rollback that was never given
# anything to refuse. A check that fails for the wrong reason costs as
# much as one that passes for the wrong reason; this one was caught by
# running the step rather than by reading it.
TENANT="$(${PSQL} -d ${DB} -tAqc \
  "INSERT INTO tenants (name, slug) VALUES ('p11-078','p11-078') RETURNING id")"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO billing_plans (plan_id, display_name) VALUES ('ci_probe','CI probe')"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenant_subscriptions (tenant_id, plan_id, current_period_end)
   VALUES ('${TENANT}', 'ci_probe', NOW() + INTERVAL '30 days')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/078_billing_plans_and_entitlements_down.sql 2>/dev/null; then
  echo "::error::rollback/078 dropped the billing tables while a tenant was subscribed"
  exit 1
fi
[ "$(q "SELECT to_regclass('public.tenant_subscriptions') IS NOT NULL")" = "t" ] \
  || { echo "::error::the refused rollback still dropped the table"; exit 1; }
echo "✅ EXPECTED_REFUSAL / PASS — 078-down refuses once a tenant is subscribed"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
echo "✅ 078 sets no price, keeps the runtime out, and its rollback composes"
