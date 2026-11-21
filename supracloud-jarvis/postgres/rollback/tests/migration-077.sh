#!/usr/bin/env bash
#
# Migration 077 records where the chain model changed, and refuses to forget it
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# 077 writes down one fact: the highest `seq` that existed when the
# Trust Ledger stopped being one global chain and became one per
# tenant. Anchors issued before that boundary anchored the whole
# deployment; anchors after it name a tenant. The owner's rule is that
# an anchor may only verify the chain model it was created for, so this
# boundary is the thing that makes the distinction checkable — and
# losing it is silent, because `chain_eras` then returns an empty list
# and every anchor falls through to "unknown".
#
# On its own disposable database, because the refusal below needs a
# ledger row ABOVE the boundary and the shared one must not carry it
# into the assertions after this step.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest077
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE ${DB}"
# `init.sql` first, then the numbered chain — the same order the 076
# step uses. A glob of `postgres/0*.sql` alone skips init.sql, and
# init.sql is what creates the extensions: the chain then dies at the
# first `uuid_generate_v4()` default with "function does not exist",
# which reads like a migration defect rather than a missing prologue.
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null
done
q() { ${PSQL} -d ${DB} -tAc "$1"; }

[ "$(q "SELECT count(*) FROM trust_ledger_chain_eras")" = "2" ] \
  || { echo "::error::077 did not record exactly two chain eras"; exit 1; }
[ "$(q "SELECT count(*) FROM trust_ledger_chain_eras WHERE ends_at_seq IS NULL")" = "1" ] \
  || { echo "::error::077 left none or several open eras"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','trust_ledger_chain_eras','UPDATE')")" = "f" ] \
  || { echo "::error::ira_app can move the chain-era boundary"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','trust_ledger_chain_eras','SELECT')")" = "t" ] \
  || { echo "::error::ira_app cannot READ the chain eras it must consult"; exit 1; }

# With nothing above the boundary the rollback is harmless, and must
# work — a rollback that refuses unconditionally is not a rollback.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/077_ledger_chain_eras_down.sql
[ "$(q "SELECT to_regclass('public.trust_ledger_chain_eras') IS NULL")" = "t" ] \
  || { echo "::error::rollback/077 left its table behind"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/077_ledger_chain_eras.sql
[ "$(q "SELECT count(*) FROM trust_ledger_chain_eras")" = "2" ] \
  || { echo "::error::077 did not re-apply"; exit 1; }

# ...and now the refusal. Write a ledger row ABOVE the boundary and
# the rollback must decline, because dropping the table would destroy
# the only copy of a boundary that anchors have been issued against —
# and re-applying would stamp a later, wrong one.
BOUNDARY="$(q "SELECT ends_at_seq FROM trust_ledger_chain_eras WHERE era = 1")"
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "INSERT INTO trust_ledger (user_id, action_id, kind, record, prev_hash, hash, body_digest)
   VALUES ('ci','after-cutover','action','{}'::jsonb, repeat('0',64), repeat('a',64), repeat('d',64))"
[ "$(q "SELECT count(*) FROM trust_ledger WHERE seq > ${BOUNDARY}")" = "1" ] \
  || { echo "::error::the fixture row did not land above the boundary"; exit 1; }

if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q \
     -f postgres/rollback/077_ledger_chain_eras_down.sql 2>/dev/null; then
  echo "::error::rollback/077 dropped the cutover record after rows were written above it"
  exit 1
fi
[ "$(q "SELECT to_regclass('public.trust_ledger_chain_eras') IS NOT NULL")" = "t" ] \
  || { echo "::error::the refused rollback still dropped the table"; exit 1; }
echo "✅ EXPECTED_REFUSAL / PASS — 077-down refuses once the boundary is in use"

# ── Re-applying, with the boundary IN USE ────────────────────────
#
# The rollback refuses here. The migration itself must not: it is
# re-applied inside the down/up cycle so that 042's
# `GRANT … ON ALL TABLES` does not leave `ira_app` able to move this
# boundary. That is not a hypothetical — it is what turned the
# recompose step red, with the row above written and the privilege
# quietly restored.
#
# Two things have to hold at once, and they pull in opposite
# directions: the re-apply must SUCCEED despite rows above the
# boundary, and it must NOT move the boundary those rows sit above.
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c \
  "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
   TO ira_app, ira_worker"
[ "$(q "SELECT has_table_privilege('ira_app','trust_ledger_chain_eras','UPDATE')")" = "t" ] \
  || { echo "::error::the 042-shaped GRANT did not reach the chain-era table, so this check proves nothing"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/077_ledger_chain_eras.sql
[ "$(q "SELECT ends_at_seq FROM trust_ledger_chain_eras WHERE era = 1")" = "${BOUNDARY}" ] \
  || { echo "::error::re-applying 077 moved the boundary from ${BOUNDARY}; the row written above it is now classified legacy-global"; exit 1; }
[ "$(q "SELECT has_table_privilege('ira_app','trust_ledger_chain_eras','UPDATE')")" = "f" ] \
  || { echo "::error::re-applying 077 did not take back the UPDATE that a blanket GRANT restored"; exit 1; }
echo "✅ 077 re-applies over a boundary in use: privilege restored, boundary unmoved"

${PSQL} -d postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE ${DB}"
echo "✅ 077 and its rollback compose, and the refusal holds"
