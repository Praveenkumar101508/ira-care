#!/usr/bin/env bash
#
# Migration 070 makes the dataset lifecycle state, not a directory
#
# Extracted verbatim from the `tenant-isolation` CI job, which had grown to
# 5,354 lines of inline YAML. The reasoning below is the original, unchanged.
#
# Run it directly against a database that already has the migration chain
# applied — see postgres/rollback/tests/README.md. CI runs this same file, so
# what passes here is what passes there.
#
# Same shape as 054 through 069. On its own disposable database, so a
# destructive rollback test cannot reach the development one.
#
# BASELINE_MODEL_INVENTORY §6 asks for "Datasets
# (raw/quarantined/reviewed/train/val/test)" and "Dataset cards,
# licences, PII/secret scanning, contamination checks". Both describe a
# DIRECTORY LAYOUT, which is the easiest thing in Phase 7's list to
# satisfy dishonestly: a file that moved from raw/ to reviewed/ has been
# moved, not reviewed.
#
# The load-bearing assertion here is the digest one. A check that passed
# is a fact about the bytes it read, and four passing checks against a
# DIFFERENT digest would promote a dataset nobody scanned while every
# record looked current.
set -euo pipefail

# CI supplies these through the job's `services:` block; the defaults make a
# local run against a scratch cluster identical to the CI one.
export PGPASSWORD="${PGPASSWORD:-postgres}"
PGHOST="${PGHOST:-localhost}"; export PGHOST
PGPORT="${PGPORT:-5432}"; export PGPORT

# The original step ran with `working-directory: supracloud-jarvis`; resolve it from this
# file so the script works from anywhere.
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

DB=iratest070
PSQL="psql -h localhost -U postgres"
${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
${PSQL} -d postgres -c "CREATE DATABASE ${DB}"
q() { ${PSQL} -d ${DB} -tAc "$1"; }
reg() { ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$1"; }
refuse() {
  # $1 expected message, $2 SQL, $3 what success would mean
  if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -c "$2" 2>/tmp/c070.err; then
    echo "::error::$3"; exit 1
  fi
  grep -q "$1" /tmp/c070.err \
    || { echo "::error::refused for the wrong reason (wanted $1):"; cat /tmp/c070.err; exit 1; }
}
for f in postgres/init.sql postgres/[0-9][0-9][0-9]_*.sql; do
  ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f "$f"
done

A=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'a')
B=sha256:$(printf 'x%.0s' $(seq 1 64) | tr 'x' 'b')
WHO='CI FIXTURE — not a determination'

for tbl in training_datasets dataset_checks; do
  [ "$(q "SELECT to_regclass('public.${tbl}') IS NOT NULL")" = "t" ] \
    || { echo "::error::070 did not create ${tbl}"; exit 1; }
  for p in INSERT UPDATE DELETE; do
    [ "$(q "SELECT has_table_privilege('ira_app','${tbl}','$p')")" = "f" ] \
      || { echo "::error::ira_app holds $p on ${tbl} — a process that answers requests could certify its own training data"; exit 1; }
  done
done
echo "✅ 070 creates the governance tables and the runtime role cannot write them"

reg "INSERT INTO training_datasets (name,version,source,content_sha256,record_count) VALUES ('ci70',1,'ci fixture','${A}',10)"
D=$(q "SELECT id FROM training_datasets WHERE name='ci70'")

# ── The lifecycle is not a directory ────────────────────────────
refuse "no passing pii check" \
  "UPDATE training_datasets SET state='reviewed' WHERE id='${D}'::uuid" \
  "a dataset reached 'reviewed' with no checks at all"
echo "✅ 070 refuses to promote a dataset with no checks"

# Four passing checks — against the WRONG digest.
for k in pii secret contamination licence; do
  reg "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,tool,tool_version) VALUES ('${D}'::uuid,'$k','${B}','passed','ci','1')"
done
refuse "no passing pii check" \
  "UPDATE training_datasets SET state='reviewed' WHERE id='${D}'::uuid" \
  "four checks against a DIFFERENT digest promoted a dataset nobody scanned"
echo "✅ 070 refuses checks that describe different bytes"

for k in pii secret contamination licence; do
  reg "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,tool,tool_version) VALUES ('${D}'::uuid,'$k','${A}','passed','ci','1')"
done

# ── The determinations engineering may not make ─────────────────
refuse "its licence is unknown" \
  "UPDATE training_datasets SET state='reviewed' WHERE id='${D}'::uuid" \
  "a dataset with no licence determination was promoted"
for who in migration automation system script collector engineering ci operator postgres tbd; do
  refuse "dataset_licence_determination_is_attributed" \
    "UPDATE training_datasets SET licence_classification='known_approved', licence_classified_by='${who}', licence_classified_at=NOW() WHERE id='${D}'::uuid" \
    "a dataset licence approved by '${who}' was accepted"
done
echo "✅ 070 refuses a dataset licence approved by a process"

reg "UPDATE training_datasets SET licence_classification='known_approved', licence_classified_by='${WHO}', licence_classified_at=NOW() WHERE id='${D}'::uuid"
refuse "no lawful basis is recorded" \
  "UPDATE training_datasets SET state='reviewed' WHERE id='${D}'::uuid" \
  "a dataset was promoted with no lawful basis — PP-05"
echo "✅ 070 refuses a dataset with no lawful basis"

reg "UPDATE training_datasets SET lawful_basis='CI fixture only.', lawful_basis_by='${WHO}', lawful_basis_at=NOW() WHERE id='${D}'::uuid"
refuse "contains personal data" \
  "UPDATE training_datasets SET state='reviewed' WHERE id='${D}'::uuid" \
  "a dataset was promoted while nobody had answered the personal-data question"
echo "✅ 070 refuses until a human answers the personal-data question"

# ── The positive case, and the demotion ─────────────────────────
reg "UPDATE training_datasets SET contains_personal_data='no' WHERE id='${D}'::uuid"
reg "UPDATE training_datasets SET state='train' WHERE id='${D}'::uuid"
[ "$(q "SELECT state FROM training_datasets WHERE id='${D}'::uuid")" = "train" ] \
  || { echo "::error::a fully governed dataset could not be promoted — the gate refuses everything"; exit 1; }
echo "✅ 070 promotes a fully governed dataset"

reg "UPDATE training_datasets SET content_sha256='${B}' WHERE id='${D}'::uuid"
[ "$(q "SELECT state FROM training_datasets WHERE id='${D}'::uuid")" = "raw" ] \
  || { echo "::error::a promoted dataset survived a content change; its checks now describe bytes nobody scanned"; exit 1; }
echo "✅ 070 demotes a promoted dataset when its content changes"

# ── Incoherent check records ────────────────────────────────────
refuse "a_pass_found_nothing" \
  "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,finding_count,tool,tool_version) VALUES ('${D}'::uuid,'pii','${A}','passed',3,'ci','1')" \
  "a passing check reported findings — the shape a scanner that 'handled' them would produce"
refuse "a_failure_found_something" \
  "INSERT INTO dataset_checks (dataset_id,check_kind,content_sha256,outcome,finding_count,tool,tool_version) VALUES ('${D}'::uuid,'pii','${A}','failed',0,'ci','1')" \
  "a failing check reported no findings"
echo "✅ 070 refuses incoherent check records"

# ── The rollback refuses to discard a human determination ───────
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/070_training_datasets_down.sql 2>/tmp/r070.err; then
  echo "::error::rollback/070 discarded a licence determination and a recorded lawful basis"
  exit 1
fi
grep -q "070-down REFUSING" /tmp/r070.err \
  || { echo "::error::rollback/070 failed for the wrong reason:"; cat /tmp/r070.err; exit 1; }
echo "✅ rollback/070 refuses to discard a human determination"

reg "UPDATE training_datasets SET licence_classification='review_required', licence_classified_by=NULL, licence_classified_at=NULL, lawful_basis=NULL, lawful_basis_by=NULL, lawful_basis_at=NULL"

# ── A chain reverses in reverse order, and says so ──────────────
#
# This database holds the WHOLE chain, so 071 is applied and
# training_run_datasets references training_datasets. Before P7-04
# this step failed here on PostgreSQL's own "cannot drop table
# ... because other objects depend on it" — accurate, arriving after
# the determination guard had passed, and reading like the rollback
# was broken rather than like it was run in the wrong order.
if ${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/070_training_datasets_down.sql 2>/tmp/o070.err; then
  echo "::error::rollback/070 ran while 071 was applied"
  exit 1
fi
grep -q "070-down OUT OF ORDER" /tmp/o070.err \
  || { echo "::error::rollback/070 failed without naming the file to run first:"; cat /tmp/o070.err; exit 1; }
echo "✅ rollback/070 refuses while a later migration depends on it, by name"

${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/072_model_promotion_gate_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/071_model_training_runs_down.sql
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/rollback/070_training_datasets_down.sql
[ "$(q "SELECT to_regclass('public.training_datasets') IS NULL")" = "t" ] \
  || { echo "::error::rollback/070 refused but left the table"; exit 1; }
${PSQL} -d ${DB} -v ON_ERROR_STOP=1 -q -f postgres/070_training_datasets.sql
[ "$(q "SELECT count(*) FROM pg_trigger WHERE tgname='training_dataset_promotion' AND NOT tgisinternal")" = "1" ] \
  || { echo "::error::070 re-applied without its promotion gate"; exit 1; }
echo "✅ 070 and its rollback compose"

${PSQL} -d postgres -c "DROP DATABASE IF EXISTS ${DB}"
