#!/usr/bin/env bash
#
# Migration 086/087 — the price is one row, and 086 refuses to leave
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# B2. Two properties on a disposable database, because neither can be
# proved in the shared cycle: the second one deliberately REFUSES, and a
# refusal in the shared cycle would stop the cycle.
#
# 1. `086` is IDEMPOTENT. It writes the owner's price with a FIXED
# `effective_from`, and `plan_price_unique_at_instant` is what makes
# a second application insert nothing rather than a duplicate at the
# same amount. A migration that quietly doubles its own rows is not
# reversible in any useful sense — and the SECOND row would be the
# one a later `ORDER BY effective_from DESC LIMIT 1` picked.
#
# 2. `rollback/086` REFUSES while a tenant holds a live ira_pro
# subscription. Removing the plan would leave that row naming a
# plan that does not exist, and no invoice issued under it could be
# reconstructed. The refusal must be actionable, so this proves both
# halves: it refuses with the subscription present, the PRICE
# SURVIVES the refusal, and it succeeds once the subscription is
# gone.
#
# The amount is asserted, not the presence of a row. A price row that
# came back with a different figure would satisfy "the migration applied"
# and charge somebody the wrong amount.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest087
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
for f in postgres/init.sql postgres/0[0-8][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done

# 1. Idempotency — apply 086 a second time and count the rows.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/086_ira_pro_plan_and_price.sql
test "$(${PSQL} -d ${DB} -tAc "SELECT count(*) FROM billing_plan_prices WHERE plan_id='ira_pro'")" = "1" \
  || { echo "::error::086 applied twice produced more than one ira_pro price row"; exit 1; }
test "$(${PSQL} -d ${DB} -tAc "SELECT unit_amount_minor || ':' || currency || ':' || recurrence FROM billing_plan_prices WHERE plan_id='ira_pro'")" = "1500:GBP:monthly" \
  || { echo "::error::086 did not record the owner's price as 1500 GBP monthly"; exit 1; }
echo "✅ 086 is idempotent and records 1500 GBP monthly, once"

# 2. The rollback refuses while somebody is subscribed.
#
# `-tAqc`, not `-tAc`. Without `-q` psql appends its "INSERT 0 1"
# command tag after the RETURNING value, and `tr -d '[:space:]'`
# then glues the two into `<uuid>INSERT01`. The 078 step above
# carries a comment about exactly this, written when it happened
# there — and this step reproduced it anyway, because the comment
# lives next to the code that was fixed rather than next to the
# next place someone writes `-tAc ... RETURNING`.
#
# It was invisible for four CI runs: this step is ordered after the
# 078 step, 078 failed first every time, and a skipped step reports
# nothing. A step that has never executed is not a passing step.
TENANT=$(${PSQL} -d ${DB} -tAqc "INSERT INTO tenants (name,slug,edition) VALUES ('b2guard','b2guard','cloud') RETURNING id" | tr -d '[:space:]')
case "${TENANT}" in
  ????????-????-????-????-????????????) ;;
  *) echo "::error::the tenant id came back as '${TENANT}', not a uuid. Everything below would fail on a malformed insert and the refusal test would prove nothing."; exit 1 ;;
esac
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO tenant_subscriptions (tenant_id,plan_id,status,current_period_end)
   VALUES ('${TENANT}','ira_pro','active',NOW()+interval '30 days')"
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/086_ira_pro_plan_and_price_down.sql 2>/dev/null; then
  echo "::error::rollback/086 removed the plan while a tenant held a live subscription"
  exit 1
fi
echo "✅ EXPECTED_REFUSAL / PASS — 086 will not remove a plan somebody is on"
test "$(${PSQL} -d ${DB} -tAc "SELECT count(*) FROM billing_plan_prices WHERE plan_id='ira_pro'")" = "1" \
  || { echo "::error::the price was destroyed by a rollback that refused"; exit 1; }
echo "✅ the price survived the refusal — nothing was half-done"

# …and the refusal is actionable.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "DELETE FROM tenant_subscriptions WHERE tenant_id='${TENANT}'"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/086_ira_pro_plan_and_price_down.sql
test "$(${PSQL} -d ${DB} -tAc "SELECT count(*) FROM billing_plan_prices WHERE plan_id='ira_pro'")" = "0" \
  || { echo "::error::rollback/086 left the price behind once nobody was subscribed"; exit 1; }
echo "✅ 086's refusal is actionable — cancelling the subscription is what unblocks it"

# 3. 087 reverses and goes back on, with its boundary intact.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/087_billing_provider_lifecycle_down.sql
test "$(${PSQL} -d ${DB} -tAc "SELECT to_regclass('public.billing_checkout_sessions') IS NULL")" = "t" \
  || { echo "::error::rollback/087 left billing_checkout_sessions behind"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/087_billing_provider_lifecycle.sql
test "$(${PSQL} -d ${DB} -tAc "SELECT relforcerowsecurity FROM pg_class WHERE relname='billing_checkout_sessions'")" = "t" \
  || { echo "::error::087 re-applied billing_checkout_sessions WITHOUT forced row-level security — every tenant's checkout attempts would be visible to every other"; exit 1; }
echo "✅ 087 reverses and re-applies with FORCE RLS intact"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
