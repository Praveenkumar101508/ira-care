#!/usr/bin/env bash
#
# Migration 079 makes an issued invoice evidence rather than a row
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 079 is where metering becomes money. Three properties earn a
# disposable database, and none of them is provable on the shared one —
# each needs an ISSUED invoice to exist, and an issued invoice cannot be
# deleted afterwards, which is precisely the property being tested.
#
# FIRST: an issued invoice cannot be edited or deleted BY ANYONE,
# including the schema owner this step connects as. Withholding DELETE
# from `ira_app` is not the same control: a privilege says who may ask,
# a trigger says the answer is no.
#
# SECOND: `issued -> void` is permitted and alters no amount. Without
# it the table would be unusable — a wrong bill could never be
# corrected — so an implementation that refused everything would look
# maximally safe and be useless.
#
# THIRD: the rollback REFUSES once anything has been issued.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest079
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
q() { ${PSQL} -d ${DB} -tAc "$1"; }

# ── the runtime cannot make a bill disappear ──────────────────────
[ "$(q "SELECT has_table_privilege('ira_app','invoices','DELETE')")" = "f" ] \
  || { echo "::error::ira_app can DELETE an invoice"; exit 1; }
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='invoices'")" = "t" ] \
  || { echo "::error::FORCE RLS is not set on invoices"; exit 1; }
[ "$(q "SELECT count(*) FROM invoices")" = "0" ] \
  || { echo "::error::079 seeded invoices. Nothing has been billed."; exit 1; }

# ── an invoice, issued. `-tAqc` matters here for the same reason it
# does in the 078 step: without `-q`, psql glues its "INSERT 0 1"
# command tag onto the RETURNING value and every id below arrives
# malformed. The step would then fail on a bad uuid while reporting
# something about immutability.
TENANT="$(${PSQL} -d ${DB} -tAqc \
  "INSERT INTO tenants (name, slug) VALUES ('p11-079','p11-079') RETURNING id")"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO billing_plans (plan_id, display_name) VALUES ('ci_probe','CI probe')"
SUB="$(${PSQL} -d ${DB} -tAqc \
  "INSERT INTO tenant_subscriptions (tenant_id, plan_id, current_period_end)
   VALUES ('${TENANT}','ci_probe', NOW() + INTERVAL '30 days') RETURNING id")"
INV="$(${PSQL} -d ${DB} -tAqc \
  "INSERT INTO invoices (tenant_id, subscription_id, period_start, period_end,
                         currency, total_minor, status, issued_at)
   VALUES ('${TENANT}','${SUB}', NOW() - INTERVAL '30 days', NOW(),
           'GBP', 2500, 'issued', NOW()) RETURNING id")"

# ── it cannot be edited, and cannot be deleted ────────────────────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
     "UPDATE invoices SET total_minor = 1 WHERE id = '${INV}'" 2>/dev/null; then
  echo "::error::an ISSUED invoice's amount was changed"
  exit 1
fi
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
     "DELETE FROM invoices WHERE id = '${INV}'" 2>/dev/null; then
  echo "::error::an ISSUED invoice was deleted"
  exit 1
fi
[ "$(q "SELECT total_minor FROM invoices WHERE id = '${INV}'")" = "2500" ] \
  || { echo "::error::the refused edits changed the amount anyway"; exit 1; }
echo "✅ EXPECTED_REFUSAL / PASS — an issued invoice is immutable, even to the schema owner"

# ── the rollback declines while it exists ─────────────────────────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/079_invoices_down.sql 2>/dev/null; then
  echo "::error::rollback/079 dropped the invoice tables after one was issued"
  exit 1
fi
[ "$(q "SELECT to_regclass('public.invoices') IS NOT NULL")" = "t" ] \
  || { echo "::error::the refused rollback still dropped the table"; exit 1; }
# …and the refusal left FORCE RLS exactly as it found it. The guard
# counts under NO FORCE so it can see rows it would otherwise be
# blind to — an unbound read under FORCE RLS does not error, it
# matches nothing, and a guard that read zero that way would report
# "nothing to lose" about a table full of issued invoices.
#
# What restores the control is the transaction, not the ALTER that
# follows the count: the RAISE aborts, and DDL in PostgreSQL rolls
# back with everything else. Planting a missing restore therefore does
# NOT make this line fail, and an assertion that cannot fail is not a
# check. It is kept because it DOES fail for the defect that can
# really happen — the toggle drifting outside the transaction, where
# nothing would undo it — and that was confirmed by planting exactly
# that and watching this line go red.
[ "$(q "SELECT relforcerowsecurity FROM pg_class WHERE relname='invoices'")" = "t" ] \
  || { echo "::error::the refused rollback left FORCE RLS off on invoices"; exit 1; }
echo "✅ EXPECTED_REFUSAL / PASS — 079-down refuses once an invoice is issued"

# ── voiding is the way out, and changes no amount ─────────────────
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "UPDATE invoices SET status='void', voided_at=NOW(), void_reason='ci probe'
    WHERE id = '${INV}'"
[ "$(q "SELECT total_minor FROM invoices WHERE id = '${INV}'")" = "2500" ] \
  || { echo "::error::voiding altered the amount"; exit 1; }
# A void invoice is still not a draft, so the rollback still refuses:
# the record that a bill was SENT survives the bill being withdrawn.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/079_invoices_down.sql 2>/dev/null; then
  echo "::error::rollback/079 dropped the tables once the invoice was voided"
  exit 1
fi

# ── unused, it composes ──────────────────────────────────────────
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "SET session_replication_role = replica; DELETE FROM invoice_lines; DELETE FROM invoices"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/079_invoices_down.sql
[ "$(q "SELECT to_regclass('public.invoices') IS NULL")" = "t" ] \
  || { echo "::error::rollback/079 left its tables behind"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/079_invoices.sql
[ "$(q "SELECT to_regclass('public.invoices') IS NOT NULL")" = "t" ] \
  || { echo "::error::079 did not re-apply"; exit 1; }

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
echo "✅ 079 makes an issued invoice immutable and its rollback composes"
